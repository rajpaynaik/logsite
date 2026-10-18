# Lint rules

| Code  | Rule |
|-------|------|
| LL001 | Eager formatting (f-string, `.format()`, `%`, `+`) in a log call |
| LL002 | Possible PII / secret logged (`password`, `token`, `email`, `card_number`, …) |
| LL003 | `logger.error` inside `except` without `exc_info`, so the traceback is lost |
| LL004 | `info`/`debug` log inside a loop |
| LL005 | Message built at runtime, so it can't be traced back to production |
| LL006 | Missing required context: fields for every log, errors, retry loops and dependency calls (`[context]`), opt-in; context added automatically is credited |
| LL007 | Failure logged below `warning` ("failed", "timeout", "could not" at `info`/`debug`), or an error swallowed by a broad `except` and logged at `info` |
| LL008 | Values formatted into the message instead of `extra=` fields; editor quick fix rewrites the call. **Opt-in** (`extend_select = ["LL008"]`) |
| LL100 | Noisy line: emits more than N times per window *(needs a backend)* |
| LL101 | Warning/error line never seen in production *(needs a backend)* |
| LL102 | Line spiked: ≥ `spike_ratio`× the previous window and ≥ `spike_min_count` emits *(needs a backend)* |
| LL104 | Level doesn't match production: `info` lines whose real messages look like failures, or `error` lines firing constantly *(needs a backend)* |
| LL105 | Required context missing from production records: e.g. `request_id` in only 71% of a line's real records *(needs a backend)* |

## Context rules

`database timeout`, with no query, duration, retry count or request ID, can't be acted on.
LL006 requires context fields by situation, which logsite works out from the code:

| Situation | When | Typical fields |
|-----------|------|----------------|
| `all` | every log call | `request_id`, `trace_id` |
| `error` | `error` / `critical` / `exception` | `tenant_id`, `user_id` |
| `in_retry` | a loop with `try`/`except` that sleeps or counts attempts, or a `@retry` / `@backoff` function | `attempt` |
| `near_dependency` | an `except` around a call to `requests`, `httpx`, `boto3`, `cursor.execute` or a `*client` / `*session` / `*gateway` | `dependency`, `status_code` |

The right way to add common context is once, for every log, not on each call. logsite
credits fields that the project adds automatically: a logging filter or record factory
that sets `record.request_id`, structlog `bind_contextvars(...)` or processors,
`LoggerAdapter(logger, {...})` extras, and OpenTelemetry's logging instrumentation (which
satisfies `trace_id` / `span_id`). Common spellings count as the same field
(`requestId`, `correlation_id`). `logsite context` shows the rules, what was found and where,
and how many log calls are missing what.

Code can look right and still lose context: a filter that only some handlers have, a
thread or async task that doesn't copy the context, a background job with no request.
`logsite context --production` checks what real records carry:

```
In production (last 24h, 115 sampled record(s)):
  tenant_id           33% of 30 record(s)
  request_id          52% of 115 record(s) (added automatically)

Log calls below 95% (with >= 20 records):
     0%  request_id      app/orders.py:14  (0/30)
    33%  tenant_id       app/orders.py:14  (10/30)
    71%  request_id      app/orders.py:7  (60/85)
```

Fields count wherever they appear in a record, nested (`ctx.tenant_id`) or under a common
synonym. Placeholder values (`""`, `"-"`, `null`) count as missing. LL105 reports the same
gaps in `lint --volume`.

## Structured messages

`logger.error("payment failed for order %s: %s", order.id, err)` produces a different
message for every order. You can't count or alert on it without a regex, and `order_id`
isn't a field you can filter on. The structured version keeps the message constant:

```python
logger.error("payment failed for order", extra={"order_id": order.id, "err": err})
```

LL008 flags values formatted into a message (`%` args, f-strings, `.format()`). It's
opt-in, because `%`-style messages are idiomatic Python logging. Turn it on with
`extend_select = ["LL008"]` once your team has chosen structured logging. In the editor, the
quick fix does the rewrite: it names fields after the expressions (`order.id` → `order_id`),
merges into an existing `extra={...}`, and avoids `LogRecord` attribute names (`msg`,
`name`, ...) that would raise at runtime. It only offers the fix when the rewrite is safe.

`logsite structure` scores the codebase by top-level directory, and with `--production`
by real volume, listing the loudest unstructured lines (the ones most worth converting).

## Severity checks

A payment failure logged at `info` never reaches an alert that watches `warning`/`error`.
LL007 catches it in the code: a failure word in the message at `info`/`debug`, or an error
swallowed by `except Exception:` (or a bare `except:`) and logged at `info`. Narrow handlers
(`except KeyError:`) and handlers that re-raise are fine.

Some lines only look like failures once rendered: `logger.info("settlement %s: %s", id,
status)` writes `settlement 44: failed (card declined)`. With `lint --volume`, LL104 checks a
sample of each line's real messages, and also flags `error` lines that fire constantly,
since alert fatigue is how real errors end up downgraded to `info`. LL104 reports only the
matched word, never the message, because messages can hold personal data.
