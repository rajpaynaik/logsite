# Reproduce a production error as a test

`logsite repro` turns one production error group into what a failing test needs, and for
Python writes the test. `logsite agent --expect` then checks that the test fails **the way
production did**: the same exception, through the same line of your code. A test that fails
for another reason (a mock that raises the error itself, the wrong function called) doesn't
count.

Nothing here calls an AI model. You can write the rest of the test yourself, or let Claude
Code (or any MCP client) do it with the `reproduction_case` tool: either way the check is
the same.

## From a log file: no connection needed

Download the logs (Grafana Explore has a download button; any JSON-lines or plain-text log
works), or paste a stack trace:

```
$ logsite repro --from incident.log --write-test
Reproduce 2d27bfe0 · TimeoutError: timed out · 4 error(s)
  Fail with    TimeoutError through app/supplier.py:10 (restock)
  Call path    inventory.reorder › supplier.restock › socket
  Call         reorder(sku, qty, warehouse)  from app.inventory  app/inventory.py:11
  Fake         app/supplier.py:10 calls urllib.request.urlopen(…): patch urllib.request.urlopen raise TimeoutError
  Inputs       sku       = BK-103                logged at app/inventory.py:12
               qty       = 5                     logged at app/inventory.py:12
               warehouse = main                  logged at app/inventory.py:12
  Request      trace_id=t3: 2 line(s)
               19:23:01 INFO  reorder BK-103 qty=5 warehouse=main  app/inventory.py:12
               19:23:03 ERROR restock failed for BK-103  app/inventory.py:19
  Next         run tests/test_repro_2d27bfe0.py as it is:
               logsite agent --expect 2d27bfe0 -- pytest tests/test_repro_2d27bfe0.py

$ pbpaste | logsite repro --from -        # a stack trace copied from Slack or an alert
```

With a `[backend]` configured, leave out `--from`: `logsite repro <id>` reads the last
`--hours` from it (the ids are the ones `logsite errors` shows). The first group is the
default; pick another by id or by its number in the list.

Where each part comes from:

| | From |
|---|---|
| **Fail with** | the error group: the exception, and the line of your code its cause went through |
| **Call** | the outermost function of yours on the stack trace, read from the code |
| **Inputs** | the failing request's log lines (every line with its trace id), mapped back to the code that filled each log call: `log.info("reorder %s qty=%s", sku, qty)` logged `BK-103` and `5`, so `sku="BK-103"`, `qty=5`. Structured fields and `key=value` pairs count too. Secrets and personal data stay masked. |
| **Fake** | when the cause came out of a library: the call on that line, and what to patch so it fails the same way (`self.client.post` is patched on the class `self.client` was made from) |
| **Missing** | what the logs didn't say, and how to log it next time |

## The test

`--write-test` writes `tests/test_repro_<id>.py` (or the path you give it):

```python
from unittest import mock

from app.inventory import reorder


def test_repro_2d27bfe0(caplog):
    """Production: TimeoutError: timed out (4x, 2026-10-21 19:20 UTC).
    Raised through app/supplier.py:10. Fails until that's fixed.
    """
    # app/supplier.py:10 calls urllib.request.urlopen(...): make it fail as in production
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        reorder(
            sku="BK-103",  # logged at app/inventory.py:12
            qty=5,  # logged at app/inventory.py:12
            warehouse="main",  # logged at app/inventory.py:12
        )
    # Once it's fixed the code goes further: fake what it calls next (a service, the
    # database) so the test doesn't need them.
    # If the code logs the error instead of raising it, fail with what it logged.
    for record in caplog.records:
        if record.exc_info and record.exc_info[1] is not None:
            raise record.exc_info[1]
```

It asserts no behaviour: it fails while the bug is there and passes once it's gone. Add
the assertions you want (what `reorder` should return when the supplier is slow) as you fix
it. What the logs don't hold becomes a `TODO`: an object to build (`order` when only
`order.id` was logged), a value that was masked, a call it couldn't tell how to patch.

Skeletons are Python-only for now; for Java and Go the case (`--format json`) has
everything a test needs.

## Is it really the production error?

```
$ logsite agent --expect 2d27bfe0 -- pytest tests/test_repro_2d27bfe0.py
pytest: 1 failed · 1 cause(s) · exit 1 · 0.3s
...
✓ 2d27bfe0 reproduced: TimeoutError through app/supplier.py:10, as in production
```

| Verdict | Means |
|---|---|
| `✓ reproduced` | a test failed with production's exception, and its traceback goes through production's line |
| `✗ wrong failure` | it failed, but not like production; the message says how: raised in the test itself (a mock raising it), raised before production's line (something on the way is faked, so the code that failed never ran), or another exception |
| `✗ didn't fail` | every test passed |

It exits 0 only for `reproduced`. A line a few lines away from production's, in the same
file, still counts, with a note: the code you have differs a little from what production
ran. The case is saved under `~/.cache/logsite/repro/` when you run `logsite repro`, so the
check needs no logs.

Then fix the code, and run the tests as usual:

```
$ logsite agent -- pytest tests/test_repro_2d27bfe0.py
pytest: 1 passed · exit 0 · 0.3s
✓ fixed   TimeoutError: timed out  (1 test(s))
```

## With Claude Code

The MCP tool `reproduction_case` returns the same case as JSON, with the skeleton. The
`/logsite:fix-error` skill reproduces first: it writes the test, runs `--expect` until it
says `reproduced`, then fixes the code until the test passes.

## How well it works

The [repro benchmark](../bench/repro/README.md) runs this on three planted bugs in a demo
service, from incident logs the service really wrote: logsite alone (no AI), Claude Code
with only the log, and Claude Code with logsite.

## Limits

- The test is only as good as what was logged. No values on the failing request's lines
  means `None` and a `TODO` for each argument; `Missing` says which log call to add them to.
- No stack trace logged (`logger.error` without `exc_info`) means no line to check: the
  verdict then only compares the exception.
- Only the error's own dependency is faked. Once it's fixed, the code may call the next
  one (another service, the database): fake that too.
