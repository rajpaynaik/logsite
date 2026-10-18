# Roadmap

- [x] **v0.1**: log index, Loki + file backends, code-lens counts, quality linter, CLI
- [x] **Released**: [PyPI](https://pypi.org/project/logsite/) (0.1.0; 0.2.0 with incidents, the CI gate and agent mode; 0.2.1) and the [VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=rajpaynaik.logsite) ([`editors/vscode`](../editors/vscode); releases: [RELEASING.md](../RELEASING.md))
- [ ] Open VSX (VSCodium, Cursor, Windsurf)
- [x] Git correlation: when a line spikes, show the commits that touched it
- [x] MCP server: let AI assistants ask "which log lines spiked after the last deploy?"
- [x] OpenSearch and CloudWatch Logs backends
- [ ] Kafka module: consumer lag, DLQ messages and errors per topic
- [x] Server-side aggregation for Loki and OpenSearch (`aggregate = true`) for large volumes
- [x] structlog and loguru: indexing, library-aware lint rules and quick fixes, loguru JSON parsing
- [x] Cost per log line: bytes and estimated spend per line (not just counts), shown in the code lens and as `logsite cost`, with a configurable price per GB
- [ ] Audit event coverage: declare sensitive code paths (e.g. functions that change permissions, money or personal data) and the audit events they must emit; lint flags paths that don't, and checks that audit events never carry PII
- [x] Deploy-aware comparison: `logsite spikes --since-deploy <sha|tag>` compares error and volume per line before vs after a real release instead of a rolling window
- [x] Leak scanning in production logs: sample real log contents for secrets and personal data (tokens, card numbers, emails) and point to the code line that emitted each leak
- [x] Error summary: production errors grouped by cause (`logsite errors`, the `error_summary` MCP tool, and a code lens on the line that raised), new and spiking first, with `--since-deploy`
- [x] Grafana dashboard generator: `logsite grafana` writes a dashboard of log cost and errors per code line from Loki, with source links
- [x] Grafana Alloy rules: `logsite pipeline` drops or samples noisy log calls before they're shipped, sized and priced from production volume
- [x] Alert enrichment: `logsite alert-server` receives Grafana's webhook contact point and posts the error summary for the alert's window to Slack or a webhook
- [x] "Error groups" dashboard panel (`logsite grafana --error-groups`) reading `logsite alert-server`'s `/errors` through the Infinity data source
- [ ] Grafana app plugin: the same views as pages inside Grafana, listed in the plugin catalog
- [x] Incident investigation: `logsite investigate` links error groups into incidents (shared trace ids, starting together), with the cause, effects, the deploy before it and suspect commits; MCP tool `investigate`
- [x] CI gate after a deploy: `logsite errors --since-deploy <ref> --fail-on-new` (and `--fail-on-spike`) exits 1
- [x] Agent mode: `logsite agent -- <command>` groups test and build failures by cause (pytest, go, Maven/Gradle, tsc, javac...) and shows what changed since the last run
- [ ] Agent mode, more: Jest / Vitest, Rust (cargo test), a Claude Code hook that routes test commands automatically
- [x] History store: "first seen ever", regressions with the fix that worked before ([history](history.md))
- [ ] Incident investigation, more: affected services and tenants, and a few full request timelines per incident
- [ ] More MCP tools for incident investigation: recent messages for a line, so AI assistants can do root-cause analysis on logsite data
- [x] Severity correctness: flag log levels that don't match the event, statically (`info`/`debug` inside `except`, or messages like "failed"/"timeout" logged below `warning`) and from production (INFO lines whose messages look like failures, ERROR lines firing constantly at high volume), so failures aren't hidden from alerting
- [x] Context rules in code: required fields by situation (`[context]`: errors, retry loops, dependency calls), with credit for context injected automatically (logging filters, structlog contextvars, LoggerAdapter, OpenTelemetry)
- [x] Context coverage in production: per-line field coverage measured from real records (e.g. `tenant_id` present in 62% of records), to catch context lost in threads, async tasks and background jobs
- [x] Structured messages: flag values interpolated into the message (`"order %s failed"`) and offer an editor quick fix that moves them into `extra=` fields, plus a per-service structure score
- [x] Request timeline MCP tool: given a `trace_id`, every log line from every service for that request, in order, each linked to the code line that wrote it

### Other languages

The parts that read production logs (backends, matching by file and line, error grouping,
timelines, cost, leaks) don't depend on the language; each language needs an indexer for its
log calls, lint rules that fit its loggers, and a stack trace parser.

- [x] **Java**: SLF4J / Log4j 2 / java.util.logging / Commons / Flogger, Lombok, fluent APIs;
  caller fields from logstash-logback-encoder, Log4j 2 and ECS; Java stack traces in the
  error summary
- [x] **Go**: slog, zap, zerolog, logrus, logr, klog, `log`; caller fields; panics and zap
  stack traces in the error summary
- [x] Editor support for Java and Go through the same language server
- [ ] **JavaScript / TypeScript**: pino, winston, bunyan and `console.*` in Node; child
  loggers (`logger.child({ requestId })`); Node stack traces (`at charge (src/checkout.ts:42:7)`),
  with source maps to map built JavaScript back to TypeScript lines
- [ ] Java and Go: context rules (MDC, `context.Context` fields) and the structured-message
  quick fix
