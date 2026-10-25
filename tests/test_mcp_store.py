"""The MCP server's error-store tools, and the server over HTTP behind a token."""

import asyncio

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from test_prompt import NOW, project  # noqa: E402, F401  (the fixture)

from logsite.config import load_config  # noqa: E402
from logsite.mcp_server import build_server, http_app  # noqa: E402


def call(root, tool, args=None):
    async def run():
        async with Client(build_server(load_config(root))) as client:
            r = await client.call_tool(tool, args or {})
            if r.is_error:
                return True, r.content[0].text
            out = r.structured_content
            return False, out["result"] if set(out) == {"result"} else out

    return asyncio.run(run())


def test_stored_errors_for_a_range(project):  # noqa: F811
    err, out = call(project, "stored_errors", {"hours": 48})
    assert not err
    assert [g["id"] for g in out["groups"]] == ["abcd1234", "abff0000"]
    assert out["groups"][0]["recent"] == 15 and out["total"] == 16
    assert out["facets"]["services"] == [{"value": "payments", "n": 16}]
    # the last 3 hours only, and a text search
    err, out = call(project, "stored_errors", {"hours": 3})
    assert [(g["id"], g["recent"]) for g in out["groups"]] == [("abcd1234", 3)]
    err, out = call(project, "stored_errors", {"hours": 48, "search": "timed out"})
    assert [g["id"] for g in out["groups"]] == ["abff0000"]
    err, out = call(project, "stored_errors", {"since": "2026-13-01"})
    assert err and "ISO 8601" in out


def test_error_case(project):  # noqa: F811
    err, case = call(project, "error_case", {"id": "abcd"})
    assert not err
    assert case["group"]["title"] == "KeyError: 'JPY'"
    assert case["started"]["commits"][0]["summary"] == "Look rates up directly"
    assert case["shared"]["same"][0] == {"field": "currency", "value": "JPY", "count": 15,
                                         "of": 15}  # fmt: skip
    assert case["code"]["path"] == "payments.py"
    err, text = call(project, "error_case", {"id": "ab"})
    assert err and "2 stored errors start with 'ab'" in text and "abff0000" in text


def test_occurrences_and_trend(project):  # noqa: F811
    err, events = call(project, "error_occurrences", {"id": "abcd1234", "limit": 2})
    assert not err and len(events) == 2 and events[0]["trace_id"] == "t20"
    assert events[0]["fields"]["currency"] == "JPY"
    err, trend = call(project, "error_trend", {"id": "abcd1234", "days": 3})
    assert not err and trend["total"] == 15 and trend["title"] == "KeyError: 'JPY'"
    err, text = call(project, "error_trend", {"id": "zz"})
    assert err and "no stored error" in text


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}  # fmt: skip
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_http_needs_the_token(project):  # noqa: F811
    TestClient = pytest.importorskip("starlette.testclient").TestClient

    app = http_app(load_config(project), "0.0.0.0", 8078, "s3cret")
    with TestClient(app, base_url="http://logsite.internal") as client:
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "authorization": "Bearer nope"}
        assert client.post("/mcp", json=INIT, headers=bad).status_code == 401
        assert client.post("/mcp/nope", json=INIT, headers=HEADERS).status_code == 401
        good = {**HEADERS, "authorization": "Bearer s3cret"}
        r = client.post("/mcp", json=INIT, headers=good)
        assert r.status_code == 200 and "logsite" in r.text
        r = client.post("/mcp/s3cret", json=INIT, headers=HEADERS)  # the token in the URL
        assert r.status_code == 200 and "logsite" in r.text


def test_http_on_localhost_refuses_other_hosts(project):  # noqa: F811
    TestClient = pytest.importorskip("starlette.testclient").TestClient

    def app():  # an app starts once
        return http_app(load_config(project), "127.0.0.1", 8078, None)

    with TestClient(app(), base_url="http://127.0.0.1:8078") as client:
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 200
    with TestClient(app(), base_url="http://evil.example:8078") as client:  # DNS rebinding
        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code in (400, 421)


def test_off_localhost_without_a_token_is_refused(project, monkeypatch):  # noqa: F811
    from logsite.cli import main

    monkeypatch.delenv("LOGSITE_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--config-dir", str(project), "mcp", "--http", "--host", "0.0.0.0"])
    assert "needs a token" in str(exc.value)


assert NOW  # imported with the fixture
