# Claude Code plugin

One install gives Claude Code three things:

- **Production context while it codes**: the `production_context` MCP tool tells the
  assistant, before it edits a function, which errors are raised or pass through there,
  whether one came back after a fix (and which commit fixed it), what its log calls cost,
  and the last commits. So it doesn't undo an earlier fix, and doesn't add a log line to a
  line that already writes millions a day.
- **Test and build failures grouped by cause**: a hook runs the assistant's `pytest`,
  `go test`, `mvn test`, `./gradlew build`, `npx jest`, `npm test`, `tsc`… through [agent mode](agent.md), so
  it gets "30 failed → 2 causes, here's the line" and what changed since the last run.
- **Skills**: `/logsite:investigate` (what's broken in production and why),
  `/logsite:fix-error` (find the cause, fix it, run the tests until they pass) and
  `/logsite:log-cost` (which log lines cost the most, and what to do about them).

## Install

```bash
pip install "logsite[mcp]"        # or: pipx install "logsite[mcp]"; needs logsite 0.3.1+
```

Then, in Claude Code:

```
/plugin marketplace add rajpaynaik/logsite
/plugin install logsite@logsite
```

Run Claude Code in your project (where `logsite.toml` is). Without a `[backend]`, the
hook, the lint findings and the git history still work; the production parts say they're
unavailable.

## What the assistant sees

Before changing `CarrierApiClient.java:58`, `production_context` returns (shown here as
`logsite about`, its command-line twin):

```
$ logsite about src/main/java/com/example/shipping/CarrierApiClient.java:58
src/main/java/com/example/shipping/CarrierApiClient.java:43-61
  errors in the last 24h:
        660  java.lang.IllegalStateException: Shipping label request failed  (raised here, back)
             root cause: java.net.SocketTimeoutException: Read timed out
             ↩ came back: gone after v1.3, fixed by 41bdce40 Retry carrier timeouts with backoff
  last changed:
    0df4dac1  2026-10-18  Maya: Lower carrier read timeout to 3s
    41bdce40  2026-09-03  Maya: Retry carrier timeouts with backoff
    bf7683ed  2026-07-20  Maya: Carrier label client
```

A Python line widens to the function around it; elsewhere to the 15 lines on each side;
`PATH:START-END` takes a range and `PATH` the whole file. `--format json` is what the MCP
tool returns.

When the assistant runs `pytest -q`, the hook turns it into `logsite agent -- pytest -q`
and the assistant gets:

```
pytest: 8 test(s) failing · 1 cause(s) · exit 1 · 0.2s

1. 8 test(s)  sqlite3.OperationalError: no such column: missing
   raised   app/repo.py:7 in get()
            7 │ return db.execute("select id, missing from orders where id = ?", (order_id,)).fetchone()
   tests    tests/test_repo.py ×8 (test_0, test_1, test_2, …)
```

## The hook

- Only plain test and build commands are rewritten (optionally after `cd DIR &&`):
  pipes, redirects, chains (`&&`, `;`), `VAR=value` prefixes and other commands are left
  as the assistant wrote them. `mvn`/`gradle` only for build and test goals.
- The command's exit code is kept, the full output is saved, and agent mode prints the raw
  output when it can't do better, so nothing is hidden from the assistant.
- It doesn't approve anything: your permission rules still decide whether the command runs.
- It works next to [RTK](https://github.com/rtk-ai/rtk): RTK leaves `logsite agent` commands
  alone, and an `rtk pytest` is turned into `logsite agent -- pytest` (agent mode needs the
  test tool's own output). Checked with both hooks installed.
- `LOGSITE_HOOK=off` turns it off; it never fails a command (a missing `logsite`, a broken
  config or bad input means no rewrite).

Without the plugin, the same hook in `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [{ "type": "command", "command": "logsite hook claude" }] }
    ]
  }
}
```

and the MCP server with `claude mcp add logsite -- logsite mcp`.
