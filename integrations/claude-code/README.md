# logsite for Claude Code

Production context, test failures grouped by cause, and skills to investigate and fix
production errors. See [docs/claude-code.md](../../docs/claude-code.md).

```bash
pip install "logsite[mcp]"
```

```
/plugin marketplace add rajpaynaik/logsite
/plugin install logsite@logsite
```

- `.mcp.json`: the logsite MCP server (`logsite mcp`), including `production_context`
- `hooks/hooks.json`: `logsite hook claude`, test and build commands through agent mode
- `skills/`: `/logsite:investigate`, `/logsite:fix-error`, `/logsite:log-cost`
