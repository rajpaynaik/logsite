# Repro benchmark: a production error, reproduced and fixed

Does logsite make an AI coding assistant better at fixing production errors, or does the
assistant do as well with the raw log? This measures it.

## The scenarios

[`examples/bookstore`](../../examples/bookstore) is a small Python service with three
planted bugs. Each scenario is its code plus the `incident.log` of one bug, written by
[`make_incidents.py`](make_incidents.py) running the service for real (real socket
timeouts against a slow local supplier; every stack trace is the one Python printed). Each
log also has a decoy: a cache outage logged as warnings with a traceback, which breaks
nothing.

| Scenario | Bug | The fix |
|---|---|---|
| `supplier-timeout` | a read timeout raises `TimeoutError`, which the retry loop doesn't catch (it catches `URLError`) | retry it too |
| `isbn-spaces` | `normalize_isbn` drops hyphens but not spaces, though its docstring says both | drop spaces |
| `shipping-config` | the code reads the setting `rate_per_kg`; it's called `rates_per_kg` | the right name |

Those are easy: a 400-line log, one request at a time, one bug. The hard scenarios are what
a real incident looks like. Their logs are a day of traffic assembled from real requests
captured the same way (each with its real lines and stack traces), interleaved as
concurrent requests are; they're written on the fly (`make_incidents.py --hard`, ~30 MB, not
in git):

| Scenario | Log | What makes it hard |
|---|---|---|
| `hard-noisy-day` | 18.5 MB, 64,580 lines | the ISBN bug only starts at 09:00, and a cache outage (1,500) and client errors (700) are louder than it (400) |
| `hard-rare-value` | 8.6 MB, 31,070 lines | a 4th bug: one feed row in a hundred has `pages: 0` (`ZeroDivisionError`). The 0 is on a line of its own, among the lines of every other request in flight |
| `hard-drift` | 5.2 MB, 17,500 lines | the supplier timeout, but production runs last week's build (every line 5 earlier than here), and the gateway's retries are the loudest errors in the log (750, none of them in this code) |

## The arms

Every arm gets a fresh copy of the service and the log, and the same task: *find the error
behind most of the failures, write a pytest test that reproduces it, fix the code.* The hard
scenarios describe the symptom as a user would ("since 09:00", "some feed imports fail").

| Arm | Has |
|---|---|
| `logsite` | no AI: `logsite repro --write-test`, unedited. Free, and in CI ([`tests/test_bench_repro.py`](../../tests/test_bench_repro.py)). It picks the error itself, except in `hard-rare-value`, where it's given `ZeroDivisionError` as a person would pick it from `logsite errors` after reading "feed imports fail". |
| `claude` | Claude Code (`claude -p`), the code and the log |
| `claude+logsite` | the same, plus logsite: its MCP server and the `logsite` command |

The Claude runs leave your own settings, hooks, plugins and memory out
(`--setting-sources project --strict-mcp-config --no-session-persistence`), run in a fresh
directory each, and stop at `--budget` dollars (default 2.00) or 20 minutes.

## The score

All deterministic, nothing judged by a model:

- **fail-to-pass**: the arm's new tests fail on the buggy code and pass with the reference
  fix (as SWE-bench checks a test), each on a clean copy;
- **logsite verdict**: `logsite agent --expect` on the same tests against the buggy code;
- **fixed**: a hidden test for the bug (in [`oracles/`](oracles), never in the arm's copy)
  passes on the arm's code, and so do the service's own tests;
- **tokens, cost, time** as `claude -p` reports them.

## Running it

```bash
pip install -e .                                   # logsite, with pytest
python bench/repro/run.py --check                  # the scenarios are sound (free)
python bench/repro/run.py                          # the logsite arm (free)
python bench/repro/run.py --run --hard --trials 3  # the hard scenarios: 18 claude -p runs
python bench/repro/run.py --run --arm claude+logsite --scenario isbn-spaces   # one run
```

The Claude arms use your Claude usage. Results go to `bench/repro/results/<time>/`
(`results.md`, `results.json`, and each run's workspace and `claude.json`).

## Results

### `logsite`, no AI (every CI run)

| Scenario | picked the right error | logsite verdict | fail-to-pass | time |
|---|---|---|---|---|
| supplier-timeout | ✓ | reproduced | ✗ | 0.2 s |
| isbn-spaces | ✓ | reproduced | ✓ | 0.2 s |
| shipping-config | ✓ | reproduced | ✓ | 0.2 s |
| hard-noisy-day | ✓ (it started at 09:00: ranked first, above louder errors) | reproduced | ✓ | 1.9 s |
| hard-rare-value | given the name | reproduced | ✓ | 0.8 s |
| hard-drift | ✓ (skips the gateway's errors: not this code's) | reproduced | ✗ | 0.7 s |

The generated tests reproduce all six with no edits: the function to call, its arguments
and the fake all came from the log and the code. In `hard-rare-value` the `0` came from the
failing request's own lines, found by its trace id among 31,000 interleaved ones; in
`hard-drift` the statement production failed at (line 26) was found at line 31 here, by the
source text in the traceback. It fixes nothing, by design: that's the next step.

Both supplier-timeout scenarios miss fail-to-pass: the test fakes a supplier that always
times out, so once retries are fixed the last attempt still does. The right test, "time out
once, then answer", needs a person or an assistant.

### `claude` vs `claude+logsite`: the easy scenarios (1 trial, 2026-10-21)

| Arm | fail-to-pass | fixed | tokens (mean) | cost (total) | time (mean) |
|---|---|---|---|---|---|
| `claude` | 3 / 3 | 3 / 3 | 318k | $0.93 | 43 s |
| `claude+logsite` | 3 / 3 | 3 / 3 | 370k | $0.93 | 49 s |

No difference: Claude alone reproduced and fixed all three. On a 400-line log with one bug
it reads the whole log, and logsite has nothing to add; the logsite arm spent more tokens
(its tools' descriptions) and never called `reproduction_case`. Cost as `claude -p` reports
it at API prices; on a subscription it's usage, not a charge.

### The hard scenarios

Not run yet with Claude.
