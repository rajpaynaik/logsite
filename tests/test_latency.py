"""Latency from logs: timings read from log lines, stored per hour, and what got slower."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from logsite.backends.base import parse_line
from logsite.cli import main
from logsite.latency import LatencyConfig, Timing, duration, endpoint, format_seconds, slower
from logsite.store import connect

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@pytest.mark.parametrize("line, seconds", [
    ('{"msg": "request", "duration_ms": 182}', 0.182),
    ('{"msg": "done", "took": "1.5s"}', 1.5),
    ('{"msg": "done", "duration": "2m3.5s"}', 123.5),  # Go's time.Duration as text
    ('{"msg": "done", "duration": 1500000000}', 1.5),  # slog's JSON: nanoseconds
    ('{"msg": "done", "duration": 0.042}', 0.042),  # zap: seconds
    ('{"msg": "done", "responseTime": 35}', 0.035),  # pino-http
    ('{"msg": "x", "http": {"server": {"request": {"duration": 0.25}}}}', 0.25),
    ('level=info msg="handled" latency=350ms', 0.35),
    ("INFO sync_inventory completed in 2.3s", 2.3),
    ('10.0.0.1 "GET /orders/7 HTTP/1.1" 200 512 (35ms)', 0.035),
    ("INFO retry in 5m", None),  # a delay, not how long something took
    ("ERROR timed out after 30s", None),
    ('{"msg": "x", "duration": "slow"}', None),
    ('{"msg": "x", "duration": -3}', None),
])  # fmt: skip
def test_durations(line, seconds):
    found = duration(parse_line(line))
    assert found == pytest.approx(seconds) if seconds is not None else found is None


@pytest.mark.parametrize("line, expected", [
    ('{"msg": "r", "route": "/checkout", "method": "post"}', "POST /checkout"),
    ('{"msg": "r", "path": "/orders/123?x=1"}', "/orders/{id}"),
    ('{"msg": "r", "req": {"method": "GET", "url": "/x/42"}}', "GET /x/{id}"),
    ('{"msg": "r", "url": "https://api.example.com/v1/items/7?q=1", "method": "GET"}',
     "GET /v1/items/{id}"),
    ('1.2.3.4 "GET /users/9f8e7d6c5b4a3f2e1d0c HTTP/1.1" 200', "GET /users/{id}"),
    ('{"msg": "nothing here"}', None),
])  # fmt: skip
def test_endpoints(line, expected):
    assert endpoint(parse_line(line)) == expected


def test_percentiles_and_formatting():
    t = Timing.of([0.1] * 90 + [1.0] * 9 + [5.0])
    assert (t.n, t.p50, t.p95, t.p99, t.max) == (100, 0.1, 1.0, 1.0, 5.0)
    assert Timing.of([]) is None
    assert [format_seconds(x) for x in (0.0004, 0.18, 1.44, 95, None)] == [
        "400µs", "180ms", "1.4s", "1.6m", "—"]  # fmt: skip


@pytest.fixture
def project(tmp_path):
    (tmp_path / "logsite.toml").write_text(
        f'[backend]\ntype = "file"\npath = "app.log"\n\n'
        f'[store]\nurl = "sqlite:///{tmp_path / "store.db"}"\n'
    )
    store = connect(f"sqlite:///{tmp_path / 'store.db'}")
    checkout, orders = ("api.py:88", "POST /checkout"), ("orders.py:41", "GET /orders/{id}")
    steady, few = ("jobs.py:17", "sync"), ("rare.py:3", "GET /rare")
    for h in range(1, 49):
        after = h <= 24  # the last 24 hours
        store.write_latency(NOW - timedelta(hours=h), {
            checkout: Timing(500, .1, 1.4 if after else .18, 3.2 if after else .31, 6.0),
            orders: Timing(150, .02, .21 if after else .045, .64 if after else .09, 1.0),
            steady: Timing(30, 1.0, 2.0, 2.5, 3.0),
            few: Timing(2, .1, 9.0 if after else .1, 9.0, 9.0),  # too few calls to judge
        })  # fmt: skip
    store.close()
    return tmp_path


def test_what_got_slower(project):
    store = connect(f"sqlite:///{project / 'store.db'}")
    try:
        found = slower(store, NOW - timedelta(hours=48), NOW - timedelta(hours=24), NOW,
                       LatencyConfig())  # fmt: skip
    finally:
        store.close()
    assert [(s.site, s.endpoint) for s in found] == [
        ("api.py:88", "POST /checkout"), ("orders.py:41", "GET /orders/{id}")]  # fmt: skip
    assert found[0].ratio == pytest.approx(1.4 / 0.18)
    assert found[0].after["n"] == 500 * 24


def test_cli_slow(project, capsys):
    assert main(["--config-dir", str(project), "slow", "--color", "never"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Slower in the last 24h, against the 24h before"
    assert lines[2].split() == ["#", "P95", "P99", "CALLS", "WHERE", "LOG", "CALL"]
    assert "180ms → 1.4s" in lines[4] and "310ms → 3.2s" in lines[4]
    assert "12,000" in lines[4] and "POST /checkout" in lines[4] and "api.py:88" in lines[4]
    assert "45ms → 210ms" in lines[5]
    assert len(lines) == 6 and max(len(x) for x in lines) <= 100
    assert main(["--config-dir", str(project), "slow", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["endpoint"] for s in data["slower"]] == ["POST /checkout", "GET /orders/{id}"]


def test_cli_slow_without_timings(tmp_path, capsys):
    (tmp_path / "logsite.toml").write_text(f'[store]\nurl = "sqlite:///{tmp_path / "s.db"}"\n')
    assert main(["--config-dir", str(tmp_path), "slow"]) == 0
    assert "No timings stored yet" in capsys.readouterr().out


def test_watch_stores_an_hours_timings(tmp_path):
    from logsite.backends import from_config
    from logsite.watch import _line_counts

    hour = NOW - timedelta(hours=1)
    lines = [json.dumps({"timestamp": (hour + timedelta(seconds=i)).isoformat(),
                         "msg": "request", "duration_ms": 10 * (i + 1), "route": "/checkout",
                         "method": "POST"}) for i in range(20)]  # fmt: skip
    lines.append(json.dumps({"timestamp": hour.isoformat(), "msg": "no timing here"}))
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    backend = from_config({"type": "file", "path": str(tmp_path / "app.log")})
    store = connect(f"sqlite:///{tmp_path / 's.db'}")
    try:
        _line_counts(store, backend, [], NOW + timedelta(minutes=5), 5000)
        (row,) = store.latency(hour, NOW)
    finally:
        store.close()
    assert (row["site"], row["endpoint"], row["n"]) == ("", "POST /checkout", 20)
    assert row["p50"] == pytest.approx(0.1) and row["p95"] == pytest.approx(0.19)
    assert row["max"] == pytest.approx(0.2)
