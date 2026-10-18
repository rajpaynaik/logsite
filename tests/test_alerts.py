import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from logsite.alerts import (
    AlertsConfig,
    Enricher,
    make_server,
    narrow_selector,
    parse_grafana,
    render,
    window_for,
    with_selector,
)

# A real payload from Grafana 13.2's webhook contact point (unified alerting).
PAYLOAD = json.loads((Path(__file__).parent / "fixtures" / "grafana-alert.json").read_text())
NOW = datetime(2026, 10, 17, 17, 0, tzinfo=timezone.utc)

OPE = "com.example.orders.exception.OrderProcessingException"
PSQL = "org.postgresql.util.PSQLException: ERROR: canceling statement due to statement timeout"
REPO = "src/main/java/com/example/orders/repository/OrderRepository.java"
SUMMARY = {
    "window": {},
    "events": 758,
    "previous_events": 800,
    "groups_total": 4,
    "resolved": 0,
    "truncated": False,
    "groups": [
        {
            "id": "bc3531d5",
            "title": f"{OPE}: Unable to save order ORD-<n>",
            "type": OPE,
            "causes": [
                PSQL,
                "org.springframework.dao.QueryTimeoutException: JDBC statement timed out",
            ],
            "root_cause": PSQL,
            "count": 561,
            "previous": 0,
            "trend": "new",
            "new": True,
            "raised_at": "src/main/java/com/example/orders/repository/OrderRepository.java:87",
            "origin": "org.postgresql",
            "logged_at": "src/main/java/com/example/orders/OrderController.java:56",
            "sample": {"ids": {"traceId": "77064c2c"}},
        },
        {
            "id": "e631ff07",
            "title": "Retry failed for order ORD-<n>",
            "type": None,
            "causes": [],
            "root_cause": None,
            "count": 197,
            "previous": 200,
            "trend": "1.0x",
            "new": False,
            "raised_at": None,
            "origin": None,
            "logged_at": "src/main/java/com/example/orders/service/OrderService.java:170",
            "sample": {"ids": {}},
        },
    ],
}


def test_parse_a_real_grafana_payload():
    (alert,) = parse_grafana(PAYLOAD)
    assert alert.name == "order-service errors" and alert.status == "firing"
    assert alert.labels["service_name"] == "order-service"
    assert alert.starts_at == datetime(2026, 10, 17, 16, 49, 30, tzinfo=timezone.utc)
    assert alert.url == "http://localhost:31300/alerting/grafana/order-errors/view?orgId=1"
    assert alert.dashboard_url is None  # "" in the payload when the rule has no panel
    assert alert.summary == "order-service is logging more than 10 errors in 5 minutes"
    assert alert.silence_url.startswith("http://localhost:31300/alerting/silence/new")
    assert (
        parse_grafana({"alerts": [{"status": "firing", "startsAt": "0001-01-01T00:00:00Z"}]})[
            0
        ].starts_at
        is None
    )


def test_window_and_selector():
    (alert,) = parse_grafana(PAYLOAD)
    start, end = window_for(alert, 30, NOW)
    assert (start, end) == (datetime(2026, 10, 17, 16, 19, 30, tzinfo=timezone.utc), NOW)
    labels = {"service_name": "order-service", "pod": 'a"b'}
    names = ["service_name", "pod"]
    assert narrow_selector('{app="orders"}', labels, names) == (
        '{app="orders", service_name="order-service", pod="a\\"b"}'
    )
    assert narrow_selector('{service_name=~"order.*"}', labels, ["service_name"]) == (
        '{service_name=~"order.*"}'  # already narrowed by the user: kept
    )
    assert narrow_selector("{}", labels, ["service_name"]) == '{service_name="order-service"}'
    assert with_selector({"type": "file", "path": "x"}, labels, names) == {
        "type": "file",
        "path": "x",
    }


