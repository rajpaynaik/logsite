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

### Rates, not counts

Twice the errors at twice the traffic isn't a regression. logsite counts every log event of
the service in both windows (server-side on Loki and OpenSearch; files too) and measures
spikes against the window before **scaled to today's traffic**, so the CI gate, incidents
and alerts don't fire on a busy day:

```
60 errors in the last 24h · 1 group(s) · before: 20, 3.0x · traffic 3.0x
   Trend        3.0x errors at 3.0x traffic: the rate is 1.0x, it grew with traffic
```

`--format json` has `traffic_ratio` and `rate_ratio` per group. Traffic is the service's
non-error log volume: a stand-in for requests, not a request count. CloudWatch and windows
with fewer than 100 events fall back to counts.

### Incident report: ranked causes with the evidence

`logsite investigate --report` (or `-o report.md`) writes an incident report that someone
can check: what happened, then the explanations ranked by evidence, each with what
supports it, what speaks against it, what logsite can't see, and the cheapest next check.
Some errors already say what kind of failure they are, and get their own explanation and
next check: a **deadlock**, an open **circuit breaker** (the dependency behind it is the
cause), a **bug** (a null, an index out of range, at the line of yours that raised it) and
**bad input data** (a value a message or request had that the code can't read, with where it
came from). Otherwise the candidates are a **change** (the release and the commits around the code that raised),
**traffic** (same error rate, more requests), a **dependency** (timeouts, refused
connections: a call that failed elsewhere) and **capacity** (a connection pool, a database's
connection limit, memory or threads running out). It's deterministic: no model writes it.

Besides the errors, logsite reads a few of the failing requests in full (by trace id) for
evidence the error lines don't carry: **warnings in the same requests** (a pool-exhausted
warning with its numbers) and **impact** (the line that closed the request:
`POST /api/v1/orders → 500 after 30,042 ms`). When several groups share **one root cause**
reached from different entry points (an API and a Kafka consumer), it says so.

A commit near the failing line only counts as strong evidence when a deploy ties it to when
the errors started; without one it's a weak lead, and "the error is new" only counts when
there were logs before to compare with.

```markdown
### Likely causes (strong evidence)

**1. The change in v1.4: 0df4dac1 Lower carrier read timeout to 3s** (score 9)

- For: v1.4 was deployed 4 min before the errors started
- For: 0df4dac1 changed the lines around src/main/java/com/example/shipping/CarrierApiClient.java:58, where it's raised
- For: the error wasn't there in the window before
- For: it was fixed before (41bdce40) and came back: the change may undo that fix
- **Next check:** read `git show 0df4dac1 -- src/main/java/com/example/shipping/CarrierApiClient.java`; revert it (or roll back) in staging and see whether the error stops

**2. A dependency failing: SocketTimeoutException from the service requestLabel() calls** (score 2)

- For: the root cause is SocketTimeoutException: a call to another service or database failed
- For: it was thrown inside java.net, not by your code
- Against: but a recent commit changed the code that makes the call
- Unknown: logsite reads logs only: the latency and status of the service requestLabel() calls aren't in them
- **Next check:** check the latency and error rate of the service requestLabel() calls since 02:52 UTC (its dashboard or status page); if it's slow for every caller, the change isn't the cause
```

Scores only rank the explanations; they aren't probabilities. When nothing stands out, the
report says the evidence is unclear rather than picking one, and marks weak explanations
"unlikely". The terminal card shows the lead (`Likely`, with how strong the evidence is),
and `investigate --format json` and the MCP `investigate` tool include the whole
`diagnosis`.

### Benchmark

`tests/test_benchmark.py` runs incidents with a known cause through `logsite investigate`
on every CI build and checks the top-ranked cause and how strong the evidence is:

| Case | What happened | logsite's answer |
|---|---|---|
| `db-pool` | Postgres out of connections ("too many clients") behind Spring wrappers, from an API and a Kafka consumer; Hikari's `total=40, active=40, idle=0, waiting=126` warning in the same request; an old commit near the code, no deploy | **capacity**, strong (the commit ranks last) |
| `regression-after-deploy` | read timeouts starting 4 minutes after a deploy whose commit changed the raising line | **change**, strong |
| `dependency-without-a-change` | the same timeouts, no deploy, no commit near the code | **dependency**, moderate |
| `busy-day` | 3× the errors at 3× the traffic | **no incident** |
| `too-little-evidence` | a new error and nothing to tie it to | **unclear** |
| `six-failures` | six unrelated failures in six services within 22 minutes: a deadlock, a carrier timeout, a Kafka message with `"UNKNOWN"` for a number, a null, out of memory in a CSV export, an open circuit breaker | **six incidents**, each with its own cause: concurrency, dependency (`carrier.example.com`), bad data, a bug (`getDiscount()` was null), capacity (memory, while `CsvExporter.export()` buffered), dependency (`inventoryService`) |

And twelve worst cases (`tests/benchmark_hard.py`):

| Case | What makes it hard | logsite's answer |
|---|---|---|
| `cascade` | inventory's database times out; checkout (Python) times out calling it *first*, the gateway (Go) returns 502s; trace ids in three formats | one incident; **inventory** is the cause (checkout's error names it), checkout and gateway are effects; the slow-query warning is evidence |
| `red-herring` | a deploy 3 minutes before, whose commit changed the failing call, but the same DNS failure hits two services that weren't deployed | **dependency** (`catalog-db.internal` doesn't resolve) over the change |
| `storm` | 20,000 copies of a known error and 3 of a new one, with a 5,000-record limit | the new one first; counts flagged as a lower bound |
| `py-chain` | `raise ... from exc`: OrderLookupError over `database is locked` | root cause and raised-at from the original error |
| `go-fatal` | `fatal error: concurrent map writes`: no timestamp, no level | found, new, raised at `Cache.Set()`; **concurrency** (a data race) |
| `truncated` | `... 87 more`, `Suppressed:`, a frame cut by the collector | one group; root cause `RejectedExecutionException`; **capacity** (threads) |
| `retries` | four WARN retries with the trace, then the ERROR | at most two groups; **dependency** (connection refused) |
| `config` | an expired TLS certificate; a rotated database password | **config**, with the certificate check and the user that was rejected |
| `no-trace-capacity` | `no space left on device` and `too many open files` only in an `error` field | two groups; **capacity** (disk space, file descriptors) |
| `node` | an unhandled promise rejection with a Node.js stack | `Error: connect ECONNREFUSED`, raised at `src/cache/redis.js:44`; **dependency** |
| `logstash` | Logstash JSON with `stack_trace`, `app` and a nested cause | the root cause and the service from the fields |
| `interleaved` | two threads' traces mixed line by line | no exception merged into another or put on the wrong code |

Interleaved traces can't be fully separated without a thread id on every line; logsite keeps
them apart where the lines say so, and doesn't invent the rest.

The traps matter as much as the hits. Every misdiagnosis found becomes a new case.

### CI gate

```bash
logsite errors --since-deploy "$TAG" --fail-on-new     # exit 1 if the release brought a new error
logsite errors --hours 1 --fail-on-spike               # or one that spiked
```

Run it a few minutes after a deploy to stop a rollout or roll back: it prints
`logsite: FAIL: 3 new error group(s) since v1.4 (…)` and exits 1.
