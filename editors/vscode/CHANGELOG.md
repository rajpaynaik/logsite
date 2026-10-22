# Changelog

## 0.3.5

- No extension changes; released with logsite 0.3.5, which joins stack traces split into separate Loki entries (`logsite pipeline --multiline`, and when reading them).

## 0.3.4

- No extension changes; released with logsite 0.3.4, which adds the Grafana data source plugin.

## 0.3.3

- The extension page now explains the error summaries: error groups on the line that raised them, and `logsite errors`, `investigate` and `--report` in the terminal.

## 0.3.2

- Stack traces from more runtimes (Node.js, Go fatal errors) and wrapped Java messages are read in production logs.

## 0.3.1

- Fix: an error that had stopped is no longer reported as back when a wide window re-reads its old records.

## 0.3.0

- History store: errors seen before, the fix that worked, and regressions, in the editor's language server and the CLI.

## 0.2.1

- The project README is a short front page, with the details in the docs.

## 0.2.0

The extension itself is unchanged; it uses logsite 0.2.0, which adds:

- `logsite investigate`: error groups linked into incidents, with the deploy and suspect commits.
- `logsite agent -- <command>`: test and build failures grouped by cause, with what changed
  since the last run, for AI coding assistants.
- A CI gate: `logsite errors --fail-on-new` / `--fail-on-spike`.
- Better Java and Go error summaries: framework wrappers skipped, panics pointing at your code.

## 0.1.0 (preview)

First release.

- Production counts and cost per month above every log call, with a spike marker.
- Hover: trend vs the previous window, cost, and recent commits around the call.
- Production errors: a code lens and hover on the line that raised them, grouped by cause.
- Log-quality lint as you type (LL001–LL008), including context rules and severity checks.
- Quick fix: move values out of the message into `extra=` / keyword arguments (LL008).
- Python (`logging`, structlog, loguru), Java (SLF4J, Log4j 2, java.util.logging, Lombok)
  and Go (slog, zap, zerolog, logrus, logr, klog).
- Finds the server through the Python interpreter selected in VS Code, then `PATH`, and
  restarts when you pick another interpreter.
- Each file uses the nearest `logsite.toml`, so a monorepo or a parent folder works.
