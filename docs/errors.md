# Errors

A bad deploy can write thousands of tracebacks an hour, each a little different: another
order id, another timeout, the code moved down two lines. `logsite errors` turns them into a
few causes, each with the line of your code behind it.

```bash
logsite errors                      # the last 24 hours
logsite errors --from incident.log  # a log file
logsite errors -i                   # browse them: search, any time range, open one
```

```
820 errors in the last 24h · 3 group(s) · 3 new · before: 0

  #  TREND  COUNT  ERROR                                         RAISED AT                 LOGGED AT
  1  new      400  IllegalStateException: Shipping label reque…  CarrierApiClient.java:58  —
  2  new      400  Record processing failed topic=shipment-eve…  —                         ShipmentEventListener.java:91

━━ 1. IllegalStateException: Shipping label request failed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 400 new
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Chain        ListenerExecutionFailedException › IllegalStateException › ResourceAccessException › SocketTimeoutException
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58  requestLabel()
                58 │ return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);
   Call path    ShipmentEventListener.consume › ShipmentProcessor.process › CarrierService.createLabel › CarrierApiClient.requestLabel › java.net
   Seen         2026-10-17 19:22 → 20:21 UTC  (60 min)
   Example      logsite trace tr-6e6948
```

New groups come first, then the ones that spiked, then the biggest.

## Reading a group

| | What it tells you |
|---|---|
| **Root cause** | The first exception in the chain, and the library it came from |
| **Chain** | Every exception wrapped around it. Framework wrappers, like Spring Kafka's `ListenerExecutionFailedException`, are noted but never the headline |
| **Raised at** | The deepest line of *your* code in the root cause, with the code itself |
| **Logged at** | The log call that wrote the error |
| **Call path** | How the request got there |
| **Seen** | First and last time in the window |
| **Example** | A trace id: `logsite trace` shows every line of that request |

In a terminal, new groups are red, spikes yellow and your code highlighted.

## What makes a group

- **One group per cause:** the exception types in the chain, plus the frames in your code.
  Line numbers, library frames and values in the message don't split a group. Values that
  differ show as `<n>`, `<str>` or `<id>`.
- **Errors without a stack trace** group by the log call that wrote them, or by their
  message with the values masked.

## Options

| Option | Does |
|---|---|
| `--since-deploy v1.4.0` | From that deploy until now, against an equally long window before it |
| `--brief` | Only the table |
| `--traceback` | A sample trace per group: your frames marked `▶`, library frames dimmed |
| `--format json` | Everything, for scripts |

Samples are redacted the way [`logsite leaks`](cost-and-leaks.md#leak-scanning) reports.

## Where stack traces come from

python-json-logger's `exc_info`, ECS `error.stack_trace`, OpenTelemetry
`exception.stacktrace`, loguru's `serialize=True`, structlog's `dict_tracebacks`, error type
and message fields, and plain-text `Traceback (most recent call last):` blocks. A log file's
multi-line traces are joined back together.

Each log store is asked for errors only (a LogQL line filter, an OpenSearch query, a
CloudWatch filter pattern), so `--limit` isn't used up by `info` lines.

### Stack traces split into lines (Loki)

A stack trace is many lines. If your log shipper sends each line as its own Loki entry, the
`ERROR` line arrives without its exception, and pieces like `Caused by: …` look like errors
of their own.

**logsite joins them back.** When the errors it reads include pieces of a trace, it reads
the same stream from 1 second before to 5 seconds after each error and joins each trace into
one event: one extra query per burst of errors, up to 20. Beyond that, `logsite errors` says
so:

```
note: 20 record(s) are single lines of a stack trace, so the log shipper sends each line
separately and errors lose their exception chain. Join them before shipping: logsite
pipeline --multiline (docs/errors.md)
```

**Better: join them where they're shipped**, so Grafana's views and alerts see whole events
too:

```bash
logsite pipeline --multiline -o logsite.alloy
```

This adds a `stage.multiline` block to your Alloy rules. A line that starts with a timestamp,
a JSON object, `level=` or `time=`, a klog header or a Go panic starts a record; every other
line (`at …`, `Caused by: …`, `... 12 more`) belongs to the one above, up to 500 lines.
Reading `/var/log/pods` directly? Put `stage.cri {}` before it. For a different first line,
set `multiline_firstline` in `[pipeline]` (RE2).

Tested with Alloy and Loki on 20 Spring errors with 8-line traces, shipped line by line:
without joining, 3 groups and no root cause. Joined either way: 1 group, `PSQLException` as
the root cause, raised at `StockRepository.java:54`.

## Elsewhere

- **In the editor:** the line that raised an error shows `TimeoutError 3k (new) in 24h`
  above it, with the groups in its hover. See [VS Code](vscode.md).
- **For AI assistants:** the MCP tool `error_summary` returns the same groups, with trace
  ids for `request_timeline`. See [MCP](mcp.md).
- **During an outage:** [`logsite investigate`](incidents.md) links the groups into
  incidents, with the deploy and commit behind them.

## In CI

Stop a rollout when a release brings a new error:

```bash
logsite errors --since-deploy "$TAG" --fail-on-new     # exit 1 on a new error group
logsite errors --hours 1 --fail-on-spike               # or one that spiked
```

It prints `logsite: FAIL: 3 new error group(s) since v1.4 (…)` and exits 1. Run it a few
minutes after a deploy. For a fuller picture of a release, see
[Release health](releases.md).
