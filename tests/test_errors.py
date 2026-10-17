import json
import traceback
from datetime import datetime, timedelta, timezone

import pytest
from test_backends import FakeLogs, stub_server

from logsite.backends import (
    CloudWatchBackend,
    FileBackend,
    LokiBackend,
    OpenSearchBackend,
    TimeRange,
    parse_line,
)
from logsite.cli import main
from logsite.errors import AppFiles, group_errors, normalize, summarize_errors
from logsite.index import index_source
from logsite.lint import LintConfig
from logsite.models import LogRecord
from logsite.tracebacks import Frame, extract_error, is_error, join_multiline, parse_traceback

NOW = datetime.now(timezone.utc)

CHAINED = """\
Traceback (most recent call last):
  File "/srv/app/pricing.py", line 2, in convert
    return order["amount"] * RATES[order["currency"]]
                             ~~~~~^^^^^^^^^^^^^^^^^^^
KeyError: 'EUR'

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/srv/app/checkout.py", line 19, in price
    raise PricingError(f"can't price order {order['id']}") from e
app.checkout.PricingError: can't price order 9001
"""


def timeout_tb(line=9, sku="SKU-1"):
    return f"""\
Traceback (most recent call last):
  File "/srv/app/checkout.py", line 11, in charge
    inventory.reserve(order["sku"], 1)
  File "/srv/app/inventory.py", line {line}, in reserve
    return client.post(url, timeout=3.5)
  File "/usr/lib/python3.12/site-packages/httpx/_client.py", line 1145, in post
    return self.request("POST", url)
TimeoutError: POST http://inventory/{sku} timed out after 3.5s
"""


APP = AppFiles(["app/checkout.py", "app/inventory.py", "app/pricing.py"])


def rec(minutes_ago=1, message="payment failed", **fields):
    fields.setdefault("level", "ERROR")
    return LogRecord(NOW - timedelta(minutes=minutes_ago), message, fields)


# --- parsing ----------------------------------------------------------------------------


def test_parse_traceback_reads_the_chain_root_cause_first():
    root, final = parse_traceback(CHAINED)
    assert root.title == "KeyError: 'EUR'"
    assert root.frames == [
        Frame(
            "/srv/app/pricing.py", 2, "convert", 'return order["amount"] * RATES[order["currency"]]'
        )
    ]  # the ^^^^ marker line isn't mistaken for code
    assert final.type == "app.checkout.PricingError" and final.message == "can't price order 9001"


def test_parse_traceback_of_a_real_exception():
    def boom():
        raise ValueError("bad value 3")

    try:
        boom()
    except ValueError:
        text = traceback.format_exc()
    (exc,) = parse_traceback(text)
    assert exc.title == "ValueError: bad value 3"
    assert (
        exc.frames[-1].function == "boom"
        and exc.frames[-1].code == 'raise ValueError("bad value 3")'
    )


def test_library_frames():
    assert Frame("/usr/lib/python3.12/site-packages/httpx/_client.py", 1, "post").library == "httpx"
    assert Frame("/usr/lib/python3.12/socket.py", 1, "create_connection").library == "socket"
    assert Frame("<frozen runpy>", 1, "_run").is_library
    assert not Frame("/srv/app/checkout.py", 1, "charge").is_library


def test_extract_error_from_every_shape():
    text = extract_error(rec(exc_info=CHAINED))
    assert text.final.type == "app.checkout.PricingError" and text.text == CHAINED
    assert extract_error(rec(message="failed\n" + CHAINED)).root.type == "KeyError"
    ecs = extract_error(rec(**{"error.type": "KeyError", "error.message": "'EUR'"}))
    assert ecs.final.title == "KeyError: 'EUR'" and ecs.text is None
    otel = extract_error(rec(**{"exception.stacktrace": timeout_tb()}))
    assert otel.final.type == "TimeoutError"
    structlog = extract_error(
        rec(
            exception=[
                {
                    "exc_type": "PricingError",
                    "exc_value": "can't price",
                    "frames": [{"filename": "/srv/app/checkout.py", "lineno": 19, "name": "price"}],
                },
                {"exc_type": "KeyError", "exc_value": "'EUR'", "frames": []},
            ]
        )
    )
    assert [e.type for e in structlog.chain] == ["KeyError", "PricingError"]  # root first
    assert extract_error(rec(message="ok")) is None


