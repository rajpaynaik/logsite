"""Grouping, on lines as Grafana Play's Loki returned them (OpenTelemetry demo services,
2026-10-25): Spring Boot text, Python text with a traceback split into its own entry, and
JSON with a chained exc_info."""

import json
from datetime import datetime, timedelta, timezone

from logsite.errors import AppFiles, group_errors, normalize, strip_prefix
from logsite.models import LogRecord

T0 = datetime(2026, 10, 25, 2, 34, 55, tzinfo=timezone.utc)
APP = AppFiles([])

SPRING = ("2026-10-25 02:50:10.426 ERROR --- [   scheduling-{t}] "
          "c.e.inventoryservice.InventoryService    : REQ{req} Failed to {op} for product "
          "{product} ({sku}). Error: {reason}")  # fmt: skip
PY_LINE = ("2026-10-25 02:34:55,056 ERROR [opentelemetry.context] [__init__.py:157] "
           "[trace_id={trace} span_id=8b6926a9f23ab28c resource.service.name=chatservice "
           "trace_sampled=True] - Failed to detach context")  # fmt: skip
PY_TRACE = """Traceback (most recent call last):
  File "/usr/local/lib/python3.10/site-packages/opentelemetry/context/__init__.py", line 155, in detach
    _RUNTIME_CONTEXT.detach(token)
  File "/usr/local/lib/python3.10/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
    self._current_context.reset(token)
ValueError: <Token var=<ContextVar name='current_context' default={} at 0xffff89d72d90> at 0xfffefda311c0> was created in a different Context"""  # noqa: E501
CHAINED = """Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/opentelemetry/trace/__init__.py", line 589, in use_span
    yield span
  File "/app/banking_chatservice/downstream.py", line 54, in list_transactions
    raise PermissionError("That account does not belong to this customer")
PermissionError: That account does not belong to this customer

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/__init__.py", line 155, in detach
    _RUNTIME_CONTEXT.detach(token)
  File "/usr/local/lib/python3.12/site-packages/opentelemetry/context/contextvars_context.py", line 53, in detach
    self._current_context.reset(token)
ValueError: <Token var=<ContextVar name='current_context' default={} at 0xffffa6af9cb0> at 0xffff74d6c2c0> was created in a different Context"""  # noqa: E501


def rec(message, at=T0, **fields):
    return LogRecord(at, message, {"service_name": "chatservice", **fields})


def test_prefixes_and_values_in_real_lines():
    line = SPRING.format(t=1, req="0534", op="mark low stock", sku="OLJCESPC7Z",
                         product="National Park Foundation Explorascope",
                         reason="Inventory record locked by another transaction")  # fmt: skip
    assert normalize(strip_prefix(line)) == (
        "<id> Failed to mark low stock for product <name> (<id>). Error: Inventory record "
        "locked by another transaction")  # fmt: skip
    assert strip_prefix(PY_LINE.format(trace="afc7")) == "Failed to detach context"
    assert strip_prefix("2026-10-14 10:00:05,123 ERROR 1234 --- [main] com.acme.Pay : boom") == (
        "boom")  # fmt: skip
    # numbers inside words stay; HTTP phrases stay; numbers and "sda1"'s 1 go
    assert normalize("agento11y hook on k8s") == "agento11y hook on k8s"
    assert normalize("upstream 502 Bad Gateway after 3.5s on sda1") == (
        "upstream <n> Bad Gateway after <n>s on sda<n>")  # fmt: skip


def test_names_and_codes_dont_make_a_group_each():
    products = [("Red Flashlight", "LS4PSXUNUM"), ("Roof Binoculars", "2ZYFJ3GM2N"),
                ("Solar System Color Imager", "66VCHSJNUP"), ("Eclipsmart Travel Refractor",
                                                              "1YMWWN1N4O")]  # fmt: skip
    ops = ["mark low stock", "schedule restock"]
    reasons = ["Database connection timeout after 6 seconds",
               "Inventory record locked by another transaction"]  # fmt: skip
    records = [rec(SPRING.format(t=i % 3, req=f"0{500 + i}", op=op, product=p, sku=sku,
                                 reason=reason), service_name="inventoryservice")
               for i, (op, (p, sku), reason) in enumerate(
                   (o, x, r) for o in ops for x in products for r in reasons)]  # fmt: skip
    groups = group_errors(records, [], APP)
    assert len(records) == 16 and len(groups) == 4  # 2 operations x 2 reasons
    titles = sorted(g.title for g in groups.values())
    assert titles[0] == ("<id> Failed to mark low stock for product <name> (<id>). Error: "
                         "Database connection timeout after <n> seconds")  # fmt: skip


