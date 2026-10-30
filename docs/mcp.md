# Connect Claude (or any AI assistant) to logsite

`logsite mcp` gives AI assistants logsite's tools over the [Model Context Protocol](https://modelcontextprotocol.io),
so you can ask in plain English: *"what failed in checkout yesterday after 3pm?"*, *"is the
timeout error from last month back?"*, *"why is 346fa937 happening, and how do I fix it?"*.
The assistant picks the tools and reads the answers; logsite itself calls no AI. Every tool is
read-only.

```bash
pip install "logsite[mcp]"
```

## Claude Code

```bash
claude mcp add logsite -- logsite --config-dir /path/to/your/project mcp
```

With the [Claude Code plugin](claude-code.md) it's set up for you, with a test-output hook and skills.

## Claude Desktop

Add logsite to `claude_desktop_config.json` (**Settings → Developer → Edit Config**), then
restart Claude Desktop:

```json
{
  "mcpServers": {
    "logsite": {
      "command": "/full/path/to/logsite",
      "args": ["--config-dir", "/path/to/your/project", "mcp"]
    }
  }
}
```

Use the full path (`which logsite`): Claude Desktop doesn't see your shell's `PATH`. It runs on
your machine, so it reaches your log store and error store the way your terminal does.

## claude.ai, or a whole team: over HTTP

Run logsite where it can reach the error store (next to [`logsite watch`](watch.md), e.g. on
[Kubernetes](kubernetes.md)), with a token:

```bash
export LOGSITE_MCP_TOKEN=$(openssl rand -hex 24)
logsite mcp --http --host 0.0.0.0 --port 8078
```

It serves `http://<host>:8078/mcp`, and refuses requests without the token: send it as
`Authorization: Bearer <token>`, or put it at the end of the URL, `/mcp/<token>`, for clients that
take only a URL. On localhost (`--host 127.0.0.1`, the default) no token is needed, and requests
for any other host name are refused, so a web page can't reach it.

- **claude.ai**: custom connectors are called from Anthropic's servers, so the server needs a
  public HTTPS address (a load balancer, an ingress, or a tunnel such as Cloudflare Tunnel).
  Add it under **Settings → Connectors → Add custom connector** with the URL
  `https://logsite.example.com/mcp/<token>`. Anyone with that URL can read your stored errors
  (masked values, stack traces, code locations): keep it secret, and rotate the token if it leaks.
- **Claude Code, Cursor and other clients** take the URL and a header:
  `claude mcp add --transport http logsite https://logsite.example.com/mcp --header "Authorization: Bearer <token>"`.

## Tools

| Tool | What it answers |
|------|-----------------|
| `stored_errors` | What failed between 2 and 4pm last Tuesday, in `checkout`, in `v1.4`, or mentioning "timeout"? An instant lookup in the [error store](watch.md#what-it-stores), for any range it keeps. |
| `error_case` | Everything about error `346fa937` to explain and fix it: the code, how often and where, when it started and the commits just before, past fixes, what the failing requests share, examples. The same as [`logsite prompt`](prompt.md). |
| `error_occurrences` | The latest occurrences of an error, with their fields, stack traces and trace ids. |
| `error_trend` | An error per hour: when it started, whether it's rising, whether a fix stopped it. |
| `slow_calls` | What got slower since yesterday? Endpoints and log calls whose p95 went up, with p50/p95/p99 and calls before and after. The same as [`logsite slow`](latency.md). |
| `release_health` | Is the latest release healthy? Its verdict, errors per hour before and after, and the errors it made new, brought back, spiked or fixed, with suspect commits. The same as [`logsite release`](releases.md). |
| `production_context` | Before changing `app/checkout.py:42`: which errors are raised, logged or pass through there (and did one come back after a fix?), what its log calls cost, the last commits, lint findings. |
| `investigate` | What's the outage and what caused it? Incidents with their cause, the deploy and suspect commits, and a `diagnosis`: ranked explanations with evidence for and against, and the next check. |
| `error_summary` | What's failing in production right now (or since release `v1.4.0`), grouped by cause, and where is it raised? |
| `request_timeline` | What happened to request `4bf92f35…`, service by service, and where did it first go wrong? |
| `reproduction_case` | Reproduce error `2d27bfe0` as a test: the function to call, the values the request logged, what to fake, and a pytest skeleton. `logsite agent --expect` then checks the test fails the way production did. Works from a log file too. |
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