def test_loguru_serialized_keeps_the_exception():
    line = json.dumps(
        {
            "text": "2026 | ERROR | boom\n" + timeout_tb(),
            "record": {
                "message": "boom",
                "level": {"name": "ERROR"},
                "exception": {"type": "TimeoutError", "value": "timed out", "traceback": True},
                "file": {"name": "checkout.py", "path": "/srv/app/checkout.py"},
                "line": 13,
                "time": {"timestamp": NOW.timestamp()},
            },
        }
    )
    info = extract_error(parse_line(line))
    assert info.final.type == "TimeoutError" and len(info.final.frames) == 3


def test_is_error():
    assert is_error(rec())
    assert is_error(LogRecord(None, "x", {"levelname": "CRITICAL"}))
    assert is_error(LogRecord(None, "x", {"levelno": 40}))
    assert is_error(LogRecord(None, "2026-10-17 12:00:01 ERROR app: payment failed"))
    assert is_error(LogRecord(None, "x", {"level": "info", "exc_info": timeout_tb()}))
    assert not is_error(LogRecord(None, "x", {"level": "info"}))
    assert not is_error(LogRecord(None, "an error rate dashboard", {"level": "info"}))
    assert not is_error(LogRecord(None, "2026-10-17 12:00:01 INFO retrying after an error"))


def test_join_multiline_puts_a_plain_text_traceback_back_together():
    lines = [
        "2026-10-17 12:00:00 INFO start",
        "2026-10-17 12:00:01 ERROR pricing failed",
        *CHAINED.splitlines(),
        "2026-10-17 12:00:02 INFO next",
        json.dumps({"message": "json lines stay whole"}),
        "",
    ]
    events = list(join_multiline(lines))
    assert events[0] == "2026-10-17 12:00:00 INFO start"
    assert events[1].startswith("2026-10-17 12:00:01 ERROR pricing failed\nTraceback")
    assert events[1].endswith("app.checkout.PricingError: can't price order 9001")
    assert events[2:] == ["2026-10-17 12:00:02 INFO next", '{"message": "json lines stay whole"}']
    assert len(parse_traceback(events[1])) == 2


# --- grouping ---------------------------------------------------------------------------


def test_normalize():
    assert normalize("timeout after 3.5s for order 8812") == "timeout after <n>s for order <n>"
    assert normalize("user 'bob' id 4bf92f3577b34da6a3ce929d0e0e4703") == "user <str> id <id>"


def test_app_files_resolve_deployed_paths():
    assert APP.resolve(Frame("/srv/app/checkout.py", 1, "f")) == "app/checkout.py"
    assert APP.resolve(Frame("C:\\srv\\app\\checkout.py", 1, "f")) == "app/checkout.py"
    assert APP.resolve(Frame("/usr/lib/python3.12/site-packages/app/checkout.py", 1, "f")) is None
    assert APP.resolve(Frame("/srv/other.py", 1, "f")) is None


def test_values_line_numbers_and_library_frames_dont_split_a_group():
    records = [
        rec(exc_info=timeout_tb(line=9, sku="SKU-1")),
        rec(exc_info=timeout_tb(line=12, sku="SKU-2")),  # code moved down 3 lines
        rec(exc_info=timeout_tb(sku="SKU-3").replace("1145", "1200")),  # httpx upgraded
    ]
    (g,) = group_errors(records, [], APP).values()
    assert g.count == 3 and g.variants == 3
    assert g.title == "TimeoutError: POST http://inventory/SKU-<n> timed out after <n>s"
    assert g.raised_at == "app/inventory.py:9" and g.raised_in == "reserve"
    assert g.origin == "httpx"
    assert g.path == ["checkout.charge", "inventory.reserve", "httpx"]


