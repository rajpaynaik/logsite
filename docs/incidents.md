# Incidents

During an outage, several error groups are one story. `logsite investigate` links them, finds
the one that started it, and says what changed just before.

```bash
logsite investigate               # the last 24 hours
logsite investigate --report      # an incident report with the evidence (or -o report.md)
```

```
1 incident(s) in the last 24h · 820 errors in 3 group(s)

━━ Incident 1 · since 19:22 UTC (60 min) · 820 errors · 3 group(s) ━━━━━━━━━━━━━━━━━━━━━━━━━━ new
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

## How it decides

| | |
|---|---|
| **Linked** | The same requests ran into both groups (a shared trace id), or they started within `--link-minutes` (10) of each other and both are new or spiking. Steady background errors stay apart, as "ongoing" |
| **Cause** | Among the groups that changed, the earliest with an exception: its root cause and the line of your code that raised it |
| **Then** | The other groups, in the order they started |
| **Deploy** | The git tag released in the 24 hours before it started |
| **Suspects** | Commits in that release (or the week before) that changed the code around the line that raised it |

`--format json` and the MCP tool `investigate` give an AI assistant the whole incident in one
call.

## Rates, not counts

Twice the errors at twice the traffic isn't a regression. logsite counts all of the service's
log events in both windows, and measures spikes against the window before **scaled to
today's traffic**, so a busy day doesn't page anyone:

```
60 errors in the last 24h · 1 group(s) · before: 20, 3.0x · traffic 3.0x
   Trend        3.0x errors at 3.0x traffic: the rate is 1.0x, it grew with traffic
```

Traffic is the service's non-error log volume, a stand-in for requests rather than a request
count. CloudWatch, and windows with fewer than 100 events, fall back to counts.

## The incident report

`logsite investigate --report` writes a report someone can check: what happened, then the
possible explanations ranked by evidence. Each comes with what supports it, what speaks
against it, what logsite can't see, and the cheapest next check. No AI model writes it: the
same logs always give the same report.

```markdown
### Likely causes (strong evidence)

**1. The change in v1.4: 0df4dac1 Lower carrier read timeout to 3s** (score 9)

- For: v1.4 was deployed 4 min before the errors started
- For: 0df4dac1 changed the lines around src/main/java/com/example/shipping/CarrierApiClient.java:58, where it's raised
- For: the error wasn't there in the window before
- **Next check:** read `git show 0df4dac1 -- src/main/java/com/example/shipping/CarrierApiClient.java`; revert it (or roll back) in staging and see whether the error stops

**2. A dependency failing: SocketTimeoutException from the service requestLabel() calls** (score 2)

- For: the root cause is SocketTimeoutException: a call to another service or database failed
- Against: but a recent commit changed the code that makes the call
- Unknown: logsite reads logs only: the latency and status of the service requestLabel() calls aren't in them
- **Next check:** check the latency and error rate of that service since 02:52 UTC; if it's slow for every caller, the change isn't the cause
```

**The explanations it weighs:**
- a **change**: the release, and the commits around the code that raised;
- **traffic**: the same error rate, with more requests;
- a **dependency**: timeouts or refused connections, a call that failed elsewhere;
- **capacity**: a connection pool, a database's connection limit, memory or threads
  running out.

Some errors say what kind of failure they are, and get their own explanation: a
**deadlock**, an open **circuit breaker**, a **bug** (a null, an index out of range) and
**bad input data**.

**What else it reads.** It reads a few failing requests in full, by trace id, for evidence
the error lines don't carry. That includes warnings in the same requests (a pool-exhausted
warning with its numbers), and the line that closed the request
(`POST /api/v1/orders → 500 after 30,042 ms`).

**How it stays honest:**
- Scores rank the explanations; they aren't probabilities. When nothing stands out, the
  report says the evidence is unclear rather than picking one.
- A commit near the failing line is strong evidence only when a deploy ties it to when the
  errors started.
- Rare but **severe** errors (fatal, out of memory, data corruption) rank above steady
  ordinary ones.
- When records carry a tenant (`tenant_id`, `customer_id`…), "95% from `t-17`" becomes
  evidence of its own. Outputs show only the leading tenant and shares.
- A circuit breaker still rejecting after the real failures stopped is reported as "the
  dependency may have recovered".
- When the timestamps' order is in doubt, the report says so.

How well this works on known incidents, including the traps: [Benchmark](benchmark.md).
