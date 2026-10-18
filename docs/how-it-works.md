# How it works

## How log → line matching works

Matching production logs to source lines is the hard part. logsite tries two methods, most reliable first:

1. **Structured fields.** If your logs carry `pathname`/`filename` + `lineno` (or the ECS `log.origin.file.*` / OpenTelemetry `code.*` equivalents, or the [Java and Go](languages.md#java-and-go) caller fields), matching is exact. A call that spans several lines matches on any of them. Python's `logging` provides these for free; for example with [python-json-logger](https://github.com/nhairs/python-json-logger):
   ```python
   JsonFormatter("%(asctime)s %(levelname)s %(message)s %(pathname)s %(lineno)d")
   ```
2. **Template matching.** Each message template (`"user %s logged in"`) becomes a regex. When several match, the one with the most literal text wins, so `"%s"` never steals a match from a specific template.

Adding `pathname` + `lineno` to your log format is the biggest accuracy upgrade you can make.

### Counting at scale

By default logsite downloads up to 5,000 log lines per window and matches them itself, so
counts on busy services are capped and quieter lines can be missed entirely. With
`aggregate = true` (Loki and OpenSearch/Elasticsearch), the log store does the counting
per file and line, for example `sum by (pathname, lineno) (count_over_time(...))` in Loki,
so counts are exact at any volume. Lines without file/line fields are still downloaded and
matched by template, so only those are subject to the cap.

## How spikes map to commits

Each refresh fetches counts for the current window and the one just before it. A line whose
count grew by `spike_ratio` (and passed `spike_min_count`) is a spike. logsite then runs
`git log -L` over the **whole function** that contains the log call, because a line usually
spikes when its callers or surrounding logic change, not the log call itself. Line numbers
refer to `HEAD`, so uncommitted edits above the function can shift the range.

### After a release

`logsite spikes --since-deploy v1.4.0` compares each line from the deploy until now with an
equally long window just before it. Besides spikes, it lists **new errors**: error lines that
never fired before the deploy, even a handful of times. For each one it shows only the
commits **in that release** (from the previous tag, or `--base`, up to `v1.4.0`) that touched
the surrounding function, so unreleased work on `main` doesn't show up.

The deploy time defaults to the tag date (annotated tags) or the commit date. If you deploy
later than you tag, pass the real time: `--deployed-at 2026-10-16T14:05:00Z`.

## Architecture

```
VS Code / JetBrains / Neovim (thin client)
        │  Language Server Protocol
Python core (pygls)  ── also exposed as an MCP server (mcp_server.py)
  ├── index.py     → finds every log call in code (ast)
  ├── matching.py  → production log → source line
  ├── backends/    → file | Loki | OpenSearch/Elasticsearch | CloudWatch
  ├── lint.py      → noise, dead logs, missing IDs, PII
  ├── tracebacks.py → finds and parses the exception in a log record
  ├── errors.py    → groups production errors by cause
  ├── kafka*       → consumer lag, DLQ, errors per topic
  └── git.py       → commits that touched a spiking line (git log -L)
                                                   * = roadmap
```

Every backend implements one interface ([`backends/base.py`](../src/logsite/backends/base.py)):

```python
class LogBackend(ABC):
    def query_logs(self, time_range, contains=None, limit=5000) -> list[LogRecord]: ...
    def count_by_line(self, sites, time_range) -> dict[str, int]: ...  # default: fetch + match
    def query_errors(self, time_range, limit=5000) -> list[LogRecord]: ...  # default: filter
```
