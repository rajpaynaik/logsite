# Changelog

logsite follows [semantic versioning](https://semver.org). Before 1.0, a minor version
(0.x.0) can change commands, config or JSON output; a patch version (0.x.y) doesn't.
The VS Code extension has [its own changelog](editors/vscode/CHANGELOG.md).

## Unreleased

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