def test_a_traceback_in_its_own_entry_joins_its_error_line():
    # as Grafana Play sends them: line, trace (another stream, another level label), and a
    # second request's pair interleaved, its trace 3.6 s late
    records = [
        rec(PY_LINE.format(trace="afc7881f86ae7b6d4b21ef604774049c"), T0, detected_level="error"),
        rec(PY_TRACE, T0 + timedelta(microseconds=1), detected_level="unknown"),
        rec(PY_LINE.format(trace="bbb7881f86ae7b6d4b21ef604774049c"),
            T0 + timedelta(microseconds=5), detected_level="error"),  # fmt: skip
        rec(PY_TRACE, T0 + timedelta(seconds=3.6), detected_level="info"),
        rec(PY_LINE.format(trace="ccc7881f86ae7b6d4b21ef604774049c"),
            T0 + timedelta(minutes=5), detected_level="error"),  # no trace ever came
    ]  # fmt: skip
    groups = group_errors(records, [], APP)
    by_title = {g.title: g for g in groups.values()}
    assert sorted(g.count for g in groups.values()) == [1, 2]
    joined = next(g for t, g in by_title.items() if t.startswith("ValueError"))
    assert joined.count == 2 and joined.trace_ids == {
        "afc7881f86ae7b6d4b21ef604774049c",
        "bbb7881f86ae7b6d4b21ef604774049c",
    }
    assert by_title["Failed to detach context"].count == 1
    # a trace too long after its line (or from another service) stays on its own
    late = [rec(PY_LINE.format(trace="d" * 32)), rec(PY_TRACE, T0 + timedelta(seconds=30))]
    other = [rec(PY_LINE.format(trace="e" * 32)), rec(PY_TRACE, service_name="cartservice")]
    assert sum(1 for _ in group_errors(late, [], APP)) == 2
    assert sum(1 for _ in group_errors(other, [], APP)) == 2


def test_library_noise_on_top_of_a_real_error():
    line = json.dumps({"asctime": "2026-10-25 02:48:33,267", "levelname": "ERROR",
                       "name": "opentelemetry.context", "message": "Failed to detach context",
                       "exc_info": CHAINED})  # fmt: skip
    from logsite.backends.base import parse_line

    (g,) = group_errors([parse_line(line, timestamp=T0)], [], APP).values()
    assert g.title == "PermissionError: That account does not belong to this customer"
    assert g.exc_type == "PermissionError" and "ValueError" in g.wrapped_in
    # the noise alone keeps its own title
    alone = json.dumps({"levelname": "ERROR", "message": "Failed to detach context",
                        "exc_info": PY_TRACE})  # fmt: skip
    (g,) = group_errors([parse_line(alone, timestamp=T0)], [], APP).values()
    assert g.title.startswith("ValueError: <Token var=")


# --- Go's logfmt and klog, and Next.js: lines from Grafana Play that weren't seen as errors ----


def test_logfmt_and_klog_are_parsed():
    from logsite.backends.base import parse_line
    from logsite.errors import error_field
    from logsite.tracebacks import is_error, level_of

    k6 = parse_line(
        'time="2026-10-25T03:04:02Z" level=error msg="process with PID 3199 '
        'unexpectedly ended: signal: killed" category=browser elapsed="0 ms"'
    )
    assert is_error(k6) and k6.message.startswith("process with PID 3199")
    assert k6.timestamp == datetime(2026, 10, 25, 3, 4, 2, tzinfo=timezone.utc)
    assert k6.fields["category"] == "browser" and k6.fields["elapsed"] == "0 ms"
    alloy = parse_line(
        'ts=2026-10-25T02:30:35.16023561Z level=error msg="failed to rejoin list '
        'of peers" service=cluster err="failed to join memberlist: i/o timeout"'
    )
    assert is_error(alloy) and error_field(alloy) == "failed to join memberlist: i/o timeout"
    klog = parse_line('E1025 03:04:55.030320       1 reflector.go:227] "Failed to watch" '
                      'err="failed to list *v1.VolumeSnapshotClass: not found" '
                      'logger="UnhandledError"')  # fmt: skip
    assert is_error(klog) and klog.message == "Failed to watch"
    assert (klog.source_file, klog.source_line) == ("reflector.go", 227)
    assert klog.fields["logger"] == "UnhandledError" and error_field(klog).startswith("failed")
    assert level_of(parse_line("I1025 03:00:00.000001       1 main.go:10] starting")) == "info"
    # not errors, not logfmt
    assert not is_error(parse_line('level="info" schema="orders" errors="0" mysql_errno="0"'))
    plain = parse_line("a plain line with a = sign")
    assert plain.fields == {} and plain.message == "a plain line with a = sign"


