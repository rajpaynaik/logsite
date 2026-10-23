# OpenTelemetry

logsite reads OpenTelemetry's OTLP JSON, as the Collector's
[`file` exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/fileexporter)
writes it: no change to your services if they already send OTLP to a Collector.

- **Logs**: each log record, with its attributes and its resource's (`service.name`,
  `code.filepath` / `code.file.path`, `exception.type`, `exception.stacktrace`...), the
  severity, and the trace and span ids.
- **Traces**: each `exception` event on a span is an error record (the exception's type,
  message and stack trace, the span's name and ids), so a service that's traced but has no
  log pipeline gets its errors grouped too.

## Export to a file

Add a `file` exporter to the Collector's pipelines (the contrib distribution has it):

```yaml
exporters:
  file:
    path: /var/log/otel/export.json
    rotation:
      max_megabytes: 100

service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [file]     # next to your existing exporters
    traces:
      receivers: [otlp]
      exporters: [file]
```

Then read it like any log file:

```bash
logsite errors --from /var/log/otel/export.json
logsite repro --from /var/log/otel/export.json --write-test
```

or point the config at it, for the editor, the MCP server and `logsite investigate`:

```toml
[backend]
type = "file"
path = "/var/log/otel/export.json"
```

For counts and cost per log call, the records need the code location: OpenTelemetry's Python
logging instrumentation and the Java agent add `code.filepath` / `code.lineno` (or the newer
`code.file.path` / `code.line.number`); in Go, slog's `source` attribute works too.
