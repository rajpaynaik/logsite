# Latency from logs

`logsite slow` shows which log calls and endpoints got slower, from the timings your logs
already carry: no tracing SDK, no agent. `logsite watch` reads them once an hour and stores
p50, p95 and p99 per log call and endpoint in the [error store](watch.md#what-it-stores).

```
$ logsite slow
Slower in the last 24h, against the 24h before

  #  P95           P99            CALLS  WHERE             LOG CALL
  ─────────────────────────────────────────────────────────────────────
  1  180ms → 1.4s  310ms → 3.2s  12,000  POST /checkout    api.py:88
  2  45ms → 210ms  90ms → 640ms   3,600  GET /orders/{id}  orders.py:41

Released in this window: v1.4.0 (Oct 29 14:02)
```

[`logsite release`](releases.md) shows the same for a release, as a `p95 latency` row, and
calls a release **degraded** when its p95 went up `ratio` times.

## What it reads

A timing from a field first, then the message:

| Where | Examples |
|---|---|
| A field with its unit in the name | `duration_ms`, `elapsed_ms`, `latency_ms`, `took_ms`, `duration_us`, `duration_seconds` |
| A field with its unit in the value | `"took": "1.2s"`, `latency=350ms`, Go's `"duration": "2m3.5s"` |
| Known fields | nginx's `request_time`, pino-http's `responseTime`, OpenTelemetry's `http.server.request.duration` |
| The message | `took 3.5s`, `completed in 120ms`, `(352ms)` at the end of an access log line |

A bare number in a field without a unit (`"duration": 3`) is a guess: from a million up it's
nanoseconds (Go's slog), with a fraction under 1,000 it's seconds (zap), otherwise
milliseconds. Name the unit in the field to be sure.

**The endpoint** comes from `http.route`, `route`, `endpoint`, `url.path`, `path`, `uri` or
`url`, with the method from `method` or `http.method`, or from an access log line
(`"GET /orders/7 HTTP/1.1"`). Ids in it become `{id}`: `/orders/7` and `/orders/8` are one
endpoint, `GET /orders/{id}`.

A timing whose line no log call in your code matches (nginx, a proxy) is kept under its
endpoint alone.

## How it compares

The last `window_hours` (24) in whole hours, against as long before. A row is **slower**
when its p95 went up `ratio` (2) times, with at least `min_calls` (100) timed calls in both
windows, so a handful of slow requests don't count.

Percentiles over several hours are each hour's, weighted by its calls: close, not exact.
On Loki only the lines that may carry a timing are read; on other log stores a busy hour is
a sample of up to `--limit` lines.

## Configuration

```toml
[latency]
window_hours = 24          # this long, against as long before
ratio = 2.0                # slower: p95 this many times the window before
min_calls = 100            # ... over at least this many timed calls in each
```

```bash
logsite slow --hours 6           # the last 6 hours against the 6 before
logsite slow --format json
```

## Ask Claude

The [MCP server](mcp.md) has a `slow_calls` tool: "what got slower since yesterday?"
