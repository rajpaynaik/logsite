# Changelog

logsite follows [semantic versioning](https://semver.org). Before 1.0, a minor version
(0.x.0) can change commands, config or JSON output; a patch version (0.x.y) doesn't.
The VS Code extension has [its own changelog](editors/vscode/CHANGELOG.md).

## Unreleased

- A log call whose template is only placeholders and punctuation (`"%s: %s"`,
  `` `${a}: ${b}` ``) is no longer matched against messages: it claimed unrelated errors,
  so "logged at" pointed at the wrong line. Templates need 4 letters of fixed text;
  constant messages still match.
- pytest's log format (`ERROR    app.orders:orders.py:42 ...`, in captured-log sections
  and live `log_cli` output) is parsed: the level, logger, and the file and line of the
  log call, so CI output maps to the code without a template match.
- hclog, HashiCorp's Go logger (Consul, Vault, Nomad, Terraform), is parsed: the level,
  the logger's name, the message, and the `key=value` pairs after it. The message alone
  now matches its log call; on Consul's CI output, every group found its "logged at",
  where before none did.
- slog's `source=main.go:1477` (its text handler with `AddSource`) gives the file and line,
  and a structured line a Go test logs through `t.Log` (`reload_test.go:204: time=...
  level=ERROR ...`) is parsed past that prefix.
