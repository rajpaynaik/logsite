"""OpenTelemetry's OTLP JSON: what the Collector's ``file`` exporter writes, one export
request per line (``{"resourceLogs": [...]}`` or ``{"resourceSpans": [...]}``).

Each log record becomes a :class:`LogRecord` whose fields are its attributes and its
resource's (``service.name``, ``code.filepath``, ``exception.stacktrace``...), so matching,
error grouping and timelines read them like any other JSON log. From traces, each
``exception`` event on a span becomes an error record: teams with tracing and no log
pipeline get their errors grouped too.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import LogRecord

# The newer semantic conventions' names, as the older ones logsite reads
_RENAMED = {
    "code.file.path": "code.filepath",
    "code.line.number": "code.lineno",
    "code.function.name": "code.function",
}
# severityNumber ranges: 1-4 TRACE, 5-8 DEBUG, 9-12 INFO, 13-16 WARN, 17-20 ERROR, 21-24 FATAL
_SEVERITY = ((21, "FATAL"), (17, "ERROR"), (13, "WARN"), (9, "INFO"), (5, "DEBUG"), (1, "TRACE"))


def is_otlp(data: object) -> bool:
    return isinstance(data, dict) and ("resourceLogs" in data or "resourceSpans" in data)


def otlp_records(data: dict, size: int | None = None) -> list[LogRecord]:
    """The log records (and span exception events) in one OTLP JSON export request."""
    out = []
    for rl in data.get("resourceLogs") or []:
        resource = _attributes((rl.get("resource") or {}).get("attributes"))
        for sl in rl.get("scopeLogs") or rl.get("instrumentationLibraryLogs") or []:
            scope = (sl.get("scope") or sl.get("instrumentationLibrary") or {}).get("name")
            for lr in sl.get("logRecords") or []:
                out.append(_log_record(lr, resource, scope))
    for rs in data.get("resourceSpans") or []:
        resource = _attributes((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for span in ss.get("spans") or []:
                for event in span.get("events") or []:
                    if event.get("name") == "exception":
                        out.append(_exception_event(event, span, resource))
    if size is not None and out:
        for r in out:  # the batch's stored size, shared out: cost per line stays comparable
            r.size = size // len(out)
    return out


def _log_record(lr: dict, resource: dict, scope: str | None) -> LogRecord:
    fields = {**resource, **_attributes(lr.get("attributes"))}
    level = lr.get("severityText") or _severity(lr.get("severityNumber"))
    if level:
        fields["severity"] = level
    _ids(fields, lr)
    if scope:
        fields.setdefault("logger", scope)
    body = _value(lr.get("body"))
    if isinstance(body, dict):  # a structured body: its keys are fields too
        fields = {**body, **fields}
        body = body.get("message") or body.get("msg") or json.dumps(body)
    message = "" if body is None else str(body)
    if not message and fields.get("exception.message"):
        message = str(fields["exception.message"])
    ts = _time(lr.get("timeUnixNano")) or _time(lr.get("observedTimeUnixNano"))
    return LogRecord(timestamp=ts, message=message, fields=fields)


def _exception_event(event: dict, span: dict, resource: dict) -> LogRecord:
    fields = {**resource, **_attributes(span.get("attributes")),
              **_attributes(event.get("attributes")), "severity": "ERROR"}  # fmt: skip
    _ids(fields, span)
    if span.get("name"):
        fields["span.name"] = span["name"]
    message = str(fields.get("exception.message") or fields.get("exception.type") or "exception")
    ts = _time(event.get("timeUnixNano")) or _time(span.get("endTimeUnixNano"))
    return LogRecord(timestamp=ts, message=message, fields=fields)


def _ids(fields: dict, obj: dict) -> None:
    for key, name in (("traceId", "trace_id"), ("spanId", "span_id")):
        if obj.get(key):
            fields[name] = obj[key]


def _attributes(attrs: list | None) -> dict:
    out = {}
    for kv in attrs or []:
        if isinstance(kv, dict) and "key" in kv:
            key = _RENAMED.get(kv["key"], kv["key"])
            out[key] = _value(kv.get("value"))
    return out


def _value(v: object) -> object:
    """An OTLP ``AnyValue`` as a plain value."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])  # a string in OTLP JSON, to keep 64 bits
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "arrayValue" in v:
        return [_value(x) for x in (v["arrayValue"] or {}).get("values") or []]
    if "kvlistValue" in v:
        return _attributes((v["kvlistValue"] or {}).get("values"))
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def _severity(number: object) -> str | None:
    if not isinstance(number, int) or number <= 0:
        return None
    return next(name for low, name in _SEVERITY if number >= low)


def _time(nanos: object) -> datetime | None:
    try:
        n = int(nanos)  # a string in OTLP JSON
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n / 1e9, tz=timezone.utc)
