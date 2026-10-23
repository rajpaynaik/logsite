"""Sentry: a project's error events through its web API, grouped at the line of code."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from logsite.backends import from_config
from logsite.backends.base import TimeRange
from logsite.backends.sentry import event_record
from logsite.cli import main
from logsite.tracebacks import extract_error

NOW = datetime.now(timezone.utc)

PRICING = """\
RATES = {"USD": 1.0}


def convert(order):
    return order["amount"] * RATES[order["currency"]]


class PricingError(Exception):
    pass


def price(order):
    try:
        return convert(order)
    except KeyError as e:
        raise PricingError(f"can't price order {order['id']}") from e
"""


def frame(path, line, function, code, in_app=True):
    context = [[line - 1, "    # before"], [line, f"    {code}"], [line + 1, ""]]
    return {"filename": path.rsplit("/srv/", 1)[-1], "absPath": path, "lineNo": line,
            "function": function, "inApp": in_app, "context": context}  # fmt: skip


def python_event(n, minutes_ago=5):
    return {
        "eventID": f"{n:032x}",
        "groupID": "4242",
        "title": f"PricingError: can't price order {9000 + n}",
        "dateCreated": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "platform": "python",
        "tags": [{"key": "level", "value": "error"}, {"key": "release", "value": "shop@1.4"},
                 {"key": "environment", "value": "production"}],  # fmt: skip
        "contexts": {"trace": {"trace_id": f"{n + 1:032x}"}},
        "entries": [
            {"type": "breadcrumbs", "data": {"values": []}},
            {"type": "exception", "data": {"values": [  # root cause first, as Sentry sends
                {"type": "KeyError", "value": "'EUR'", "stacktrace": {"frames": [
                    frame("/srv/app/pricing.py", 14, "price", "return convert(order)"),
                    frame("/srv/app/pricing.py", 5, "convert",
                          'return order["amount"] * RATES[order["currency"]]'),
                ]}},
                {"type": "PricingError", "value": f"can't price order {9000 + n}",
                 "module": "app.pricing", "stacktrace": {"frames": [
                    frame("/srv/app/pricing.py", 16, "price",
                          """raise PricingError(f"can't price order {order['id']}") from e"""),
                 ]}},
            ]}},
        ],
    }  # fmt: skip


class Sentry:
    """A fake Sentry serving one project's events, two pages, and recording requests."""

    def __init__(self, events):
        self.events, self.requests = events, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, self.headers.get("Authorization")))
                url = urlparse(self.path)
                page = int(parse_qs(url.query).get("cursor", ["0"])[0])
                chunk = outer.events[page * 2 : page * 2 + 2]
                more = len(outer.events) > page * 2 + 2
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                nxt = f"http://127.0.0.1:{outer.port}{url.path}?cursor={page + 1}"
                self.send_header("Link", f'<{nxt}>; rel="next"; results="{str(more).lower()}"; '
                                 f'cursor="{page + 1}"')  # fmt: skip
                self.end_headers()
                self.wfile.write(json.dumps(chunk).encode())

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def sentry():
    server = Sentry([python_event(n) for n in range(5)])
    yield server
    server.close()


def test_an_event_becomes_a_record_with_its_chain_and_code():
    record = event_record(python_event(1), "shop")
    assert record.message == "PricingError: can't price order 9001"
    assert record.fields["trace_id"] == f"{2:032x}" and record.fields["release"] == "shop@1.4"
    assert record.fields["service"] == "shop" and record.fields["level"] == "error"
    info = extract_error(record)
    root, final = info.chain
    assert root.title == "KeyError: 'EUR'"
    assert root.frames[-1].file == "/srv/app/pricing.py" and root.frames[-1].line == 5
    assert root.frames[-1].code == 'return order["amount"] * RATES[order["currency"]]'
    assert final.type == "PricingError" and final.frames[0].function == "price"


def test_java_frames_get_their_package_path():
    event = {"platform": "java", "entries": [{"type": "exception", "data": {"values": [
        {"type": "java.lang.IllegalStateException", "value": "no stock", "stacktrace": {
            "frames": [{"filename": "Inventory.java", "module": "com.acme.shop.Inventory",
                        "function": "reserve", "lineNo": 5}]}}]}}]}  # fmt: skip
    (exc,) = extract_error(event_record(event)).chain
    assert exc.frames[0].file == "com/acme/shop/Inventory.java"


def test_reads_every_page_with_the_token(sentry):
    backend = from_config({"type": "sentry", "org": "acme", "project": "shop", "token": "t0k",
                           "url": f"http://127.0.0.1:{sentry.port}"})  # fmt: skip
    records = backend.query_errors(TimeRange.last(1))
    assert len(records) == 5 and len(sentry.requests) == 3
    path, auth = sentry.requests[0]
    assert path.startswith("/api/0/projects/acme/shop/events/?full=true&start=")
    assert auth == "Bearer t0k"
    assert len(backend.query_errors(TimeRange.last(1), limit=3)) == 3


def test_a_token_is_required():
    with pytest.raises(ValueError, match="needs a token"):
        from_config({"type": "sentry", "org": "acme", "project": "shop"})


def test_errors_and_repro_from_sentry(sentry, tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "pricing.py").write_text(PRICING)
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "sentry"\norg = "acme"\nproject = "shop"\n'
        f'url = "http://127.0.0.1:{sentry.port}"\ntoken_env = "SENTRY_AUTH_TOKEN"\n'
    )
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "t0k")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--format", "json", "--no-history", "--hours", "1"]) == 0
    (group,) = json.loads(capsys.readouterr().out)["groups"]
    assert group["count"] == 5 and group["type"] == "PricingError"
    # where the root cause was raised, as for errors read from logs
    assert group["raised_at"] == "app/pricing.py:5" and group["root_cause"].startswith("KeyError")
    assert main(["repro", "--format", "json", "--hours", "1"]) == 0
    case = json.loads(capsys.readouterr().out)
    assert case["entry"]["function"] == "price" and case["entry"]["module"] == "app.pricing"
