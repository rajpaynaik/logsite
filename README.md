# logsite

[![PyPI](https://img.shields.io/pypi/v/logsite?label=PyPI&cacheSeconds=3600)](https://pypi.org/project/logsite/)
[![Python](https://img.shields.io/pypi/pyversions/logsite?cacheSeconds=86400)](https://pypi.org/project/logsite/)
[![CI](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml/badge.svg)](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/rajpaynaik/logsite/blob/main/LICENSE)

**Your logs say something broke. logsite tells you which line of your code, and writes a test that proves it.**

It reads the logs you already have, and failed Databricks jobs, groups thousands of stack
traces into a few causes, and points each one at your line of code. No SDK, nothing to add to
your services.

```bash
pip install logsite
```

## Works with

**Reads errors from**<br>
<img src="https://cdn.simpleicons.org/databricks/FF3621" height="18" alt="">&nbsp;Databricks &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/grafana/F46800" height="18" alt="">&nbsp;Loki &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/opensearch/2D86D9" height="18" alt="">&nbsp;OpenSearch &nbsp;&nbsp; <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/amazonwebservices/amazonwebservices-plain-wordmark.svg" height="18" alt="">&nbsp;CloudWatch &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/sentry/7E6AD9" height="18" alt="">&nbsp;Sentry &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/opentelemetry/F5A800" height="18" alt="">&nbsp;OpenTelemetry

**Shows them in**<br>
<img src="https://cdn.simpleicons.org/grafana/F46800" height="18" alt="">&nbsp;Grafana &nbsp;&nbsp; <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/vscode/vscode-original.svg" height="18" alt="">&nbsp;VS Code &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/claude/D97757" height="18" alt="">&nbsp;Claude &nbsp;&nbsp; <img src="https://cdn.jsdelivr.net/gh/devicons/devicon@v2.16.0/icons/slack/slack-original.svg" height="18" alt="">&nbsp;Slack &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/githubactions/2088FF" height="18" alt="">&nbsp;GitHub Actions &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/kubernetes/326CE5" height="18" alt="">&nbsp;Kubernetes

**In code written in**<br>
<img src="https://cdn.simpleicons.org/python/3776AB" height="18" alt="">&nbsp;Python &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/openjdk/ED8B00" height="18" alt="">&nbsp;Java &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/go/00ADD8" height="18" alt="">&nbsp;Go &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/typescript/3178C6" height="18" alt="">&nbsp;TypeScript &nbsp;&nbsp; <img src="https://cdn.simpleicons.org/javascript/F0DB4F" height="18" alt="">&nbsp;JavaScript

## Try it

The repo has a small demo shop with a planted bug and its production log.

```bash
pip install logsite pytest
git clone https://github.com/rajpaynaik/logsite && cd logsite/examples/bookstore
```

**1. What's failing?**

```
$ logsite errors --from ../../bench/repro/scenarios/supplier-timeout/incident.log
28 errors in incident.log · 2 group(s)

  #  TREND  COUNT  ERROR                                      RAISED AT       LOGGED AT
  1            25  TimeoutError: timed out                    supplier.py:31  api.py:36
  2             3  ConnectionRefusedError: cache unavailable  cache.py:15     api.py:23
```

**2. Write a test that reproduces it.**

```
$ logsite repro --from ../../bench/repro/scenarios/supplier-timeout/incident.log --write-test
wrote tests/test_repro_346fa937.py
```

**3. Check it fails the way production did.**

```
$ logsite agent --expect 346fa937 -- python -m pytest tests/test_repro_346fa937.py
✓ 346fa937 reproduced: TimeoutError through bookstore/supplier.py:31, as in production
```

Fix `supplier.py` until the test passes.

## Use it on your service

```bash
cd your-service
logsite errors --from app.log        # errors grouped by cause
logsite lint                         # logging mistakes in your code
```

To read from your log store, add a `logsite.toml`:

```toml
[backend]
type = "loki"                        # or "opensearch", "cloudwatch", "sentry", "databricks", "file"
url = "http://localhost:3100"
selector = '{service_name="checkout"}'
```

## Databricks

<img src="https://cdn.simpleicons.org/databricks/FF3621" height="40" alt="Databricks">

When a Databricks job fails, the failed run explains itself, right in Databricks: the cause,
the line of your code that raised it, the tasks it failed and the commit. Spark's
`Py4JJavaError` is unwrapped to the real error in your UDF.

No command line, no server, no token: import two notebooks, tick your jobs, **Run all**.
Platform teams can watch every job from outside instead, with Slack alerts in seconds. See
[Databricks](https://github.com/rajpaynaik/logsite/blob/main/docs/databricks.md) (beta).

## Watch production

`logsite watch` checks your logs every 5 minutes, stores every error in your own database,
and posts to Slack when an error is **new**, **back after a fix**, or **spiking**, when an
endpoint gets [slower](https://github.com/rajpaynaik/logsite/blob/main/docs/latency.md), or a
release made errors worse ([release health](https://github.com/rajpaynaik/logsite/blob/main/docs/releases.md)). No SDK:
it reads the log store you already have.

**Guided setup:** `logsite init` asks for your log store, Slack and database in the terminal,
testing each one; `logsite app` does the same in your browser.

**Or with a config file:**

```toml
[backend]
type = "loki"
url = "http://loki:3100"
selector = '{service_name="checkout"}'

[alerts]
slack_webhook_env = "SLACK_WEBHOOK_URL"   # a Slack incoming webhook

[store]                                   # leave out to use SQLite
url_env = "LOGSITE_DATABASE_URL"          # Postgres, MySQL, SQL Server or ClickHouse
```

```bash
pip install "logsite[postgres]"
logsite watch --dry-run                   # print the Slack messages first
logsite watch
```

Tables are created on first use. The first check loads the last 24 hours without posting;
`logsite db backfill --since 30d` loads more.

**Where to run it:** anywhere that can reach your log store: a server, Docker, or
Kubernetes. On Kubernetes (EKS included) it's one Deployment with one replica, config in a
ConfigMap, secrets in a Secret, and an IAM role for CloudWatch.
See [Run on Kubernetes](https://github.com/rajpaynaik/logsite/blob/main/docs/kubernetes.md).

## What else it does

- **Fix faster:** [write a failing test](https://github.com/rajpaynaik/logsite/blob/main/docs/repro.md) from a production error, or turn one
  into a [prompt for your AI assistant](https://github.com/rajpaynaik/logsite/blob/main/docs/prompt.md).
- **Check releases:** [`logsite release`](https://github.com/rajpaynaik/logsite/blob/main/docs/releases.md) shows whether a release made
  errors worse, with the suspect commits.
- **Catch slowdowns:** [`logsite slow`](https://github.com/rajpaynaik/logsite/blob/main/docs/latency.md) shows which endpoints got slower,
  from timings your logs already carry.
- **Spend less on logs:** see what [each log call costs](https://github.com/rajpaynaik/logsite/blob/main/docs/cost-and-leaks.md), and
  [lint](https://github.com/rajpaynaik/logsite/blob/main/docs/lint.md) logging mistakes in your code.
- **Work where you are:** [VS Code](https://github.com/rajpaynaik/logsite/blob/main/docs/vscode.md), [Grafana](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md),
  [Claude](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md) and a [GitHub Action](https://github.com/rajpaynaik/logsite/blob/main/docs/github-action.md).

## Documentation

Start with [Get started](https://github.com/rajpaynaik/logsite/blob/main/docs/get-started.md), or browse
[all the docs](https://github.com/rajpaynaik/logsite/blob/main/docs/README.md). Also: [Commands](https://github.com/rajpaynaik/logsite/blob/main/docs/commands.md) ·
[Configuration](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md) ·
[Changelog](https://github.com/rajpaynaik/logsite/blob/main/CHANGELOG.md)

Questions and ideas: [Discussions](https://github.com/rajpaynaik/logsite/discussions).
Contributions welcome, see [CONTRIBUTING.md](https://github.com/rajpaynaik/logsite/blob/main/CONTRIBUTING.md).

## License

Apache 2.0
