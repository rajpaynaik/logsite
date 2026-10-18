# Errors, incidents and the CI gate

## Error summary

A bad deploy can write thousands of tracebacks an hour, each a little different: another
order id, another timeout, the code moved down two lines. `logsite errors` groups them by
cause and ranks the groups: new ones first, then the ones that spiked, then the biggest.

```
820 errors in the last 24h · 3 group(s) · 3 new · before: 0

  #  TREND  COUNT  ERROR                                         RAISED AT                 LOGGED AT
  ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  1  new      400  IllegalStateException: Shipping label reque…  CarrierApiClient.java:58  —
  2  new      400  Record processing failed topic=shipment-eve…  —                         ShipmentEventListener.java:91

━━ 1. IllegalStateException: Shipping label request failed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 400 new
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Chain        ListenerExecutionFailedException › IllegalStateException › ResourceAccessException › SocketTimeoutException
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58  requestLabel()
                58 │ return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);
   Call path    ShipmentEventListener.consume › ShipmentProcessor.process › CarrierService.createLabel › CarrierApiClient.requestLabel › java.net
   Seen         2026-10-17 19:22 → 20:21 UTC  (60 min)
   Example      logsite trace tr-6e6948

━━ 2. Record processing failed topic=shipment-events partition=<n> offset=<n> ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 400 new
   Logged at    src/main/java/com/example/shipping/ShipmentEventListener.java:91
                91 │ log.error("Record processing failed topic={} partition={} offset={}", record.topic(), record.partition(), rec…
   Seen         2026-10-17 19:22 → 20:22 UTC  (60 min)
```

An overview table first (one row per group), then a card per group with the same fields in
the same order: the **root cause** and the library it came from, the exception **chain**
(framework wrappers like Spring Kafka's `ListenerExecutionFailedException` are noted, not
headlined), where it was **raised** in your code with the line itself, where it was
**logged**, the **call path**, when it was **seen** and an **example** trace id. In a terminal,
new groups are red, spikes yellow and your code highlighted; `--brief` prints only the table,
`--traceback` adds a sample trace with your frames marked `▶` and library frames dimmed.

- **One group per cause**: the exception types in the chain plus the frames in *your*
  code. Line numbers, library frames and the values in the message don't split a group;
  values that differ show as `<n>`, `<str>`, `<id>`.
- **Raised** is the deepest frame of your code in the root cause (and the library it came
  from); **logged** is the log call that wrote it; **path** is how the request got there.
- **Errors without a traceback** group by the log call that wrote them, or by their message
  with values masked.
- `--since-deploy v1.4.0` summarizes from the deploy until now, compared with an equally
  long window before it. `--traceback` prints a sample traceback per group, `--format json`
  everything. Samples are redacted like `logsite leaks` reports.

Tracebacks are read from python-json-logger's `exc_info`, ECS `error.stack_trace`,
OpenTelemetry `exception.stacktrace`, loguru's `serialize=True` output, structlog's
`dict_tracebacks`, error type/message fields, and plain-text
`Traceback (most recent call last):` blocks (the file backend joins multi-line ones). Each
backend asks for errors only (a LogQL line filter, an OpenSearch query, a CloudWatch
filter pattern), so `--limit` isn't used up by `info` lines.

In the editor, the line that raised an error gets a code lens
(`TimeoutError 3k (new) in 24h`) and a hover with the groups; the log call that logged it
lists them too. The MCP tool `error_summary` gives an AI assistant the same groups, with
trace ids to pass to `request_timeline`.

## Incidents

During an outage, several error groups are one story. `logsite investigate` links them and
says what changed just before:

```
1 incident(s) in the last 24h · 820 errors in 3 group(s)

━━ Incident 1 · since 19:22 UTC (60 min) · 820 errors · 3 group(s) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ new
   Cause        IllegalStateException: Shipping label request failed  (400)
   Root cause   SocketTimeoutException: Read timed out  (java.net)
   Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:58
                58 │ return restTemplate.postForObject(carrierUrl + "/labels", request, LabelResponse.class);
   Then         19:22  Record processing failed topic=shipment-events partition=<n> offset=<…  (400)  new  started together
                19:22  Consumer lag increasing group=shipment-processor lag=<n>  (20)  new  started together
   Deploy       v1.4 at 2026-10-17 19:18 UTC, 4 min before it started
   Suspects     854fbb95 2026-10-17 Maya: Lower carrier read timeout to 3s
   Example      logsite trace tr-6e6948
```

- **Linked** when the same requests ran into both groups (a shared trace id), or when they
  started within `--link-minutes` (10) of each other and both are new or spiking. Steady
  background errors stay apart, listed as "ongoing".
- **Cause**: among the groups that changed, the earliest with an exception: its root cause and
  the line of your code that raised it. **Then**: the other groups, in the order they started.
- **Deploy**: the git tag released in the 24 hours before it started. **Suspects**: commits
  that changed the code around the line that raised it, in that release (or the week before).
- `--format json` and the MCP tool `investigate` give an AI assistant the whole incident in one
  call, with an example trace id for `request_timeline`.

### CI gate

```bash
logsite errors --since-deploy "$TAG" --fail-on-new     # exit 1 if the release brought a new error
logsite errors --hours 1 --fail-on-spike               # or one that spiked
```

Run it a few minutes after a deploy to stop a rollout or roll back: it prints
`logsite: FAIL: 3 new error group(s) since v1.4 (…)` and exits 1.
