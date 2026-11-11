# Get started

By the end of this page, logsite will be showing you your own errors, grouped by cause and
pointed at your code.

## 1. Install

You need Python 3.10 or later.

```bash
pip install logsite
```

## 2. Try it on a sample

The repository has a small shop with a planted bug and its production log.

```bash
git clone https://github.com/rajpaynaik/logsite
cd logsite/examples/bookstore
logsite errors --from ../../bench/repro/scenarios/supplier-timeout/incident.log
```

```
28 errors in incident.log · 2 group(s)

  #  TREND  COUNT  ERROR                                      RAISED AT       LOGGED AT
  1            25  TimeoutError: timed out                    supplier.py:31  api.py:36
  2             3  ConnectionRefusedError: cache unavailable  cache.py:15     api.py:23
```

28 errors became 2 causes. Each one shows the line that raised it and the line that logged it.

## 3. Use it on your service

From your project's folder, point logsite at a log file:

```bash
logsite errors --from app.log
```

To read straight from your log store instead, run:

```bash
logsite init
```

It asks where your logs are (Loki, OpenSearch, CloudWatch, Sentry, Databricks or a file),
tests the
connection, and saves the answer in `logsite.toml`. After that, `logsite errors` reads the
last 24 hours on its own.

Prefer a browser? `logsite app` does the same in a page on your computer.

## 4. Watch production

```bash
logsite watch --dry-run      # see what it would post
logsite watch
```

Every few minutes, logsite checks your logs, keeps each error in a database, and posts to
Slack when an error is new, back after a fix, or spiking. `logsite init` sets up Slack and the
database too. See [Watch](watch.md).

## Next steps

- **Fix an error:** [write a test that reproduces it](repro.md).
- **Check a release:** `logsite release` shows whether it made errors worse. See
  [Release health](releases.md).
- **See it in your editor:** install the [VS Code extension](vscode.md).
- **Ask about it:** connect [Claude](mcp.md) and ask "what failed in checkout yesterday?"

Everything else is in the [documentation](README.md).