def test_hclog_is_parsed():
    # Consul's CI output (2026-10-27): its test logger's time, then hclog's format
    from logsite.backends.base import parse_line
    from logsite.errors import error_field
    from logsite.tracebacks import is_error

    r = parse_line('04:38.789 [ERROR] TestAgent.server: error performing anti-entropy sync of '
                   'federation state: error="shutdown waiting for leader"')  # fmt: skip
    assert is_error(r) and r.message == "error performing anti-entropy sync of federation state"
    assert r.fields["logger"] == "TestAgent.server"
    assert error_field(r) == "shutdown waiting for leader"
    info = parse_line('2026-10-27T10:04:38.995Z [INFO]  runnervmtai0g.leader: started routine: '
                      'routine="federation state anti-entropy"')  # fmt: skip
    assert not is_error(info) and info.message == "started routine"
    assert info.timestamp == datetime(2026, 10, 27, 10, 4, 38, 995000, tzinfo=timezone.utc)
    # no pairs at the end: the message is all of it
    r = parse_line("04:38.789 [ERROR] agent: failed: http: server closed")
    assert r.message == "failed: http: server closed"


def test_go_test_output_and_slog_source():
    # Prometheus' CI output (2026-10-27): slog's text handler, logged through t.Log
    from logsite.backends.base import parse_line

    r = parse_line(
        "    reload_test.go:204: time=2026-10-27T20:39:27.865Z level=ERROR "
        'source=main.go:1477 msg="Failed to generate checksum" err="yaml: line 5"\r'
    )
    assert r.message == "Failed to generate checksum" and r.fields["err"] == "yaml: line 5"
    assert (r.source_file, r.source_line) == ("main.go", 1477)
    plain = parse_line("    foo_test.go:12: just text")
    assert plain.fields == {} and plain.message == "    foo_test.go:12: just text"


def test_actions_annotations_dont_hide_errors():
    # Kafka's CI output (2026-10-27)
    from logsite.tracebacks import join_multiline

    lines = [
        '##[error]Exception in thread "main" java.io.IOException: Server returned HTTP 500',
        "\tat org.gradle.wrapper.Download.download(SourceFile:1)",
    ]
    (event,) = join_multiline(lines)
    assert event.startswith('Exception in thread "main" java.io.IOException')


def test_python_syntax_warnings_stay_quiet(recwarn):
    from logsite.index import index_source

    index_source('import logging\nlogging.error("a\\|b %s", x)\n', "old.py")
    assert not [w for w in recwarn if issubclass(w.category, SyntaxWarning)]


def test_pytest_captured_logs_are_parsed():
    from logsite.backends.base import parse_line
    from logsite.tracebacks import is_error, join_multiline

    lines = ["------------------------------ Captured log call -------------------------------",
             "INFO     app.orders:orders.py:30 placing order 7",
             "ERROR    app.orders:orders.py:42 payment failed for order 7",
             "Traceback (most recent call last):",
             '  File "/src/app/payments.py", line 12, in charge',
             '    raise TimeoutError("gateway timed out")',
             "TimeoutError: gateway timed out"]  # fmt: skip
    header, info, error = (parse_line(e) for e in join_multiline(lines))
    assert header.fields == {} and not is_error(info)
    assert is_error(error) and error.message.startswith("payment failed for order 7\n")
    assert (error.source_file, error.source_line) == ("orders.py", 42)
    assert error.fields["name"] == "app.orders"
    # log_cli with a time first
    live = parse_line("2026-10-28 10:00:01.123 WARNING  root:conftest.py:9 slow")
    assert (live.message, live.source_line) == ("slow", 9)
    assert live.timestamp == datetime(2026, 10, 28, 10, 0, 1, 123000, tzinfo=timezone.utc)
    assert parse_line("ERROR something: else").fields == {}


def test_one_line_titles_from_multiline_errors():
    from logsite.backends.base import parse_line

    r = parse_line(
        'level=error msg="failed to rejoin" err="1 error occurred:\\n\\t* timeout\\n\\n"'
    )
    (g,) = group_errors([r], [], APP).values()
    assert g.title == "failed to rejoin: 1 error occurred: * timeout"


def test_a_nextjs_unhandled_rejection():
    from logsite.models import LogRecord
    from logsite.tracebacks import extract_error, is_error, join_multiline

    lines = [" ⨯ unhandledRejection:  Error [AbortError]: This operation was aborted",
             "    at ServerResponse.r (.next/server/pages/api/chat.js:1:9782) {",
             "  code: 20,", "  INDEX_SIZE_ERR: 1,", "}"]  # fmt: skip
    event = next(join_multiline(lines))
    info = extract_error(LogRecord(T0, event))
    assert info.root.type == "Error" and "AbortError" in info.root.message
    assert [(f.file, f.line) for f in info.root.frames] == [(".next/server/pages/api/chat.js", 1)]
    assert is_error(LogRecord(T0, lines[0]))  # alone too: its frames may be in other entries
