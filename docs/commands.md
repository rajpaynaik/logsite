# Commands

Run them in your project (where `logsite.toml` is); `logsite <command> --help` lists the
options. The demo in [`examples/demo_app`](../examples/demo_app) has a sample log, so most of
them work there without a log backend.

```bash
cd examples/demo_app
logsite init               # set up: log store, Slack, database, each tested (asks in the terminal)
logsite index              # list every log call
logsite lint               # static log-quality checks (exit 1 on findings, good for CI)
logsite context            # required context fields, and what's already added automatically
logsite context --production   # how often real records actually carry them, per log call
logsite structure          # share of log calls with a constant message + fields (--production: by volume)
logsite counts             # production counts per line, from the configured backend
logsite cost --price-per-gb 0.5   # bytes and $/month per log call, most expensive first
logsite lint --volume      # static + volume rules
logsite spikes             # lines that jumped vs the previous window, with suspect commits
logsite spikes --since-deploy v1.4.0   # what changed after a release, with that release's commits
logsite errors             # production errors grouped by cause, new and spiking first
logsite errors --from incident.log   # ... from a log file (a download from Grafana)
logsite errors --since-deploy v1.4.0   # what started failing after a release
logsite investigate        # errors linked into incidents, with the deploy and suspect commits
logsite sync --backfill 30d   # fill the history store; then `logsite sync` on a schedule
logsite seen ed64f864      # one error group's history: first seen, fixes, regressions
logsite prompt ed64f864 --copy   # one stored error as a case file to paste into an AI assistant
logsite agent -- pytest    # test/build failures grouped by cause + what changed (AI assistants)
logsite repro --from incident.log --write-test   # a production error as a failing pytest test
logsite agent --expect 2d27bfe0 -- pytest tests/test_repro_2d27bfe0.py   # does it fail like production?
logsite pipeline --max-per-window 100000 --drop-level debug -o logsite.alloy   # Alloy drop/sample rules
logsite grafana -o dashboard.json   # Grafana dashboard: cost and errors per code line (Loki)
logsite alert-server       # send the error summary with Grafana alerts (Slack / webhook)
logsite alert-server --sync-every 15m   # ... and keep the history filled (the Docker image does)
logsite about app/checkout.py:7   # what production says about this code: errors, cost, commits, lint
logsite history app/checkout.py:7   # commits that touched the code around a log line
logsite hook claude        # Claude Code hook: test/build commands through agent mode (reads stdin)
logsite leaks              # secrets / personal data in production logs, per log call (exit 1 if any)
logsite trace 4bf92f35...  # every log line for one request, across services, in order
logsite serve              # language server on stdio (used by editors)
logsite mcp                # MCP server on stdio (used by AI assistants)
```

Most commands take `--hours` (the window, default `window_hours` in the config),
`--format json` for scripts, and `--color auto|always|never` where there's colour.
