"""OpenTelemetry's OTLP JSON, as the Collector's file exporter writes it."""

import json

from logsite.backends.file import ALL_TIME, FileBackend
from logsite.cli import main
from logsite.tracebacks import extract_error

CHECKOUT = """\
import logging

log = logging.getLogger(__name__)


def charge(order):
    try:
        return order["total"] / order["items"]
    except ZeroDivisionError:
        log.exception("charge failed for %s", order["id"])
        raise
"""

STACK = """\
Traceback (most recent call last):
  File "/srv/app/checkout.py", line 8, in charge
    return order["total"] / order["items"]
ZeroDivisionError: division by zero"""

T0 = 1792656000_000000000  # 2026-10-22 08:00 UTC, in nanoseconds


def s(value):
    return {"stringValue": value}


def logs_line(n_errors=3):
    records = [{
        "timeUnixNano": str(T0 + i * 1_000_000_000),
        "severityNumber": 17,
        "severityText": "ERROR",
        "body": s(f"charge failed for ORD-{i}"),
        "traceId": f"{i + 1:032x}",
        "spanId": f"{i:016x}",
        "attributes": [
            {"key": "code.file.path", "value": s("/srv/app/checkout.py")},
            {"key": "code.line.number", "value": {"intValue": "10"}},
            {"key": "exception.type", "value": s("ZeroDivisionError")},
            {"key": "exception.message", "value": s("division by zero")},
            {"key": "exception.stacktrace", "value": s(STACK)},
        ],
    } for i in range(n_errors)]  # fmt: skip
    records.append({
        "timeUnixNano": str(T0 + 9_000_000_000),
        "severityNumber": 9,
        "body": s("charging ORD-9"),
        "attributes": [{"key": "code.filepath", "value": s("/srv/app/checkout.py")},
                       {"key": "code.lineno", "value": {"intValue": "7"}}],
    })  # fmt: skip
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [{"key": "service.name", "value": s("checkout")}]},
        "scopeLogs": [{"scope": {"name": "app.checkout"}, "logRecords": records}],
    }]})  # fmt: skip


def spans_line():
    return json.dumps({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": s("checkout")}]},
        "scopeSpans": [{"spans": [{
            "traceId": "ab" * 16, "spanId": "cd" * 8, "name": "POST /charge",
            "endTimeUnixNano": str(T0 + 20_000_000_000),
            "status": {"code": 2},
            "events": [
                {"name": "retry", "timeUnixNano": str(T0)},
                {"name": "exception", "timeUnixNano": str(T0 + 19_000_000_000), "attributes": [
                    {"key": "exception.type", "value": s("ZeroDivisionError")},
                    {"key": "exception.message", "value": s("division by zero")},
                    {"key": "exception.stacktrace", "value": s(STACK)},
                ]},
            ],
        }]}],
    }]})  # fmt: skip


def test_log_records_and_span_exceptions_become_records(tmp_path):
    path = tmp_path / "otel.json"
    path.write_text(logs_line() + "\n" + spans_line() + "\n")
    records = FileBackend(path).query_logs(ALL_TIME)
    assert len(records) == 5  # 4 log records, 1 exception event (not the "retry" event)
    first = records[0]
    assert first.message == "charge failed for ORD-0"
    assert (first.source_file, first.source_line) == ("/srv/app/checkout.py", 10)  # new names
    assert first.fields["service.name"] == "checkout" and first.fields["severity"] == "ERROR"
    assert first.fields["trace_id"] == f"{1:032x}" and first.timestamp.isoformat().startswith(
        "2026-10-22T08:00:00"
    )
    assert records[3].fields["severity"] == "INFO" and records[3].source_line == 7
    event = records[4]
    assert event.message == "division by zero" and event.fields["span.name"] == "POST /charge"
    info = extract_error(event)
    assert info.final.type == "ZeroDivisionError" and info.final.frames[-1].line == 8
    assert len(FileBackend(path).query_errors(ALL_TIME)) == 4


def test_errors_from_an_otel_export_are_grouped_at_the_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "checkout.py").write_text(CHECKOUT)
    (tmp_path / "otel.json").write_text(logs_line() + "\n" + spans_line() + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    assert main(["errors", "--from", "otel.json", "--format", "json", "--no-history"]) == 0
    (group,) = json.loads(capsys.readouterr().out)["groups"]
    assert group["type"] == "ZeroDivisionError" and group["count"] == 4
    assert group["raised_at"] == "app/checkout.py:8" and group["raised_in"] == "charge"
    assert group["services"] == {"checkout": 4}


def test_not_otlp_is_read_as_json_lines(tmp_path):
    path = tmp_path / "app.log"
    path.write_text(json.dumps({"resource": "x", "message": "hello"}) + "\n")
    (record,) = FileBackend(path).query_logs(ALL_TIME)
    assert record.message == "hello"
