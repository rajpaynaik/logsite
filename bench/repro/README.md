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

## The arms

Every arm gets a fresh copy of the service and the log, and the same task: *find the error
behind most of the failures, write a pytest test that reproduces it, fix the code.*

| Arm | Has |
|---|---|
| `logsite` | no AI: `logsite repro --write-test`, unedited. Free, and in CI ([`tests/test_bench_repro.py`](../../tests/test_bench_repro.py)). |
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
python bench/repro/run.py --run --trials 3         # all arms, 3 trials: 18 claude -p runs
python bench/repro/run.py --run --arm claude+logsite --scenario isbn-spaces   # one run
```

The Claude arms use your Claude usage. Results go to `bench/repro/results/<time>/`
(`results.md`, `results.json`, and each run's workspace and `claude.json`).

## Results

### `logsite`, no AI (every CI run)

| Scenario | logsite verdict | fail-to-pass | fixed |
|---|---|---|---|
| supplier-timeout | reproduced | ✗ | ✗ |
| isbn-spaces | reproduced | ✓ | ✗ |
| shipping-config | reproduced | ✓ | ✗ |

The generated tests reproduce all three bugs with no edits: the function to call, its
arguments and the fake all came from the log and the code. For `isbn-spaces` the failing
row's ISBN (`978 1 59327 584 6`) was the third of a feed of three; logsite took the value
the request logged last before failing. It fixes nothing, by design: that's the next step.

`supplier-timeout` isn't fail-to-pass: the test fakes a supplier that always times out, so
once retries are fixed the last attempt still does. The right test, "time out once, then
answer", needs a person or an assistant.

### `claude` vs `claude+logsite`

Not run yet.
