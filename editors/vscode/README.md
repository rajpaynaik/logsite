# logsite for VS Code

**See how often each log line fires in production, what it costs, and catch bad logging, right next to the `logger.info(...)` call.**

```python
logger.info("charging order %s for %d cents", order.id, total)   # 📈 1.2M in 24h · $42/mo
logger.debug(f"card {card_number} charged")                      # ⚠ LL001 eager f-string · LL002 PII
logger.warning("legacy discount path used for %s", order.id)     # no logs in 24h  ← dead code?
```

logsite maps production logs back to the exact line that wrote them, for **Python**
(`logging`, structlog, loguru), **Java** (SLF4J, Log4j 2, java.util.logging, Lombok) and **Go**
(slog, zap, zerolog, logrus, logr, klog), reading from **Loki**, **OpenSearch /
Elasticsearch**, **CloudWatch Logs** or a log file.

> **Preview.** JavaScript/TypeScript is
> [planned](https://github.com/rajpaynaik/logsite#other-languages).

## Features

- **Production counts above every log call**, with a `▲ 8.0x` marker when a line spiked.
- **Cost per line**: `$/mo` next to the count once you set your price per GB.
- **Hover** for the trend, the cost, and the last commits that changed the code around the call.
- **Production errors where they're raised**: the line that raised an exception gets
  `TimeoutError 3k (new) in 24h`, and the hover lists each error group with the log call that
  logged it.
- **Log-quality lint as you type**: eager formatting, secrets and personal data, lost
  tracebacks, failures logged at `info`, logs in hot loops, missing context like `request_id`.
- **Quick fix** that turns `"order %s failed", order.id` into a constant message with
  `extra={"order_id": order.id}` (or keyword arguments for structlog / loguru).

The terminal and CI get more: `logsite errors` (production errors grouped by cause),
`logsite trace <trace_id>` (one request across services),
`logsite leaks`, `logsite cost`, `logsite spikes --since-deploy v1.4`, and an MCP server for AI
assistants. See the [project README](https://github.com/rajpaynaik/logsite#readme).

## Setup

1. **Install the server** ([logsite on PyPI](https://pypi.org/project/logsite/)) in your project's environment:

   ```bash
   pip install "logsite[lsp]"
   ```

   The extension uses the Python interpreter you've selected in VS Code
   (`python -m logsite serve`), then falls back to `logsite` on `PATH`. To use something else,
   set `logsite.serverCommand`. For Java and Go projects, which usually have no Python
   environment, install it once for your user: `pipx install "logsite[lsp]"`.

2. **Point it at your logs** with a `logsite.toml` at the project root (lint works without one).
   In a monorepo, give each service its own; every file uses the nearest one:

   ```toml
   [backend]
   type = "loki"                  # or "opensearch", "elasticsearch", "cloudwatch", "file"
   url = "http://localhost:3100"
   selector = '{app="checkout"}'
   aggregate = true               # let Loki count per file/line: exact at any volume

   [cost]
   price_per_gb = 0.50            # optional: show $/mo
   ```

3. **Add file and line to your log records** for exact matching, e.g. python-json-logger's
   `%(pathname)s %(lineno)d`, structlog's `CallsiteParameterAdder`, or loguru's
   `serialize=True`. Without them, logsite matches by message template.

## Commands

| Command | |
|---|---|
| `logsite: Restart server` | Restart after installing or changing the config |
| `logsite: Show output` | The server log, for troubleshooting |

## Settings

| Setting | Default | |
|---|---|---|
| `logsite.serverCommand` | `[]` | Command that starts the server. Empty: the selected Python interpreter, then `logsite` on `PATH` |

## Develop

```bash
npm install && npm run compile
# press F5 in VS Code with this folder open to launch an Extension Development Host
```
