# logsite

[![PyPI](https://img.shields.io/pypi/v/logsite?label=PyPI&cacheSeconds=3600)](https://pypi.org/project/logsite/)
[![Python](https://img.shields.io/pypi/pyversions/logsite?cacheSeconds=86400)](https://pypi.org/project/logsite/)
[![CI](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml/badge.svg)](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/rajpaynaik/logsite/blob/main/LICENSE)

**Your logs say something broke. logsite tells you which line of your code, and writes a test that proves it.**

It reads the logs you already have (a file, Loki, OpenSearch, CloudWatch, Sentry or
OpenTelemetry), groups thousands of stack traces into a few causes, and points each one at
your line of code. No SDK, nothing to add to your services. Works with Python, Java, Go,
TypeScript and JavaScript.

```bash
pip install logsite
```

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
type = "loki"                        # or "opensearch", "cloudwatch", "sentry", "file"
url = "http://localhost:3100"
selector = '{service_name="checkout"}'
```

## Watch production

`logsite watch` checks your logs every 5 minutes, stores every error in your own database,
and posts to Slack when an error is **new**, **back after a fix**, or **spiking**. No SDK:
it reads the log store you already have.

**In the browser:** `logsite app` walks you through the log store, Slack and database, each
with a test button, then starts watching.

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

## More

| Command | What it does |
|---|---|
| `logsite app` | Set everything up and browse errors in your browser, no terminal needed |
| `logsite db backfill --since 30d` | Store errors in your own database (SQLite, Postgres, MySQL, SQL Server, ClickHouse) for instant date-range queries |
| `logsite investigate` | Find the deploy and commit behind an incident |
| `logsite grafana --store postgres` | A Grafana dashboard on your stored errors |

Also: a [GitHub Action](https://github.com/rajpaynaik/logsite/blob/main/docs/github-action.md),
a [VS Code extension](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite),
and an [MCP server](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md) for AI assistants.

## Docs

[Commands](https://github.com/rajpaynaik/logsite/blob/main/docs/commands.md) ·
[Configuration](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md) ·
[logsite app](https://github.com/rajpaynaik/logsite/blob/main/docs/app.md) ·
[Watch production](https://github.com/rajpaynaik/logsite/blob/main/docs/watch.md) ·
[Kubernetes](https://github.com/rajpaynaik/logsite/blob/main/docs/kubernetes.md) ·
[Reproduce an error](https://github.com/rajpaynaik/logsite/blob/main/docs/repro.md) ·
[Security](https://github.com/rajpaynaik/logsite/blob/main/docs/security.md) ·
[Changelog](https://github.com/rajpaynaik/logsite/blob/main/CHANGELOG.md)

Questions and ideas: [Discussions](https://github.com/rajpaynaik/logsite/discussions).
Contributions welcome, see [CONTRIBUTING.md](https://github.com/rajpaynaik/logsite/blob/main/CONTRIBUTING.md).

## License

Apache 2.0