- GitHub Actions' `##[error]` in front of a line no longer hides the error after it.
- Indexing Python code no longer prints the `SyntaxWarning`s old code raises ("invalid
  escape sequence") over the output.

## 0.7.1 (2026-10-24)

Fixes from a test on live logs (Grafana Play's Loki, the OpenTelemetry demo services):

- Errors without a stack trace no longer split by the names and codes in their message:
  runs of capitalized words ("Roof Binoculars") and codes of capitals and digits
  ("OLJCESPC7Z", "REQ0534") are values now. On one service, 220 groups became 22. HTTP reason
  phrases ("Bad Gateway") still tell errors apart. These groups get new ids, so `watch` reports
  them as new once.
- A stack trace that arrives as its own log entry (some shippers split `logger.exception()`'s
  line and traceback, even into different Loki streams, seconds apart) is joined to its error
  line: one error, not two groups of the same size.
- When an exception from a library is raised while handling an error from your code (a
  tracing SDK failing during cleanup), the error is titled by the one being handled.
- Prefixes stripped from plain-text lines: Spring Boot 3 without a PID
  (`ERROR --- [thread] logger : `) and bracketed fields before ` - `.
- Numbers inside words (`o11y`, `k8s`) are no longer masked.
- Go's logfmt (`level=error msg="..." err="..."`: logrus, go-kit, Grafana's tools, k6) and
  klog (`E1025 03:04:55.030320 1 reflector.go:227] "..." err="..."`: Kubernetes) lines are
  parsed: level, message, error, time and, for klog, the file and line. They were missed as
  errors before.
- Next.js and Node.js: `⨯ unhandledRejection:` / `uncaughtException:` errors are found, and
  `at fn (file:line:col)` frames split into their own entries are joined back.
- Loki: joining split stack traces works on Grafana Cloud and Loki 3. It used to re-read each
  stream with a selector built from its labels, which matches nothing when they include
  `detected_level` or structured metadata; it now uses the configured selector.
- Error titles are one line, even when the error has line breaks in it.

## 0.7.0 (2026-10-24)

- `logsite init`: set logsite up from the terminal, one question at a time: where the logs are
  (tested, with a retry that keeps your answers), Slack (a test message) and where to store
  errors (tested, tables created). Settings go to `logsite.toml`, passwords, tokens and
  webhooks to the secrets file. The same steps and code as `logsite app`.
- `logsite errors -i`: browse the error store in the terminal: the error groups for a time
  range (`t` changes it, `/` searches, `s` picks a service), Enter for an error's case file
  with a 7-day chart, `p` to copy it as a prompt for an AI assistant, `r` to write the
  reproduction test. Built on curses, no new dependency.
- Testing a log file (in `logsite init` and `logsite app`) reads the whole file, not only its
  last 24 hours.

## 0.6.0 (2026-10-24)

- [`logsite prompt <id>`](docs/prompt.md): one stored error as a case file to paste into an AI
  assistant: where it's raised with the code around it, how often and in which services and
  releases, when it started and the commits just before, whether it was fixed before, what the
  failing requests have in common, and real examples with library frames folded. `--copy` puts
  it on the clipboard, `--format json` gives the data.
- [Connect Claude to logsite](docs/mcp.md): the MCP server has error-store tools
  (`stored_errors` for any time range, `error_case`, `error_occurrences`, `error_trend`), so you
  can ask "what failed in checkout yesterday after 3pm?" in plain English. `logsite mcp --http`
  serves it over HTTP with a token, for claude.ai's custom connectors and for a whole team;
  setup steps for Claude Code, Claude Desktop and claude.ai.

## 0.5.1 (2026-10-24)

- A shorter README.
- Grafana plugin 0.1.1: licensed under Apache 2.0, and test fixtures are no longer bundled.

## 0.5.0 (2026-10-24)

- [`logsite app`](docs/app.md): set logsite up in the browser (log store, Slack and database
  forms, each with a test button), then see the errors grouped by cause with the line of code,
  for any date range, filtered by service, release or text; write or save a failing test with
  one click. Secrets are kept out of `logsite.toml`.
- [`logsite watch`](docs/watch.md): checks the logs on a schedule, stores every error, and posts
  new errors, regressions and spikes to Slack, each at most once per cooldown. Its first check
  reads the last 24 hours without posting.
- **The error store, in your own database**: every occurrence (masked), the groups, hourly
  volume per log call and the alerts sent, in SQLite (the default), Postgres, MySQL / MariaDB,
  SQL Server or ClickHouse (beta). Tables are created on first use and upgraded by schema
  version. `logsite db init`, `status`, `sql` (the DDL, for a DBA), and `backfill` (your log
  store's history, chunk by chunk). Any time range is a lookup, not a log scan.
- `logsite grafana --store postgres|mysql|mssql`: a Grafana dashboard on the error store.
- `[export]` to BigQuery or Snowflake (beta): each check's new errors, and group snapshots.

## 0.4.1 (2026-10-22)

- [TypeScript and JavaScript](docs/languages.md#typescript-and-javascript): log calls from
  `console`, pino, winston, bunyan and NestJS, matched, linted and grouped; the editor
  extension opens for them too.
- pino and bunyan: `time` in epoch milliseconds (it read as seconds and crashed `logsite
  errors`), and their numeric levels (`50` is error).
- [Sentry](docs/sentry.md) backend: a project's error events, with their exception chains and
  lines of code, grouped and reproduced like errors from logs.
- structlog's `dict_tracebacks`: each frame's line of code is read too.
- [OpenTelemetry](docs/opentelemetry.md): reads OTLP JSON from the Collector's file exporter, both
  log records and the `exception` events on trace spans.
- [GitHub Action](docs/github-action.md): reads your logs on a schedule and opens a draft pull
  request with a reproduction test for each new error, checked with `logsite agent --expect`.
- [Data flow and security](docs/security.md): what logsite reads, keeps on disk, sends, and masks.
- `SECURITY.md`, a code of conduct, and issue templates.
- Releases come with signed build provenance and an SBOM.

## 0.4.0 (2026-10-22)

- **`logsite repro`**: turn a production error into a failing pytest test from what
  production logged (the function to call, the logged values, the dependency to fake), and
  `logsite agent --expect <id>` to check it fails the way production did. A line that moved
  since production ran is found by its code; a trace pasted from a script or notebook keeps
  the caller's line.
- More stack traces read: IPython and Jupyter (and Colab pasted into an issue), Django's
  debug page, tracebacks quoted with an indent, hard-wrapped by a terminal, or with frames
  left out as `...`.
- The project installed as a package (`site-packages/app/...`) counts as its own code.
- Errors that started during a log file read with `--from` are marked new.
- Benchmarks: [repro](bench/repro) (planted bugs, including a day of noisy traffic) and
  [real bugs](bench/swebench) from SWE-bench Verified.
- Licensed under Apache 2.0 (was MIT).

## 0.3.5 (2026-10-21)

- Loki: stack traces that arrive one line per entry are joined from the lines around each error.
- `logsite pipeline --multiline`: Alloy rules that join stack traces before they're shipped.

## 0.3.4 (2026-10-19)

- Grafana data source plugin: error groups, incidents, ranked explanations and history under
  Connections.

## 0.3.2 (2026-10-18)

- Rates, not counts: a busy day isn't a spike. `logsite investigate --report` ranks the
  explanations of an incident with the evidence for and against each.
- Alerts: an evidence report per incident.
- Diagnosis of harder incidents (wait cycles, orphaned work, split brain, alert storms,
  breaker recovery), checked by a benchmark of incidents with known causes.
- Agent mode: Jest and Vitest.
- Stack traces from more runtimes: Node.js, Go fatal errors, wrapped Java messages.

## 0.3.1 (2026-10-18)

- Claude Code plugin, and the `production_context` MCP tool: what a function does in
  production, before an assistant changes it.
- An error that had stopped is no longer reported as back when a wide window re-reads old records.

## 0.3.0 (2026-10-18)

- History store: errors seen before, the fix that worked, and regressions, in the CLI, the
  editor and Grafana (panel columns and annotations).
- `logsite pipeline`: only known low levels get a rule.

## 0.2.1 (2026-10-17)

- The README is a short front page, with the details in `docs/`.

## 0.2.0 (2026-10-17)

- Incidents: `logsite investigate` links error groups and finds the deploy and commits before them.
- CI gate: `logsite errors --since-deploy <ref> --fail-on-new`.
- Agent mode: `logsite agent -- <command>` groups test and build failures by cause.

## 0.1.0 (2026-10-17)

- First release: production counts per log call, a logging linter, Loki and file backends,
  git correlation for spiking lines, and an MCP server.