def test_different_causes_are_different_groups():
    other_caller = timeout_tb().replace(
        'checkout.py", line 11, in charge', 'checkout.py", line 30, in refund'
    )
    groups = group_errors(
        [rec(exc_info=timeout_tb()), rec(exc_info=other_caller), rec(exc_info=CHAINED)], [], APP
    )
    assert sorted(g.title for g in groups.values()) == [
        "TimeoutError: POST http://inventory/SKU-1 timed out after 3.5s",
        "TimeoutError: POST http://inventory/SKU-1 timed out after 3.5s",
        "app.checkout.PricingError: can't price order 9001",
    ]
    chained = next(g for g in groups.values() if g.causes)
    assert chained.causes == ["KeyError: 'EUR'"] and chained.raised_at == "app/pricing.py:2"


def test_errors_without_a_traceback_group_by_log_call_then_message():
    code = (
        'import logging\nlog = logging.getLogger()\nlog.error("label service returned %s", 503)\n'
    )
    sites = index_source(code, "app/ship.py")
    records = [
        rec(message="label service returned 503", pathname="/srv/app/ship.py", lineno=3),
        rec(message="label service returned 502"),  # matched by template
        rec(message="disk full on /dev/sda1 at 99%"),
        rec(message="disk full on /dev/sda2 at 97%"),
    ]
    groups = sorted(group_errors(records, sites, APP).values(), key=lambda g: g.title)
    assert [(g.title, g.count) for g in groups] == [
        ("disk full on /dev/sda<n> at <n>%", 2),
        ("label service returned <n>", 2),
    ]
    assert groups[1].logged_at.id == "app/ship.py:3" and groups[1].exc_type is None


def test_samples_are_redacted_and_carry_ids():
    r = rec(
        message="charge failed for card 4111 1111 1111 1111",
        exc_info=timeout_tb(),
        trace_id="4bf92f3577b34da6a3ce929d0e0e4703",
        service="checkout",
    )
    (g,) = group_errors([r], [], APP).values()
    assert "4111 1111" not in g.sample["message"] and g.sample["message"].endswith("1111")
    assert g.sample["ids"] == {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4703"}
    assert g.sample["traceback"].startswith("Traceback")
    assert g.services == {"checkout": 1} and g.levels == {"error": 1}


def write_log(path, records):
    lines = []
    for minutes_ago, fields in records:
        ts = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        lines.append(json.dumps({"timestamp": ts, "level": "ERROR", **fields}))
    lines.append(json.dumps({"timestamp": NOW.isoformat(), "level": "INFO", "message": "ok"}))
    path.write_text("\n".join(lines) + "\n")


def test_summarize_ranks_new_then_spiking_then_count(tmp_path):
    day = 24 * 60
    write_log(
        tmp_path / "prod.log",
        [(5, {"message": "timeout", "exc_info": timeout_tb()})] * 3  # new
        + [(5, {"message": "pricing", "exc_info": CHAINED})] * 40  # steady
        + [(day + 5, {"message": "pricing", "exc_info": CHAINED})] * 40
        + [(5, {"message": "disk full"})] * 30  # spiking: 3 -> 30
        + [(day + 5, {"message": "disk full"})] * 3
        + [(day + 5, {"message": "gone", "exc_info": "KeyError"})],  # not an exception text
    )
    s = summarize_errors(
        FileBackend(tmp_path / "prod.log"),
        [],
        APP,
        TimeRange.last(24),
        TimeRange.last(24).previous(),
        LintConfig(spike_min_count=10),
    )
    assert [(g.title.split(":")[0], g.count, g.previous) for g in s.groups] == [
        ("TimeoutError", 3, 0),
        ("disk full", 30, 3),
        ("app.checkout.PricingError", 40, 40),
    ]
    assert s.events == 73 and s.previous_events == 44 and s.resolved == 1 and not s.truncated
    assert s.groups[0].is_new and s.groups[1].trend == "10.0x"


def test_file_backend_reads_multiline_plain_text(tmp_path):
    (tmp_path / "app.log").write_text(
        "INFO start\nERROR pricing failed\n" + CHAINED + "INFO next\n"
    )
    records = FileBackend(tmp_path / "app.log").query_errors(TimeRange.last(1))
    assert len(records) == 1 and extract_error(records[0]).final.type.endswith("PricingError")


def test_loki_filters_errors_server_side():
    line = json.dumps({"msg": "x", "level": "error"})
    info = json.dumps({"msg": "error budget ok", "level": "info"})  # matched the filter only
    payload = {"data": {"result": [{"stream": {}, "values": [["1", line], ["2", info]]}]}}
    with stub_server(lambda path, body: payload) as (url, reqs):
        found = LokiBackend(url, '{app="a"}').query_errors(TimeRange.last(1))
    assert len(found) == 1
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(reqs[0][0]).query)["query"][0]
    assert query == '{app="a"} |~ "(?i)(error|exception|traceback|critical|fatal)"'


