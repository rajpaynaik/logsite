# Languages and logging libraries

## structlog and loguru

logsite works out which library each logger comes from (`log = structlog.get_logger()`,
`from loguru import logger`, `std = logging.getLogger(__name__)`, whatever the variable is
called) and applies the right conventions:

| | `logging` | structlog | loguru |
|---|---|---|---|
| Fields (LL006, LL008 fix) | `extra={...}` | keyword arguments + `.bind(...)` | keyword arguments + `.bind(...)` |
| Traceback (LL003) | `exc_info=True` / `.exception()` | `exc_info=True` / `.exception()` | `.opt(exception=True)` / `.exception()` |
| Context added automatically | logging filters | `bind_contextvars`, processors | `contextualize`, `configure(extra=)` |
| Matching production logs | `pathname` + `lineno` | `CallsiteParameterAdder` (`pathname`, `lineno`) | `serialize=True` JSON (`record.file`, `record.line`) |

For exact matching, add the call site to structlog's processors:

```python
structlog.processors.CallsiteParameterAdder(
    {CallsiteParameter.PATHNAME, CallsiteParameter.LINENO}
)
```

loguru includes it automatically with `logger.add(sink, serialize=True)`.

## Java and Go

logsite reads Java and Go code with [tree-sitter](https://tree-sitter.github.io) grammars
(installed with logsite), so counts, cost, the error summary, spikes, the Grafana dashboard,
Alloy rules and the editor work the same as for Python. A repo can mix all three.

| | Java | Go |
|---|---|---|
| **Loggers** | SLF4J, Log4j 2, java.util.logging, Commons Logging, Flogger, JBoss Logging; Lombok `@Slf4j` & co; fluent `log.atInfo().addKeyValue(...).log(...)` | `log/slog`, zap (and sugared), zerolog chains, logrus (`WithField(...)`), logr, klog, the standard `log` |
| **Found by** | `Logger` fields and variables, `LoggerFactory.getLogger(...)`, Lombok annotations, `log`/`LOGGER` in files that import a logging package | logging imports, and fields, parameters and variables declared or built as loggers (`Zap *zap.Logger`, `lg := slog.New(h)`) |
| **Templates** | `{}` (and `{0}`), `String.format` / `formatted` | the message, `%v`-style verbs for `Infof`-style calls |
| **Caller fields** matched | `caller_file_name` + `caller_line_number` (+ `caller_class_name` for the package), Log4j 2 `source`, ECS `log.origin.*` | slog `AddSource` (`source.file` / `source.line`), zap and zerolog `caller` (`file.go:42`), logrus `file` |
| **Stack traces** (`logsite errors`) | `at com.acme.X.y(X.java:42)`, `Caused by:`, `... 3 more`, in `stack_trace` (logstash-logback-encoder), `error.stack_trace` (ECS) or plain text | panics (`panic:` + the goroutine), zap's `stacktrace` |

Lint rules for both: LL001 (string concatenation, `String.format`, `fmt.Sprintf` in the
message), LL002 (secrets and personal data among the logged values and field keys), LL003
(Java: an error logged in a `catch` without the exception, or with only `e.getMessage()`;
Go: an error logged in `if err != nil` without `err`), LL004, LL005, LL007 (failure words
at info; a broad `catch` or an `if err != nil` that logs at info and goes on) and the
production rules (LL100–LL105). Context rules (LL006), LL008 and its quick fix are Python
only for now.

Go writes panics without a timestamp, so they count in every time window; ship them through
something that adds one (a log shipper, `slog` with a recover handler).
