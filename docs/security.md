# Data flow and security

**In short:** logsite runs where you run it (your laptop, CI, or one container you host). It
reads your code and your logs, keeps summaries on local disk, and sends nothing anywhere except
to the log store and alert destinations you configure. There's no telemetry and no logsite
service.

## What it reads

| | From | When |
|---|---|---|
| Your code | the project directory | every command; parsed locally (Python's `ast`, tree-sitter for Java and Go) |
| Git history | `git` in the project directory | commits and tags near an incident, history, `about` |
| Logs | a file you give it (`--from`), or the `[backend]` in `logsite.toml`: Loki, OpenSearch / Elasticsearch, CloudWatch Logs | `errors`, `investigate`, `repro`, `cost`, `trace`, the editor and MCP server |
| Test output | the command you run through `logsite agent -- <command>` | agent mode |

Log store credentials come from environment variables you name in the config (any key ending
in `_env`, such as `password_env = "LOKI_PASSWORD"`), or from the AWS SDK's usual chain for
CloudWatch; logsite never writes them to disk.

## Where data goes

| Destination | What | Only if |
|---|---|---|
| Your log store | queries for the time window you ask about | a `[backend]` is configured |
| Slack or a webhook | the error summary for an alert (masked titles, counts, code lines, an example trace id) | you run `logsite alert-server` with a destination configured |
| Grafana | the same summaries, served by `logsite alert-server` | Grafana's data source plugin points at it |
| Your AI assistant | what the MCP server's tools return, to the client that started it, over stdio | you add the MCP server or the Claude Code plugin |

Nothing else leaves the machine. `logsite alert-server` listens on `127.0.0.1` by default and
refuses any other address unless a token is set.

## What it keeps on disk

All under your user's directories; delete any of them at any time.

| Path | What |
|---|---|
| `$XDG_DATA_HOME/logsite/history/` (`~/.local/share/...`) | the history store, one SQLite file per project: error group summaries (masked titles, counts per hour, code locations, releases). Never log lines. Turn it off with `[history] enabled = false`. |
| `$XDG_DATA_HOME/logsite/store/`, or your `[store] url` | `logsite watch`'s error store: every error it read (masked), hourly counts per log call, alerts sent. See [Watch production](watch.md) |
| `$XDG_CACHE_HOME/logsite/repro/` (`~/.cache/...`) | `repro` cases (with masked inputs) and stack traces you pasted |
| `$XDG_CACHE_HOME/logsite/agent/` | the full output of commands run through agent mode, to compare with the next run |
| files you name | `--write-test`, `-o dashboard.json`, `-o logsite.alloy` |

## What it masks

Secrets and personal data are masked by default wherever logsite shows or stores something it
read from logs: error titles and causes, sample messages and stack traces, the values a
reproduction test is built from, request timelines, and the history store.

| Kind | Detected |
|---|---|
| Secrets | private keys, AWS access keys, GitHub, Slack and Stripe tokens, Google API keys, JWTs, bearer tokens, `password=`-style values |
| Personal data | email addresses, card numbers (Luhn-checked), IBANs (checksum-checked), US SSNs |

A masked value keeps just enough to recognise it (`a***@example.com`, `************1111`). Configure it
under `[leaks]`: `ignore` detector kinds, `allow` values that are fine, `ignore_fields`. The same
detectors power `logsite leaks`, which reports which log calls leaked what.

Masking is pattern-based, so it can miss data in an unusual format. Treat logsite's outputs
with the same care as the logs they came from.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
