# GitHub Action: a pull request per new production error

The action reads your production logs on a schedule. For each **new** error group raised in
your code, it writes a reproduction test with [`logsite repro`](repro.md) and opens a **draft
pull request** with it, so nobody has to remember to run logsite. It never opens the same pull
request twice: each error gets one branch, `logsite/repro-<id>`, and an error that already has a
pull request (open or closed) is skipped.

## Set it up

1. Commit a `logsite.toml` with your log store (see [Configuration](configuration.md)). Keep
   secrets out of it: name an environment variable instead (`password_env = "LOKI_PASSWORD"`).
2. Add the secret to the repository (**Settings → Secrets and variables → Actions**).
3. Add `.github/workflows/logsite.yml`:

```yaml
name: logsite
on:
  schedule:
    - cron: "17 * * * *"      # every hour
  workflow_dispatch:

permissions:
  contents: write             # push the test's branch
  pull-requests: write        # open the draft pull request

jobs:
  repro:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      # optional: install the project, so each test is run and checked before it's opened
      - uses: actions/setup-python@v7
        with:
          python-version: "3.13"
      - run: pip install -e . pytest
      - uses: rajpaynaik/logsite@v0.4.1
        env:
          LOKI_PASSWORD: ${{ secrets.LOKI_PASSWORD }}
        with:
          hours: 1                # the window since the last run
          test-command: python -m pytest
```

A group is new when it shows up in this window and not in the one before it, so `hours` should
match the schedule.

If GitHub Actions can't open pull requests in your organization, allow it under **Settings →
Actions → General → Workflow permissions**, or pass a token with `github-token`.

## Inputs

| Input | Default | |
|---|---|---|
| `hours` | the config's window (24) | the window to read from the log store |
| `log-file` | | read a log file instead (an export, or one written by an earlier step) |
| `max-prs` | `3` | at most this many pull requests per run |
| `all` | `false` | every error group raised in the code, not only new ones |
| `test-command` | | run each test with this; the pull request then says whether it fails the way production did (`logsite agent --expect`) |
| `base` | the checked-out branch | the branch to open pull requests against |
| `dry-run` | `false` | write the tests and report what it would open, open nothing |
| `version` | the latest | the logsite version to install |
| `extras` | | logsite extras, e.g. `cloudwatch` |
| `python-version` | `3.13` | the Python logsite is installed with |
| `github-token` | `github.token` | the token that pushes and opens pull requests |

## What a pull request contains

- the test, `tests/test_repro_<id>.py`, written from what production logged: the function to
  call, the values the failing request logged, the dependency to fake;
- with `test-command`, the verdict: `✓ <id> reproduced: TimeoutError through
  app/supplier.py:31, as in production`, or why it didn't;
- what production logged: the error, where it was raised, the call path, the inputs and the
  failing request's lines.

Fill in any `TODO`, fix the code until the test passes, and push the fix to the same branch.

Only error groups raised in your code get a pull request: an error another service logged, or
one logged without a stack trace, has no line for a test to fail through.