def test_render_for_slack_and_text():
    (alert,) = parse_grafana(PAYLOAD)
    code = {f"{REPO}:87": "return jpa.save(order);"}
    kw = {
        "code_line": code.get,
        "code_url": lambda loc: f"https://github.com/acme/orders/blob/abc/{loc.replace(':', '#L')}",
        "trace_field": "traceId",
    }
    slack = render(alert, SUMMARY, slack=True, **kw)
    lines = slack.splitlines()
    assert lines[0] == (
        ":red_circle: *order-service errors*: order-service is logging more than 10 errors in "
        "5 minutes (firing since 16:49 UTC)"
    )
    grafana = "http://localhost:31300/alerting"
    assert lines[1].startswith(
        f"758 error(s) in 4 group(s), *1 new* · <{grafana}/grafana/order-errors/view?orgId=1"
        f"|Open in Grafana> · <{grafana}/silence/new"
    )
    assert f"1. *{OPE}: Unable to save order ORD-<n>*  ·  561  ·  *new*" in slack
    assert f"    root cause: {PSQL} (org.postgresql)" in slack
    assert (
        "    raised: <https://github.com/acme/orders/blob/abc/src/main/java/com/example/orders/"
        "repository/OrderRepository.java#L87|src/main/java/com/example/orders/repository/"
        "OrderRepository.java:87>  `return jpa.save(order);`"
    ) in slack
    assert "    example: `logsite trace 77064c2c`" in slack
    assert slack.endswith("+ 2 more group(s): `logsite errors`")
    text = render(alert, SUMMARY, slack=False)
    assert "*" not in text.splitlines()[0] and "(http://localhost:31300/alerting/grafana" in text
    empty = render(alert, {**SUMMARY, "groups": [], "groups_total": 0, "events": 0}, slack=False)
    assert "No error logs in this window" in empty


def test_config():
    cfg = AlertsConfig.from_dict({"token_env": "LOGSITE_TEST_TOKEN", "top": 5})
    assert cfg.token is None and cfg.top == 5
    with pytest.raises(ValueError, match="unknown"):
        AlertsConfig.from_dict({"slack": "x"})


class Recorder:
    def __init__(self):
        self.calls = []
        self.sent = []

    def summarize(self, start, end, labels):
        self.calls.append((start, end, labels))
        return json.loads(json.dumps(SUMMARY))

    def send(self, url, body):
        self.sent.append((url, body))


def test_enricher_sends_firing_alerts_and_skips_resolved():
    rec = Recorder()
    cfg = AlertsConfig(
        slack_webhook="https://hooks.slack.test/x", webhook="https://example.test/hook", top=1
    )
    enricher = Enricher(cfg, rec.summarize, send=rec.send, now=lambda: NOW)
    resolved = json.loads(json.dumps(PAYLOAD))
    resolved["alerts"].append({**resolved["alerts"][0], "status": "resolved"})
    results = enricher.handle(resolved)
    assert [r["status"] for r in results] == ["firing", "resolved"]
    assert results[0]["sent"] == ["slack", "webhook"] and results[1]["skipped"]
    ((start, end, labels),) = rec.calls
    assert labels == {"service_name": "order-service"}  # only the configured labels
    (slack_url, slack_body), (hook_url, hook_body) = rec.sent
    assert slack_url == "https://hooks.slack.test/x" and slack_body["text"].startswith(
        ":red_circle:"
    )
    assert hook_body["alert"] == "order-service errors" and len(hook_body["summary"]["groups"]) == 1


@pytest.fixture
def server():
    rec = Recorder()
    enricher = Enricher(
        AlertsConfig(slack_webhook="https://hooks.slack.test/x"),
        rec.summarize,
        send=rec.send,
        now=lambda: NOW,
    )
    srv = make_server("127.0.0.1", 0, enricher, token="t0ken")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", rec
    srv.shutdown()