def test_opensearch_filters_errors_server_side():
    hit = {"@timestamp": NOW.isoformat(), "message": "x", "error": {"type": "KeyError"}}
    with stub_server(lambda path, body: {"hits": {"hits": [{"_source": hit}]}}) as (url, reqs):
        found = OpenSearchBackend(url, "logs-*").query_errors(TimeRange.last(1))
    assert extract_error(found[0]).final.type == "KeyError"
    ((_, _, body),) = reqs
    should = body["query"]["bool"]["filter"][-1]["bool"]["should"]
    assert {"exists": {"field": "error.stack_trace"}} in should


def test_cloudwatch_filters_errors_server_side():
    events = [{"timestamp": int(NOW.timestamp() * 1000), "message": "ERROR boom"}]
    fake = FakeLogs(events)
    assert len(CloudWatchBackend("g", client=fake).query_errors(TimeRange.last(1))) == 1
    assert fake.calls[0]["filterPattern"].startswith("?ERROR ?Error")
    fake = FakeLogs(events)
    CloudWatchBackend("g", filter_pattern='{ $.app = "a" }', client=fake).query_errors(
        TimeRange.last(1)
    )
    assert fake.calls[0]["filterPattern"] == '{ $.app = "a" }'  # can't combine: kept


# --- CLI and MCP ------------------------------------------------------------------------


def project(tmp_path):
    (tmp_path / "app").mkdir(exist_ok=True)
    (tmp_path / "app" / "checkout.py").write_text(
        "import logging\nlogger = logging.getLogger()\n\n\ndef charge(order):\n"
        "    try:\n        pass\n    except TimeoutError:\n"
        '        logger.error("payment failed for order %s", order.id, exc_info=True)\n'
    )
    (tmp_path / "app" / "inventory.py").write_text("")
    write_log(
        tmp_path / "prod.log",
        [
            (
                10 - i,  # the last one is the latest: the sample
                {
                    "message": f"payment failed for order {i}",
                    "exc_info": timeout_tb(sku=f"SKU-{i}"),
                    "pathname": "/srv/app/checkout.py",
                    "lineno": 9,
                    "trace_id": f"{i:032x}",
                },
            )
            for i in range(4)
        ],
    )
    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "prod.log"\n')


