---
name: log-cost
description: Find the log lines that cost the most and cut the bill - noisy lines, debug logging in production, logs in hot loops. Use when the user asks about logging costs, a big Datadog/Loki/CloudWatch bill, noisy logs, or which logs to remove or sample.
---

# Cut log cost

1. Call `log_costs` (top 20): events, bytes and spend per month for each log call. If
   spend is missing, the config has no `[cost] price_per_gb`; rank by bytes and say so.
2. For the top lines, open the code and check why it's loud: a log in a loop, a debug line
   at info, a large payload logged whole, a retry loop logging every attempt.
3. Propose per line, with the saving: delete it, lower its level, log once per batch,
   log a summary instead of the payload, or sample it. Call `lint` on those files for
   related problems (LL004 logs in loops, LL001 eager formatting).
4. Change code only when the user agrees. For lines that can't change soon, point them to
   `logsite pipeline -o logsite.alloy`: Grafana Alloy rules that drop or sample them
   before they're shipped (warning and error lines never get a rule). Never deploy those
   rules yourself; the user reviews and applies them.
