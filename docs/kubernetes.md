# Run logsite watch on Kubernetes (EKS)

`logsite watch` is one small process: every few minutes it reads your log store, stores the
errors in your database and posts new ones to Slack. On Kubernetes it's a Deployment with
**one replica** (two would send every alert twice).

```
          ┌──────────────── Kubernetes cluster ─────────────────┐
          │  Deployment: logsite-watch (1 replica)              │
          │   ├─ init container: git clone your repo            │──► GitHub (read-only deploy key)
          │   └─ logsite watch                                  │
          │        ├─ reads logs ──────────────────────────────►│──► Loki / CloudWatch / OpenSearch
          │        ├─ writes errors ───────────────────────────►│──► Postgres (e.g. RDS)
          │        └─ posts alerts ────────────────────────────►│──► Slack
          └─────────────────────────────────────────────────────┘
```

| Piece | On Kubernetes / AWS |
|---|---|
| Config (`logsite.toml`) | a ConfigMap |
| Secrets (Slack webhook, log store password, database URL) | a Secret, or AWS Secrets Manager through External Secrets |
| Your code, so errors map to your lines and past fixes | an init container that clones the repo with a read-only deploy key |
| Database | Postgres (RDS on AWS), so the pod needs no disk |
| CloudWatch access | IRSA or EKS Pod Identity: an IAM role with `logs:FilterLogEvents`, no AWS keys |
| Loki access | the in-cluster address, e.g. `http://loki.monitoring:3100` |

It's light: one log query and a few database writes every 5 minutes.

## 1. Build the image

There's no published image yet. Build one from the repo's Dockerfile and push it to your
registry (ECR on AWS):

```bash
docker build -t <account>.dkr.ecr.<region>.amazonaws.com/logsite:0.5.1 \
  --build-arg LOGSITE_VERSION=0.5.1 integrations/docker
docker push <account>.dkr.ecr.<region>.amazonaws.com/logsite:0.5.1
```

It installs logsite and git and runs as a non-root user. For Postgres or CloudWatch, add
the extras: change `pip install "logsite..."` in the Dockerfile to
`pip install "logsite[postgres,cloudwatch]..."`.

## 2. Config and secrets

```yaml
apiVersion: v1
kind: ConfigMap
metadata: {name: logsite-config}
data:
  logsite.toml: |
    [backend]
    type = "loki"
    url = "http://loki.monitoring:3100"
    selector = '{service_name="checkout"}'

    [alerts]
    slack_webhook_env = "SLACK_WEBHOOK_URL"

    [watch]
    every = "5m"

    [store]
    url_env = "LOGSITE_DATABASE_URL"
---
apiVersion: v1
kind: Secret
metadata: {name: logsite-secrets}
stringData:
  SLACK_WEBHOOK_URL: https://hooks.slack.com/services/...
  LOGSITE_DATABASE_URL: postgresql://logsite:...@mydb.xxxx.rds.amazonaws.com:5432/errors
```

For CloudWatch, use `type = "cloudwatch"`, `log_group = "/ecs/checkout"` and `region`
instead; credentials come from the pod's IAM role.

## 3. The Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: logsite-watch}
spec:
  replicas: 1
  strategy: {type: Recreate}             # never two at once
  selector: {matchLabels: {app: logsite-watch}}
  template:
    metadata: {labels: {app: logsite-watch}}
    spec:
      serviceAccountName: logsite        # with an IRSA role, for CloudWatch
      initContainers:
        - name: clone
          image: alpine/git:2.47.2
          args: ["clone", "--depth=200", "git@github.com:acme/checkout.git", "/repo"]
          env: [{name: GIT_SSH_COMMAND, value: "ssh -i /key/id -o StrictHostKeyChecking=accept-new"}]
          volumeMounts:
            - {name: repo, mountPath: /repo}
            - {name: deploy-key, mountPath: /key}
      containers:
        - name: watch
          image: <account>.dkr.ecr.<region>.amazonaws.com/logsite:0.5.1
          args: ["watch"]                # the image runs logsite --config-dir /repo
          envFrom: [{secretRef: {name: logsite-secrets}}]
          volumeMounts:
            - {name: repo, mountPath: /repo}
            - {name: config, mountPath: /repo/logsite.toml, subPath: logsite.toml}
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {memory: 512Mi}
      volumes:
        - {name: repo, emptyDir: {}}
        - {name: config, configMap: {name: logsite-config}}
        - {name: deploy-key, secret: {secretName: logsite-deploy-key, defaultMode: 0400}}
```

The deploy key is a read-only SSH key added to the repository (GitHub: **Settings → Deploy
keys**), stored as the Secret `logsite-deploy-key` with the key under `id`.

## 4. Check it

```bash
kubectl logs deploy/logsite-watch            # one line per check
kubectl exec deploy/logsite-watch -- logsite --config-dir /repo db status
```

The first check loads the last 24 hours into the database without posting to Slack. To
load more history: `kubectl exec deploy/logsite-watch -- logsite --config-dir /repo db backfill --since 30d`.

## Good to know

- **What it reads and writes:** it only reads your logs and code. It writes to its own tables
  in your database and posts to Slack. Values are masked (secrets, emails, card numbers)
  before they're stored. See [Data flow and security](security.md).
- **The cloned code goes stale after deploys.** Restart the pod after a release
  (`kubectl rollout restart deploy/logsite-watch`) so code lines and links match. Without the
  code, errors are still grouped, by the file and line in their stack traces.
- **Not yet available:** a published image, a health endpoint for `watch` and a Helm chart.
