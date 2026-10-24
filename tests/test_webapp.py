"""logsite app: the setup forms, the error pages' API, and what keeps it safe on localhost."""

import http.client
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest

from logsite.config import load_config, tomllib
from logsite.webapp import App, dump_toml, make_server, secrets_path

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

DIV = """Traceback (most recent call last):
  File "/srv/app/checkout.py", line 8, in charge
    return order["total"] / order["items"]
ZeroDivisionError: division by zero"""


@pytest.fixture
def app(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "checkout.py").write_text(CHECKOUT)
    (tmp_path / ".git").mkdir()
    now = datetime.now(timezone.utc)
    (tmp_path / "app.log").write_text("\n".join(json.dumps({
        "timestamp": (now - timedelta(minutes=m)).isoformat(), "level": "ERROR",
        "message": f"charge failed for ORD-{m}", "pathname": "/srv/app/checkout.py",
        "lineno": 10, "exc_info": DIV}) for m in range(3)) + "\n")  # fmt: skip
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / f".{var.lower()}"))
    monkeypatch.chdir(tmp_path)
    application = App(tmp_path)
    server = make_server(application, "127.0.0.1", 0, None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    page = request(port, "GET", "/")[1]
    token = page.split('name="logsite-token" content="')[1].split('"')[0]
    yield application, port, token, tmp_path
    server.shutdown()
    for name in list(os.environ):
        if name.startswith("LOGSITE_"):
            monkeypatch.delenv(name, raising=False)


def request(port, method, path, body=None, token=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Logsite-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    res = conn.getresponse()
    return res.status, res.read().decode()


def api(app, method, path, body=None):
    _, port, token, _ = app
    status, text = request(port, method, path, body, token)
    return status, json.loads(text)


def test_the_api_needs_the_page_token_and_its_own_host(app):
    _, port, token, _ = app
    assert request(port, "GET", "/api/status")[0] == 401  # another site can't read the token
    assert request(port, "GET", "/api/status", token="wrong")[0] == 401
    assert request(port, "GET", "/api/status", token=token, host="evil.example:80")[0] == 403
    assert request(port, "POST", "/api/settings", {}, host="evil.example:80")[0] == 403
    assert request(port, "GET", "/api/status", token=token)[0] == 200
    assert request(port, "GET", "/../webapp.py")[0] == 404
    assert request(port, "GET", "/%2e%2e/webapp.py")[0] == 404


def test_a_log_store_is_tested_before_it_is_saved(app):
    status, r = api(app, "POST", "/api/test/backend",
                    {"type": "file", "values": {"path": "app.log"}})  # fmt: skip
    assert status == 200 and r["ok"] and r["errors"] == 3
    assert r["lines"][0]["message"].startswith("charge failed for ORD-")
    _, r = api(app, "POST", "/api/test/backend", {"type": "file", "values": {"path": "nope.log"}})
    assert not r["ok"] and "no file" in r["error"]
    _, r = api(app, "POST", "/api/test/backend", {"type": "loki", "values": {}})
    assert not r["ok"] and r["error"] == "Loki URL is needed"


def test_settings_keep_secrets_out_of_the_config_file(app):
    _, _, _, root = app
    (root / "logsite.toml").write_text(
        '[lint]\nignore = ["LL004"]\n\n[pipeline]\nsample = { "app/api.py:88" = 0.01 }\n'
    )
    status, s = api(app, "POST", "/api/settings", {
        "backend": {"type": "loki", "values": {"url": "http://loki:3100",
                    "selector": '{app="checkout"}', "password": "s3cret-pw"}},
        "slack": {"webhook": "https://hooks.slack.com/services/T/B/x"},
        "service": "checkout",
    })  # fmt: skip
    assert status == 200 and s["configured"] and s["slack"]
    text = (root / "logsite.toml").read_text()
    assert "s3cret-pw" not in text and "hooks.slack.com" not in text
    data = tomllib.loads(text)
    assert data["backend"]["password_env"] == "LOGSITE_LOKI_PASSWORD"
    assert data["lint"] == {"ignore": ["LL004"]}  # the rest of the file is kept
    assert data["pipeline"]["sample"] == {"app/api.py:88": 0.01}
    secrets = secrets_path()
    assert json.loads(secrets.read_text())["LOGSITE_LOKI_PASSWORD"] == "s3cret-pw"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600  # only this user can read it
    cfg = load_config(root)  # logsite reads the secret back through the environment
    assert cfg.backend["password"] == "s3cret-pw"
    assert cfg.alerts.slack_webhook == "https://hooks.slack.com/services/T/B/x"
    _, form = api(app, "GET", "/api/settings")
    assert form["backend"]["secrets"] == {"password": True}  # the page knows one is saved
    assert "s3cret-pw" not in json.dumps(form)  # ... but never sees it
    # saving again with the password left empty keeps it
    api(app, "POST", "/api/settings", {"backend": {"type": "loki", "values": {
        "url": "http://loki:3100", "selector": '{app="web"}'}}})  # fmt: skip
    assert load_config(root).backend["password"] == "s3cret-pw"


def test_errors_and_a_test_file_from_the_store(app):
    application, _, _, root = app
    api(app, "POST", "/api/settings", {"backend": {"type": "file",
                                                   "values": {"path": "app.log"}}})  # fmt: skip
    from logsite.backends import from_config
    from logsite.errors import AppFiles
    from logsite.index import index_project
    from logsite.store import open_store
    from logsite.watch import check

    cfg = application.cfg()
    store = open_store(cfg)
    check(cfg, from_config(cfg.backend), index_project(root), AppFiles.scan(root), store,
          datetime.now(timezone.utc))  # fmt: skip
    store.close()
    _, errors = api(app, "GET", "/api/errors?hours=24")
    (group,) = errors["groups"]
    assert group["title"] == "ZeroDivisionError: division by zero" and group["recent"] == 3
    _, detail = api(app, "GET", f"/api/errors/{group['id']}")
    assert detail["group"]["raised_at"] == "app/checkout.py:8"
    assert detail["group"]["raised_code"] == 'return order["total"] / order["items"]'
    assert len(detail["events"]) == 3 and sum(h["n"] for h in detail["hourly"]) == 3
    _, r = api(app, "POST", f"/api/errors/{group['id']}/repro", {})
    assert r["ok"] and "def test_repro_" in r["test"] and not r["exists"]
    _, r = api(app, "POST", f"/api/errors/{group['id']}/repro", {"write": True})
    assert r["written"] and (root / r["path"]).read_text().startswith("from app.checkout import")
    _, r = api(app, "POST", f"/api/errors/{group['id']}/repro", {"write": True})
    assert not r["ok"] and "exists already" in r["error"]  # never overwrites


def test_dump_toml_round_trips():
    data = {"backend": {"type": "loki", "url": "http://x", "aggregate": True},
            "lint": {"ignore": ["LL004"], "noisy_threshold": 50000},
            "timeline": {"field": "trace_id", "backend": {"type": "file", "path": "a b.log"}},
            "pipeline": {"sample": {"app/api.py:88": 0.01}}}  # fmt: skip
    assert tomllib.loads(dump_toml(data)) == data


def test_errors_by_date_range_service_and_text(app):
    application, _, _, root = app
    api(app, "POST", "/api/settings", {"backend": {"type": "file",
                                                   "values": {"path": "app.log"}}})  # fmt: skip
    from logsite.backends import from_config
    from logsite.errors import AppFiles
    from logsite.index import index_project
    from logsite.store import open_store
    from logsite.watch import backfill

    cfg = application.cfg()
    store = open_store(cfg)
    now = datetime.now(timezone.utc)
    chunks = list(backfill(cfg, from_config(cfg.backend), index_project(root),
                           AppFiles.scan(root), store, now - timedelta(days=2), now,
                           timedelta(hours=6)))  # fmt: skip
    store.close()
    assert sum(c.stored for c in chunks) == 3 and len(chunks) == 8  # 2 days in 6 h chunks
    start = (now - timedelta(minutes=1, seconds=30)).isoformat()
    _, r = api(app, "GET", f"/api/errors?from={start.replace('+', '%2B')}")
    assert r["total"] == 2  # the errors 0 and 1 minute ago, not the one 2 minutes ago
    _, r = api(app, "GET", "/api/errors?hours=1&q=division")
    assert r["total"] == 3 and r["groups"][0]["title"].startswith("ZeroDivisionError")
    _, r = api(app, "GET", "/api/errors?hours=1&q=nothing-like-this")
    assert r["groups"] == []
    _, r = api(app, "GET", "/api/errors?hours=1&service=billing")
    assert r["groups"] == [] and r["facets"] == {"services": [], "releases": []}
    status, r = api(app, "GET", "/api/errors?from=2026-01-02T00:00:00&to=2026-01-01T00:00:00")
    assert status == 400 and "before its end" in r["error"]
