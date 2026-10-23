# logsite

[![PyPI](https://img.shields.io/pypi/v/logsite?label=PyPI&cacheSeconds=3600)](https://pypi.org/project/logsite/)
[![Python](https://img.shields.io/pypi/pyversions/logsite?cacheSeconds=86400)](https://pypi.org/project/logsite/)
[![VS Code](https://img.shields.io/badge/VS%20Code-Marketplace-007ACC?logo=visualstudiocode)](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite)
[![CI](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml/badge.svg)](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/rajpaynaik/logsite/blob/main/LICENSE)

**Your logs say something broke. logsite tells you which line of your code, and writes a test that proves it.**

```bash
pip install logsite
```

![64,580 log lines in, 4 causes out with the file and line of each, then a failing test that reproduces the new one](https://raw.githubusercontent.com/rajpaynaik/logsite/main/docs/demo/demo.gif)

Point it at your code and the logs you already have (a log file, Loki, OpenSearch /
Elasticsearch or CloudWatch). It turns thousands of stack traces into a few causes, each
pointing at *your* line of code. No SDK, nothing to add to your services. Python, Java and Go.

## How is this different from Sentry, Datadog or Grafana?

**It needs no SDK and works on the logs you already have.** Sentry and Datadog see errors
from services you've instrumented, inside their platform. Grafana and Loki store and search
your logs, but don't know your code. logsite reads both your logs and your code, on your
machine:

| | logsite | Sentry, Datadog |
|---|---|---|
| Setup | point it at a log file or your log store | an SDK or agent in every service |
| Where it runs | your machine or CI; no logs leave it | their cloud |
| Knows your code | yes: maps each error to the line at your commit, even after the code moved | through the stack trace and a source integration |
| Output | causes, the line, and a failing test checked to fail the way production did | an issue page |
| Price | free, Apache 2.0 | per event or GB |

It doesn't replace them: there's no hosted UI or real-time alerting of its own. It works
alongside them, and plugs into Grafana as a data source and into Grafana alerts.

## Try it in 2 minutes

The repo has a small demo shop, `examples/bookstore`, with a planted bug: calls to a
supplier time out and are never retried. Its production log is in the repo too.

```bash
pip install logsite pytest
git clone https://github.com/rajpaynaik/logsite && cd logsite/examples/bookstore
```

**1. What's failing?** Read the log and group the errors by cause:

```
$ logsite errors --from ../../bench/repro/scenarios/supplier-timeout/incident.log
28 errors in incident.log · 2 group(s)

  #  TREND  COUNT  ERROR                                      RAISED AT       LOGGED AT
  1            25  TimeoutError: timed out                    supplier.py:31  api.py:36
  2             3  ConnectionRefusedError: cache unavailable  cache.py:15     api.py:23

━━ 1. TimeoutError: timed out ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 25
   Raised at    bookstore/supplier.py:31  restock()  (in socket)
                31 │ with urllib.request.urlopen(request, timeout=self.timeout) as resp:
   Logged at    bookstore/api.py:36
   Call path    api.handle › api.<lambda> › inventory.reorder › supplier.restock › socket
```

28 errors, 2 causes, and the exact line behind each one.

**2. Turn the error into a failing test.** logsite takes the function to call, the values
the failing request logged and the dependency to fake, and writes a pytest test:

```
$ logsite repro --from ../../bench/repro/scenarios/supplier-timeout/incident.log --write-test
Reproduce 346fa937 · TimeoutError: timed out · 25 error(s)
  Fail with    TimeoutError through bookstore/supplier.py:31 (restock)
  Call         reorder(sku, qty)  from bookstore.inventory  bookstore/inventory.py:13
  Fake         bookstore/supplier.py:31 calls urllib.request.urlopen(…): patch urllib.request.urlopen raise TimeoutError
  Inputs       sku   = BK-1802               logged at bookstore/inventory.py:17
               qty   = 10                    logged at bookstore/inventory.py:17

wrote tests/test_repro_346fa937.py
```

**3. Check it fails the way production did:** the same exception, through the same line.

```
$ logsite agent --expect 346fa937 -- python -m pytest tests/test_repro_346fa937.py
✓ 346fa937 reproduced: TimeoutError through bookstore/supplier.py:31, as in production
```

Now fix `supplier.py` until the test passes. Run your tests through `logsite agent` to get
the causes instead of hundreds of lines of output:

```
$ logsite agent -- python -m pytest
pytest: 1 failed, 6 passed · 1 cause(s) · exit 1

1. 1 test(s)  TimeoutError: timed out
   raised   bookstore/supplier.py:31 in restock()
```

## Use it on your own service

```bash
cd your-service
logsite lint                         # logging mistakes in your code; no logs needed
logsite errors --from downloaded.log  # errors grouped by cause, from any log file
```

To read from your log store, add a `logsite.toml`:

```toml
[backend]
type = "loki"                        # or "opensearch", "cloudwatch", "file"
url = "http://localhost:3100"
selector = '{service_name="checkout"}'
```

```bash
logsite errors                       # production errors from the last 24h, grouped by cause
logsite investigate                  # linked into incidents, with the deploy and commit behind them
logsite about app/checkout.py:42     # what production says about this line, before you change it
```

See [Configuration](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md) for every backend.

## What else it does

- **Finds the change behind an incident**: `logsite investigate` names the deploy and the
  commits just before the errors started. → [Errors and incidents](https://github.com/rajpaynaik/logsite/blob/main/docs/errors.md)
- **Remembers past fixes**: when an old error comes back, it shows the commit that fixed it
  last time. → [History](https://github.com/rajpaynaik/logsite/blob/main/docs/history.md)
- **Opens a pull request per new error**: a [GitHub Action](https://github.com/rajpaynaik/logsite/blob/main/docs/github-action.md)
  reads your logs on a schedule and opens a draft pull request with the reproduction test.
- **Blocks bad releases in CI**: `logsite errors --since-deploy $TAG --fail-on-new` fails the build if the release brought a new error.
- **Helps AI coding assistants**: the [Claude Code plugin](https://github.com/rajpaynaik/logsite/blob/main/docs/claude-code.md)
  and the [MCP server](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md) tell the assistant what a function
  does in production before it changes it. `/logsite:fix-error` reproduces an error, then fixes it.
- **Shows production in your editor**: how often each log call runs and which lines raise
  errors, right above the code. [VS Code](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite), or any LSP editor.
- **Cuts log costs**: which `logger.info(...)` calls make up your log bill, and secrets that
  reached your logs. → [Cost and leaks](https://github.com/rajpaynaik/logsite/blob/main/docs/cost-and-leaks.md)
- **Works with Grafana**: a data source, dashboards, alert summaries and Alloy rules to drop
  noisy lines. → [Grafana](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md)

## Supported

| | |
|---|---|
| **Languages** | Python (`logging`, structlog, loguru), Java (SLF4J, Log4j 2, java.util.logging, Lombok), Go (slog, zap, zerolog, logrus, logr, klog) |
| **Log stores** | Log files, Loki, OpenSearch / Elasticsearch, CloudWatch Logs |
| **Tests and builds** | pytest, `go test`, Maven, Gradle, Jest, Vitest, tsc, javac, gcc-style compilers |
| **Editors** | VS Code; Neovim, JetBrains, Helix, Zed through LSP |
| **AI assistants** | Claude Code plugin; any MCP client (Claude Desktop, Cursor, …) |

## Install

```bash
pip install logsite                  # the command-line tool
pip install "logsite[lsp,mcp]"       # + the editor language server and the MCP server
```

## Documentation

[Commands](https://github.com/rajpaynaik/logsite/blob/main/docs/commands.md) ·
[Configuration](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md) ·
[Errors and incidents](https://github.com/rajpaynaik/logsite/blob/main/docs/errors.md) ·
[Agent mode](https://github.com/rajpaynaik/logsite/blob/main/docs/agent.md) ·
[Reproduce an error](https://github.com/rajpaynaik/logsite/blob/main/docs/repro.md) ·
[GitHub Action](https://github.com/rajpaynaik/logsite/blob/main/docs/github-action.md) ·
[History](https://github.com/rajpaynaik/logsite/blob/main/docs/history.md) ·
[Grafana](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md) ·
[Languages](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md) ·
[Lint rules](https://github.com/rajpaynaik/logsite/blob/main/docs/lint.md) ·
[Cost and leaks](https://github.com/rajpaynaik/logsite/blob/main/docs/cost-and-leaks.md) ·
[Request timeline](https://github.com/rajpaynaik/logsite/blob/main/docs/timeline.md) ·
[MCP server](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md) ·
[Claude Code plugin](https://github.com/rajpaynaik/logsite/blob/main/docs/claude-code.md) ·
[How it works](https://github.com/rajpaynaik/logsite/blob/main/docs/how-it-works.md) ·
[Data flow and security](https://github.com/rajpaynaik/logsite/blob/main/docs/security.md) ·
[Roadmap](https://github.com/rajpaynaik/logsite/blob/main/docs/roadmap.md)

## Status

Early (0.x): feedback and contributors welcome. Questions and ideas go in
[Discussions](https://github.com/rajpaynaik/logsite/discussions); see the
[changelog](https://github.com/rajpaynaik/logsite/blob/main/CHANGELOG.md) for what's new. Formerly **loglens**: `loglens.toml` and
`[tool.loglens]` configs keep working. See [CONTRIBUTING.md](https://github.com/rajpaynaik/logsite/blob/main/CONTRIBUTING.md);
a new log backend is a good first PR.

## License

Apache License 2.0. See [LICENSE](https://github.com/rajpaynaik/logsite/blob/main/LICENSE).
