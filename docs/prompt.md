# An error as a prompt for your AI assistant

`logsite prompt <id>` turns one error from the [error store](watch.md#what-it-stores) into a
case file you paste into Claude, ChatGPT, Cursor or a ticket: everything an engineer would
gather before fixing it, in one place. logsite calls no AI itself; it writes text.

```bash
logsite prompt               # lists the errors of the last 7 days, with their ids
logsite prompt 346fa937      # the case file for one (an id's first characters are enough)
logsite prompt 346f --copy   # ... straight to the clipboard
logsite prompt 346f --format json   # the same, as data
```

In `logsite errors -i`, the terminal error browser, `p` copies the same case file for the
error you're on.

The store fills as [`logsite watch`](watch.md) runs; `logsite db backfill --since 7d` loads past
errors from your log store.

## What's in it

| Section | From |
|---|---|
| **The error**: the exception, the root cause, where it's raised and logged, the code around it, a link to the line | the store and your checkout |
| **How often**: total, the last 24 hours against the 24 before, the busiest hour, per service and release | every stored occurrence |
| **When it started**: first seen, the first release with it, and the commits that changed the code around it in the 3 days before | the store and `git log` |
| **Has it happened before**: when it stopped, the commits that fixed it then, how often it came back | logsite's history |
| **What the failing requests have in common**: a field with one value in most occurrences (`currency = JPY` in 412 of 412), and the fields that vary, so are probably not the cause | up to 500 occurrences |
| **Examples**: the latest 3, with service, release and trace id; the first with its stack trace, library frames folded | the store |
| **A test that reproduces it**: the one `logsite repro` wrote, or the command to write it | your checkout |
| **What I'd like**: explain the cause, fix it where it starts, keep the test | |

Values were masked (secrets, emails, card numbers) when they were stored, so the case file
holds no more than the store does. Only failing requests are stored, so "in common" means
across the failures, not compared with normal traffic.

## Example

From the [bookstore demo](../examples/bookstore), after a release that dropped a retry:

````markdown
# Production error: TimeoutError: timed out

## The error

- **Error:** `TimeoutError: timed out`
- **Raised at:** `bookstore/supplier.py:31` in `restock()`
- **Logged at:** `bookstore/api.py:36`

```python
# bookstore/supplier.py
  29 |         for attempt in range(1, self.retries + 1):
  30 |             try:
> 31 |                 with urllib.request.urlopen(request, timeout=self.timeout) as resp:
  32 |                     return json.loads(resp.read())
```

## How often

- **25** times between 2026-10-20 08:00 UTC and 2026-10-20 08:03 UTC
- Services: `bookstore` (25)

## When it started

- First seen 2026-10-20 08:00 UTC
- Latest git tag at the time: `v1.4`
- Commits that changed the code around `bookstore/supplier.py:31` in the 3 days before it started:
  - `0e794f9c` 2026-10-19 Ada: Call the supplier directly, drop the retry wrapper

## Examples

**1.** 2026-10-20 08:03 UTC · service `bookstore` · trace_id `45c596d442d01ba3`

```
Traceback (most recent call last):
  File "/srv/bookstore/bookstore/inventory.py", line 18, in reorder
    confirmed = SUPPLIER.restock(sku, qty)
  File "/srv/bookstore/bookstore/supplier.py", line 31, in restock
    with urllib.request.urlopen(request, timeout=self.timeout) as resp:
  ... 10 library frame(s)
TimeoutError: timed out
```
````