def call(url, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_server_receives_grafana_webhooks(server):
    url, rec = server
    assert call(f"{url}/healthz")[0] == 200
    assert call(f"{url}/grafana/alert", PAYLOAD)[0] == 401  # no token
    assert call(f"{url}/grafana/alert", PAYLOAD, token="wrong")[0] == 401
    status, body = call(f"{url}/grafana/alert", PAYLOAD, token="t0ken")
    assert status == 200 and body["alerts"][0]["sent"] == ["slack"]
    assert rec.sent[0][1]["text"].startswith(":red_circle: *order-service errors*")
    assert call(f"{url}/nope", PAYLOAD, token="t0ken")[0] == 404


def test_server_serves_errors_for_a_time_range(server):
    url, rec = server
    status, body = call(
        f"{url}/errors?from=1792253000000&to=1792256600000&service_name=order-service&token=t0ken"
    )
    assert status == 200 and body["groups_total"] == 4
    start, end, labels = rec.calls[-1]
    assert start == datetime.fromtimestamp(1792253000, tz=timezone.utc)  # Grafana's ${__from}
    assert end - start == timedelta(hours=1) and labels == {"service_name": "order-service"}
    assert call(f"{url}/errors?from=nope&token=t0ken")[0] == 400


def test_cli_refuses_to_listen_publicly_without_a_token(tmp_path, monkeypatch):
    from logsite.cli import main

    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "x.log"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="needs a token"):
        main(["alert-server", "--host", "0.0.0.0"])


def test_errors_endpoint_adds_links_and_short_names(server):
    url, rec = server
    status, body = call(f"{url}/errors?service_name=&token=t0ken")
    assert status == 200
    assert rec.calls[-1][2] == {}  # an empty dashboard variable means "all services"
    first, second = body["groups"]
    assert first["title_short"] == "OrderProcessingException: Unable to save order ORD-<n>"
    assert first["root_cause_short"].startswith("PSQLException: ERROR: canceling statement")
    assert first["raised_name"] == "OrderRepository.java:87"
    assert first["trend_label"] == "new" and second["trend_label"] == "1.0x"
    assert first["raised_url"] is None  # the test enricher has no code host
    assert second["raised_name"] is None and second["logged_name"] == "OrderService.java:170"


def test_render_says_when_an_error_is_back():
    (alert,) = parse_grafana(PAYLOAD)
    summary = json.loads(json.dumps(SUMMARY))
    summary["groups"][0]["history"] = {
        "regression": True,
        "first_seen": "2026-08-03T12:00:00+00:00",
        "resolved_at": "2026-09-03T09:50:00+00:00",
        "resolved_release": "v1.3",
        "fixed_by": [{"sha": "a1b2c3d4", "author": "Maya", "summary": "Retry carrier timeouts"}],
    }
    text = render(alert, summary, slack=False, trace_field="traceId")
    assert f"1. {OPE}: Unable to save order ORD-<n>  ·  561  ·  back" in text
    assert "    ↩ came back: seen 2026-08-03 → 2026-09-03, gone after v1.3" in text
    assert "    fixed before by: a1b2c3d4 Maya: Retry carrier timeouts" in text


def test_history_columns_and_events_endpoint():
    rec = Recorder()

    def summarize(start, end, labels):
        out = rec.summarize(start, end, labels)
        out["groups"][0]["history"] = {
            "regression": True, "known": True, "first_seen": "2026-08-03T12:00:00+00:00",
            "resolved_release": "v1.3", "days_seen": 31,
            "fixed_by": [{"sha": "a1b2c3d4", "summary": "Retry carrier timeouts"}],
        }  # fmt: skip
        out["groups"][1]["history"] = {"known": False, "new_ever": True}
        return out

    events = [{"time": 1785744000000, "kind": "first seen", "title": "first seen: x"}]
    seen = []
    enricher = Enricher(
        AlertsConfig(), summarize, send=rec.send, now=lambda: NOW,
        events=lambda start, end: seen.append((start, end)) or events,
    )  # fmt: skip
    srv = make_server("127.0.0.1", 0, enricher, token=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        _, body = call(f"{url}/errors")
        back, new = body["groups"]
        assert back["trend_label"] == "back"
        assert back["history_label"] == "↩ back, gone after v1.3"
        assert back["first_seen_ever"] == "2026-08-03"
        assert back["fixed_before"] == "a1b2c3d4 Retry carrier timeouts (v1.3)"
        assert new["history_label"] == "first time ever" and new["fixed_before"] is None
        status, body = call(f"{url}/history/events?from=1785700000000&to=1785800000000")
        assert status == 200 and body == {"events": events}
        assert seen[0][0].year == 2026
        enricher.events = None
        assert call(f"{url}/history/events")[0] == 404
    finally:
        srv.shutdown()
