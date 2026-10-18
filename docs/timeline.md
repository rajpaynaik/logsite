# Following a request

A failed checkout is hard to read service by service: the gateway returns a 504, checkout
logs a timeout, inventory logs a warning. In order, the cause is obvious:

```
$ logsite trace 4bf92f3577b34da6a3ce929d0e0e4703
trace_id=4bf92f3577b34da6a3ce929d0e0e4703: 7 line(s) from 3 service(s) over 2.015s

+     0.0ms   gateway    info    POST /checkout
+     3.0ms   checkout   info    checkout_started  app/checkout.py:7
+     5.0ms   inventory  INFO    reserve request
+  2005.0ms ~ inventory  WARN    db pool exhausted, waited 2000ms
+  2010.0ms ! checkout   error   inventory_timeout  app/checkout.py:11
+  2011.0ms   checkout   info    charging card ************1111  app/checkout.py:13
+  2015.0ms ! gateway    error   POST /checkout 504

first problem: inventory (WARN): db pool exhausted, waited 2000ms
first error: checkout at app/checkout.py:11 (error): inventory_timeout
```

- Lines this repo wrote link to their log call; other services show the service name
  (from `service`, `service.name`, `app`, `container`, CloudWatch log stream, ...).
- The id matches the field and its common spellings (`traceId`, `trace.id`), or the
  message as a whole word when a record has no such field.
- `[backend]` is usually scoped to one service. Point `[timeline.backend]` at a wider scope
  (`{env="prod"}`, `logs-*`) to see the whole path.
- Secrets and personal data in messages are masked, so it's safe to hand to an AI assistant
  (`request_timeline` over MCP).

This needs a shared id in the logs. Context rules (`[context] all = ["trace_id"]`) and
`logsite context --production` check that it's there.
