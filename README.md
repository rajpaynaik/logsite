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

**Reads errors from**

[![Databricks](https://img.shields.io/badge/Databricks-FF3621?style=for-the-badge&logo=databricks&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/databricks.md)
[![Loki](https://img.shields.io/badge/Loki-F46800?style=for-the-badge&logo=grafana&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md)
[![OpenSearch](https://img.shields.io/badge/OpenSearch-005EB8?style=for-the-badge&logo=opensearch&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md)
[![CloudWatch](https://img.shields.io/badge/CloudWatch-FF4F8B?style=for-the-badge)](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md)
[![Sentry](https://img.shields.io/badge/Sentry-362D59?style=for-the-badge&logo=sentry&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/sentry.md)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-425CC7?style=for-the-badge&logo=opentelemetry&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/opentelemetry.md)

**Shows them in**

[![Grafana](https://img.shields.io/badge/Grafana-F46800?style=for-the-badge&logo=grafana&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md)
[![VS Code](https://img.shields.io/badge/VS%20Code-007ACC?style=for-the-badge)](https://github.com/rajpaynaik/logsite/blob/main/docs/vscode.md)
[![Claude](https://img.shields.io/badge/Claude-D97757?style=for-the-badge&logo=claude&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md)
[![Slack](https://img.shields.io/badge/Slack-4A154B?style=for-the-badge)](https://github.com/rajpaynaik/logsite/blob/main/docs/watch.md)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=for-the-badge&logo=githubactions&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/github-action.md)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/kubernetes.md)

**In code written in**

[![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md)
[![Java](https://img.shields.io/badge/Java-ED8B00?style=for-the-badge&logo=openjdk&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md)
[![Go](https://img.shields.io/badge/Go-00ADD8?style=for-the-badge&logo=go&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md)
[![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md)
[![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?style=for-the-badge&logo=javascript&logoColor=black)](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md)

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

[![Databricks](https://img.shields.io/badge/Databricks-FF3621?style=for-the-badge&logo=databricks&logoColor=white)](https://github.com/rajpaynaik/logsite/blob/main/docs/databricks.md)

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
