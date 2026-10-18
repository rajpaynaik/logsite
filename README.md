# logsite

[![PyPI](https://img.shields.io/pypi/v/logsite)](https://pypi.org/project/logsite/)
[![Python](https://img.shields.io/pypi/pyversions/logsite)](https://pypi.org/project/logsite/)
[![VS Code Marketplace](https://img.shields.io/visual-studio-marketplace/v/rajpaynaik.logsite?label=VS%20Code)](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite)
[![CI](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml/badge.svg)](https://github.com/rajpaynaik/logsite/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/rajpaynaik/logsite/blob/main/LICENSE)

**Find the line of code behind every production error, noisy log line and failing test.**

logsite reads your code and the logs you already have (Loki, OpenSearch / Elasticsearch,
CloudWatch or a file), maps every log line back to the code that wrote it, and turns thousands
of stack traces into a few causes, each pointing at *your* line of code. No SDK, no agent in
your services. Python, Java and Go.

## Thousands of stack traces, a few causes

`logsite errors` groups production errors by cause (the exception chain and the frames in
your code). IDs, timestamps and framework frames don't split a group; new and spiking come first.

```
820 errors in the last 24h · 3 group(s) · 3 new · before: 0

  #  TREND  COUNT  ERROR                                         RAISED AT                 LOGGED AT
  1  new      400  IllegalStateException: Shipping label reque…  CarrierApiClient.java:58  —
  2  new      400  Record processing failed topic=shipment-eve…  —                         ShipmentEventListener.java:91
  3  new       20  Consumer lag increasing group=shipment-proc…  —                         ShipmentEventListener.java:96

━━ 1. IllegalStateException: Shipping label request failed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 400 new
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Chain        ListenerExecutionFailedException › IllegalStateException › ResourceAccessException › SocketTimeoutException
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58  requestLabel()
                58 │ return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);
   Call path    ShipmentEventListener.consume › ShipmentProcessor.process › CarrierService.createLabel › CarrierApiClient.requestLabel › java.net
   Example      logsite trace tr-6e6948
```

## The incident, and what changed before it

`logsite investigate` links the groups that belong together and finds the deploy and the
commit behind them:

```
━━ Incident 1 · since 19:22 UTC (60 min) · 820 errors · 3 group(s) ━━━━━━━━━━━━━━━━━━━━━━━━━ new
   Cause        IllegalStateException: Shipping label request failed  (400)
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58
   Then         19:22  Record processing failed topic=shipment-events …  (400)  new  started together
                19:22  Consumer lag increasing group=shipment-processor …  (20)  new  started together
   Deploy       v1.4 at 2026-10-17 19:18 UTC, 4 min before it started
   Suspects     854fbb95 2026-10-17 Maya: Lower carrier read timeout to 3s
```

`logsite errors --since-deploy $TAG --fail-on-new` turns it into a CI gate that stops a bad release.

## Failing tests, for you and your AI assistant

`logsite agent -- pytest` (or `go test`, `mvn test`, `tsc`…) runs the command and prints the
causes instead of hundreds of lines, then what changed after each fix:

```
$ logsite agent -- pytest
pytest: 30 failed, 12 passed · 2 cause(s) · exit 1

1. 27 test(s)  sqlite3.OperationalError: no such column: discount_code
   raised   app/orders/repository.py:10 in get()
2. 3 test(s)  AssertionError: assert total(<n>) == <n>
   raised   tests/test_tax.py:4 in test_tax_0()

$ logsite agent -- pytest          # after fixing the first one
pytest: 3 failed (was 30), 39 passed · 1 cause(s) · exit 1
✓ fixed   sqlite3.OperationalError: no such column: discount_code  (27 test(s))
```

## And more

- **In your editor**: production counts above every log call (`1.2M in 24h`, `▲ 8.0x`), the
  line that raised an error marked with how often it did, and log-quality warnings as you type.
  [VS Code](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite), or any LSP editor with `logsite serve`.
- **Cost per line of code**: which `logger.info(...)` calls make up your log bill. → [Cost and leaks](https://github.com/rajpaynaik/logsite/blob/main/docs/cost-and-leaks.md)
- **Grafana**: a generated dashboard of cost and errors per code line, the error summary in
  your alert notifications (Slack / webhook), and Alloy rules that drop or sample noisy lines
  before they're shipped. → [Grafana, alerts and Alloy](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md)
- **Lint for logging**: lost stack traces, failures logged at `info`, secrets and card numbers
  in log calls, logs in hot loops, missing context like `request_id`. → [Lint rules](https://github.com/rajpaynaik/logsite/blob/main/docs/lint.md)
- **Leaks**: secrets and personal data that actually reached production logs, per log call.
- **Request timeline**: `logsite trace <id>`, every line of one request across services.
- **MCP server**: AI assistants ask "what's failing in production, and why?" and get the
  incidents, groups and code lines. → [MCP server](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md)

## Install

```bash
pip install logsite                # the command-line tool
pip install "logsite[lsp,mcp]"     # + the editor language server and the MCP server
pipx install "logsite[lsp]"        # once for your user, e.g. for Java and Go projects
```

## Quick start

```bash
cd your-service
logsite lint                       # logging problems in the code, no backend needed
logsite agent -- pytest            # failing tests grouped by cause

# point it at your logs: logsite.toml
cat > logsite.toml <<'TOML'
[backend]
type = "loki"
url = "http://localhost:3100"
selector = '{service_name="checkout"}'
TOML

logsite errors                     # production errors grouped by cause
logsite investigate                # ... linked into incidents, with the deploy and commits
logsite cost --price-per-gb 0.5    # what each log call costs per month
```

## Supported

| | |
|---|---|
| **Languages** | Python (`logging`, structlog, loguru), Java (SLF4J, Log4j 2, java.util.logging, Lombok), Go (slog, zap, zerolog, logrus, logr, klog) |
| **Log stores** | Loki, OpenSearch / Elasticsearch, CloudWatch Logs, log files |
| **Tests and builds** | pytest, `go test`, Maven, Gradle, tsc, javac, gcc-style compilers |
| **Editors** | VS Code; Neovim, JetBrains, Helix, Zed through LSP |

## Documentation

[Commands](https://github.com/rajpaynaik/logsite/blob/main/docs/commands.md) ·
[Configuration](https://github.com/rajpaynaik/logsite/blob/main/docs/configuration.md) ·
[Errors, incidents and the CI gate](https://github.com/rajpaynaik/logsite/blob/main/docs/errors.md) ·
[Agent mode](https://github.com/rajpaynaik/logsite/blob/main/docs/agent.md) ·
[Grafana, alerts and Alloy](https://github.com/rajpaynaik/logsite/blob/main/docs/grafana.md) ·
[Languages](https://github.com/rajpaynaik/logsite/blob/main/docs/languages.md) ·
[Lint rules](https://github.com/rajpaynaik/logsite/blob/main/docs/lint.md) ·
[Cost and leaks](https://github.com/rajpaynaik/logsite/blob/main/docs/cost-and-leaks.md) ·
[Request timeline](https://github.com/rajpaynaik/logsite/blob/main/docs/timeline.md) ·
[MCP server](https://github.com/rajpaynaik/logsite/blob/main/docs/mcp.md) ·
[How it works](https://github.com/rajpaynaik/logsite/blob/main/docs/how-it-works.md) ·
[Roadmap](https://github.com/rajpaynaik/logsite/blob/main/docs/roadmap.md)

## Status

Early (0.x): feedback and contributors welcome. Formerly **loglens**: `loglens.toml` and
`[tool.loglens]` configs keep working. See [CONTRIBUTING.md](https://github.com/rajpaynaik/logsite/blob/main/CONTRIBUTING.md);
a new log backend is a good first PR.

## License

MIT
