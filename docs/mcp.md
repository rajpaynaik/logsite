# MCP server for AI assistants

`logsite mcp` exposes logsite to AI assistants over the [Model Context Protocol](https://modelcontextprotocol.io).
Install it with `pip install "logsite[mcp]"`, then register it. For Claude Code:

```bash
claude mcp add logsite -- logsite --config-dir /path/to/your/project mcp
```

With the [Claude Code plugin](claude-code.md) it's set up for you, with a test-output hook and skills.
Other clients take the same command (`logsite --config-dir <project> mcp`) in their MCP config.
All tools are read-only:

| Tool | What it answers |
|------|-----------------|
| `production_context` | Before changing `app/checkout.py:42`: which errors are raised, logged or pass through there (and did one come back after a fix?), what its log calls cost, the last commits, lint findings. |
| `error_summary` | What's failing in production right now (or since release `v1.4.0`), grouped by cause, and where is it raised? |
| `request_timeline` | What happened to request `4bf92f35…`, service by service, and where did it first go wrong? |
| `compare_deploy` | What spiked or started erroring after release `v1.4.0`, and which commits in it are to blame? |
| `spikes` | Which log lines jumped vs the previous window, and which commits touched that code since? |
| `log_history` | Which commits changed the function around `app/checkout.py:42`? |
| `log_costs` | Which log calls cost the most per month? |
| `context_coverage` | Which required context fields are missing from real records, and on which log calls? |
| `structure_score` | How much of our logging is structured, and which loud lines aren't? |
| `find_leaks` | Which log calls are writing secrets or personal data to production logs? |
| `log_counts` | Which log calls are the loudest in production? |
| `lint` | Which log calls have quality problems (optionally including volume rules)? |
| `list_log_calls` | Where are the log calls, and what are their message templates? |
| `list_rules` | What do the `LL…` codes mean? |