def test_cli_errors(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--traceback"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == "4 errors in the last 24h · 1 group(s) · 1 new · before: 0"
    # the overview table: one aligned row per group
    assert lines[2].split() == ["#", "TREND", "COUNT", "ERROR", "RAISED", "AT", "LOGGED", "AT"]
    row = lines[4]
    assert row.startswith("  1  new        4  TimeoutError: POST http://inventory/SKU-<n>")
    assert row.endswith("inventory.py:9  checkout.py:9")
    # the card
    assert "━━ 1. TimeoutError: POST http://inventory/SKU-<n> timed out after <n>s ━" in out
    assert "   Raised at    app/inventory.py:9  reserve()  (in httpx)" in out
    assert "   Logged at    app/checkout.py:9" in out
    assert '9 │ logger.error("payment failed for order %s", order.id, exc_info=True)' in out
    assert "   Call path    checkout.charge › inventory.reserve › httpx" in out
    assert "   Example      logsite trace 00000000000000000000000000000003" in out
    assert "  TimeoutError: POST http://inventory/SKU-3 timed out after 3.5s" in out
    assert '▶   File "/srv/app/inventory.py", line 9, in reserve' in out  # the project's frame
    assert '    File "/usr/lib/python3.12/site-packages/httpx/' in out  # a library frame: no ▶
    assert "\033[" not in out  # captured output isn't a terminal: no colours

    assert main(["errors", "--traceback", "--color", "always"]) == 0
    colored = capsys.readouterr().out
    assert "\033[1;97;41m NEW \033[0m" in colored  # a new group: a red badge
    assert "\033[1;4;36mapp/inventory.py:9\033[0m" in colored  # your code: underlined cyan
    assert "\033[1;34mapp/checkout.py:9\033[0m" in colored  # your log call: blue
    assert "\033[35mhttpx\033[0m" in colored  # the library: magenta
    assert '\033[1;36m  File "/srv/app/inventory.py", line 9' in colored  # own frame highlighted
    assert '\033[90m  File "/usr/lib/python3.12/site-packages/httpx/' in colored  # library gray

    assert main(["errors", "--brief"]) == 0
    brief = capsys.readouterr().out
    assert "TimeoutError" in brief and "━━" not in brief  # the table only

    assert main(["errors", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["groups"][0]["logged_at"] == "app/checkout.py:9" and data["groups"][0]["new"]

    assert main(["errors", "--hours", "0.01"]) == 0
    assert "no errors in the last 0.01h" in capsys.readouterr().err


def test_cli_errors_since_deploy(repo, monkeypatch, capsys):
    project(repo)
    monkeypatch.chdir(repo)
    at = (NOW - timedelta(minutes=30)).isoformat()
    assert main(["errors", "--since-deploy", "HEAD", "--deployed-at", at]) == 0
    assert "4 errors since HEAD (" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="can't be combined"):
        main(["errors", "--since-deploy", "HEAD", "--hours", "1"])
    with pytest.raises(SystemExit, match="--deployed-at needs since_deploy"):
        main(["errors", "--deployed-at", at])


def test_mcp_error_summary(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    project(tmp_path)
    err, out = call(tmp_path, "error_summary", {})
    assert not err, out
    (g,) = out["groups"]
    assert g["raised_at"] == "app/inventory.py:9" and g["count"] == 4 and g["new"]
    assert g["sample"]["ids"]["trace_id"] == "00000000000000000000000000000003"
    err, out = call(tmp_path, "error_summary", {"since_deploy": "nope"})
    assert err and "unknown git ref" in out


def test_plain_text_lines_get_their_leading_timestamp():
    assert parse_line("2026-10-14 10:00:05,123 ERROR app boom").timestamp == datetime(
        2026, 10, 14, 10, 0, 5, 123000, tzinfo=timezone.utc
    )  # no zone in the line: UTC
    assert parse_line("[2026-10-14T10:00:05Z] ERROR boom").timestamp == datetime(
        2026, 10, 14, 10, 0, 5, tzinfo=timezone.utc
    )
    assert parse_line("ERROR boom").timestamp is None
    assert parse_line("2026-10-14 10:00:05 x", timestamp=NOW).timestamp == NOW  # backend wins


def test_plain_text_titles_drop_the_timestamp_and_level():
    r = LogRecord(None, "2026-10-14 10:00:05,123 ERROR app.checkout payment failed")
    (g,) = group_errors([r], [], APP).values()
    assert g.title == "app.checkout payment failed"


def test_plain_text_errors_from_loki_carry_stream_labels():
    """Loki returns the stream's labels as fields; the level is still in the text."""
    record = LogRecord(None, "2026-10-17 12:00:01 ERROR [svc] c.a.S : Retry failed", {"app": "x"})
    assert is_error(record)
    assert not is_error(LogRecord(None, "2026-10-17 12:00:01 INFO retry ok", {"app": "x"}))
