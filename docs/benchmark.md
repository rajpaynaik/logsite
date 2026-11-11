# Benchmark

Every CI build runs incidents with a known cause through [`logsite investigate`](incidents.md)
and checks the top-ranked cause and how strong the evidence is. The traps matter as much as
the hits: every misdiagnosis found becomes a new case.

## The everyday cases

`tests/test_benchmark.py`

| Case | What happened | logsite's answer |
|---|---|---|
| `db-pool` | Postgres out of connections ("too many clients") behind Spring wrappers, from an API and a Kafka consumer; Hikari's `total=40, active=40, idle=0, waiting=126` warning in the same request; an old commit near the code, no deploy | **capacity**, strong (the commit ranks last) |
| `regression-after-deploy` | read timeouts starting 4 minutes after a deploy whose commit changed the raising line | **change**, strong |
| `dependency-without-a-change` | the same timeouts, no deploy, no commit near the code | **dependency**, moderate |
| `busy-day` | 3× the errors at 3× the traffic | **no incident** |
| `too-little-evidence` | a new error and nothing to tie it to | **unclear** |
| `six-failures` | six unrelated failures in six services within 22 minutes: a deadlock, a carrier timeout, a Kafka message with `"UNKNOWN"` for a number, a null, out of memory in a CSV export, an open circuit breaker | **six incidents**, each with its own cause: concurrency, dependency (`carrier.example.com`), bad data, a bug (`getDiscount()` was null), capacity (memory, while `CsvExporter.export()` buffered), dependency (`inventoryService`) |

## The worst cases

`tests/benchmark_hard.py`

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
| `rare-severe` | 4 "checksum mismatch" corruption errors under 8,000 steady KeyErrors, both steady | the corruption ranks first, marked **severe** |
| `breaker-recovered` | real timeouts for 15 minutes, then only circuit-breaker rejections | "**inventoryService may have recovered**: its circuit breaker is still rejecting calls", with when the real failures stopped |
| `out-of-order` | the cascade delivered in a shuffled order | the same answer, and "**the order is uncertain**": callers logged before the cause |
| `one-tenant` | 95% of a KeyError from tenant `t-17` | "**One tenant: t-17**" first; the summary has shares only, no other tenants' ids |
| `wait-cycle` | order-service and inventory-service time out calling each other in the same requests | "**A wait cycle**: inventory-service waits on order-service, which waits on inventory-service", with a request to follow |
| `orphaned` | checkout gives up after 2s; inventory keeps working on the same requests for 26s more | "**Orphaned work**: inventory-service keeps working after checkout-service gave up"; pass deadlines on |
| `split-brain` | writes rejected for a stale term after node-1 and node-2 both claimed leadership | "**Split-brain**: node-1 and node-2 both acted as leader"; check leases and fencing tokens |
| alert storm | one incident fires 8 alerts in three calls | **one** full report, then one line per alert ("same cause as … · 8 alerts so far") |

Interleaved traces can't be fully separated without a thread id on every line. logsite keeps
them apart where the lines say so, and doesn't invent the rest.
