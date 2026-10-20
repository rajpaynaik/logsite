# logsite for VS Code

**Production errors and log volume, right in your code: which line raised the error, how
often, since when, and which log calls make up your bill.**

```java
return restTemplate.postForObject(url + "/labels", req, LabelResponse.class);  // ⛔ IllegalStateException 400 (new) in 24h
log.info("label created shipment={}", shipment.getId());                        // 📈 1.2M in 24h ▲ 8.0x · $42/mo
log.warn("legacy rate table used for {}", carrier);                             // no logs in 24h  ← dead code?
```

logsite reads the logs you already have (**Loki**, **OpenSearch / Elasticsearch**,
**CloudWatch Logs** or a log file), groups thousands of stack traces into a few causes, and maps
each one back to the line in *your* code. No SDK, no agent in your services. **Python**
(`logging`, structlog, loguru), **Java** (SLF4J, Log4j 2, java.util.logging, Lombok) and **Go**
(slog, zap, zerolog, logrus, logr, klog).

## Production errors where they're raised

logsite groups production errors by cause: the exception chain and the frames in your code.
IDs, timestamps and framework frames don't split a group, so 820 stack traces become 3 causes.

- **Above the line that raised it**: `⛔ IllegalStateException 400 (new) in 24h`, or
  `(3.1x)` when it's spiking.
- **Hover** for every error group raised there: the full message, the count, new or spiking,
  and the log call that logged it. Hover a log call to see the errors it logged and where they
  were raised.
- The group ID in the hover (`[a1b2c3]`) is the one the terminal, Grafana and the MCP server use.

## The full summary: what failed and why

The editor marks the lines; the terminal gives the summary. In the same project:

**`logsite errors`**: every error group, new and spiking first, with its root cause, where it
was raised and the call path to it.

```
820 errors in the last 24h · 3 group(s) · 3 new · before: 0

  #  TREND  COUNT  ERROR                                         RAISED AT                 LOGGED AT
  1  new      400  IllegalStateException: Shipping label reque…  CarrierApiClient.java:58  —
  2  new      400  Record processing failed topic=shipment-eve…  —                         ShipmentEventListener.java:91
  3  new       20  Consumer lag increasing group=shipment-proc…  —                         ShipmentEventListener.java:96

━━ 1. IllegalStateException: Shipping label request failed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 400 new
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Chain        ListenerExecutionFailedException › IllegalStateException › ResourceAccessException › SocketTimeoutException
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58  requestLabel()
                58 │ return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);
   Call path    ShipmentEventListener.consume › ShipmentProcessor.process › CarrierService.createLabel › CarrierApiClient.requestLabel › java.net
   Example      logsite trace tr-6e6948
```

**`logsite investigate`**: links the groups that belong to one incident, puts the cause first,
and finds the deploy and the commit behind it.

```
━━ Incident 1 · since 19:22 UTC (60 min) · 820 errors · 3 group(s) ━━━━━━━━━━━━━━━━━━━━━ new
   Cause        IllegalStateException: Shipping label request failed  (400)
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58
   Then         19:22  Record processing failed topic=shipment-events …  (400)  new  started together
                19:22  Consumer lag increasing group=shipment-processor …  (20)  new  started together
   Deploy       v1.4 at 2026-10-17 19:18 UTC, 4 min before it started
   Suspects     854fbb95 2026-10-17 Maya: Lower carrier read timeout to 3s
   History      ↩ came back · seen 3 Aug 2026 → 3 Sep 2026, gone after v1.3
   Fixed by     41bdce40 2026-09-03 Maya: Retry carrier timeouts with backoff
```

**`logsite investigate --report -o incident.md`**: an evidence report for the ticket or the
Slack thread. It ranks the explanations (the change, more traffic, a failing dependency, a pool
or limit running out, a deadlock, bad config, bad data…) with the evidence for and against each,
the warnings and status codes from the same failing requests, and the next check to tell them
apart.

**`logsite about <file>:<line>`**: what one line does in production (errors raised there, how
often it logs, whether an error was fixed there before) before you change it.

**`logsite trace <trace_id>`**: every log line of one request, across services, in order.

The same summaries go to Slack with every Grafana alert, and to AI assistants through the
MCP server and the Claude Code plugin. See the
[project README](https://github.com/rajpaynaik/logsite#readme).

## Log volume and cost per line

- **Production counts above every log call**: `1.2M in 24h`, with `▲ 8.0x` when a line spiked
  and `no logs in 24h` when it never runs.
- **Cost per line**: `$/mo` next to the count once you set your price per GB.
- **Hover** for the trend, the cost, and the last commits that changed the code around the call.

## Log-quality lint as you type

Eager formatting, secrets and personal data in log calls, lost stack traces, failures logged at
`info`, logs in hot loops, missing context like `request_id`. A **quick fix** turns
`"order %s failed", order.id` into a constant message with `extra={"order_id": order.id}` (or
keyword arguments for structlog / loguru). Lint works without any log backend.

## Setup

1. **Install the server** ([logsite on PyPI](https://pypi.org/project/logsite/)) in your
   project's environment:

   ```bash
   pip install "logsite[lsp]"
   ```

   The extension uses the Python interpreter you've selected in VS Code
   (`python -m logsite serve`), then falls back to `logsite` on `PATH`. To use something else,
   set `logsite.serverCommand`. For Java and Go projects, which usually have no Python
   environment, install it once for your user: `pipx install "logsite[lsp]"`.

2. **Point it at your logs** with a `logsite.toml` at the project root. In a monorepo, give
   each service its own; every file uses the nearest one:

   ```toml
   [backend]
   type = "loki"                  # or "opensearch", "elasticsearch", "cloudwatch", "file"
   url = "http://localhost:3100"
   selector = '{app="shipping"}'
   aggregate = true               # let Loki count per file/line: exact at any volume

   [cost]
   price_per_gb = 0.50            # optional: show $/mo
   ```

   No log server handy? `type = "file"` with `path = "app.log"` works on a downloaded log.

3. **Add file and line to your log records** for exact matching, e.g. python-json-logger's
   `%(pathname)s %(lineno)d`, structlog's `CallsiteParameterAdder`, or loguru's
   `serialize=True`. Without them, logsite matches by message template. Error groups come from
   the stack traces, so they work either way.

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
