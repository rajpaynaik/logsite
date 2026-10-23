"""logsite watch: every error stored, new ones sent to Slack once."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from logsite.backends import from_config
from logsite.cli import main
from logsite.config import load_config
from logsite.errors import AppFiles
from logsite.index import index_project
from logsite.store import Store, open_store
from logsite.watch import check

CHECKOUT = """\
import logging

log = logging.getLogger(__name__)


def charge(order):
    log.info("charging %s", order["id"])
    try:
        return order["total"] / order["items"]
    except ZeroDivisionError:
        log.exception("charge failed for %s", order["id"])
        raise


def refund(order):
    try:
        return gateway.refund(order["id"])
    except KeyError:
        log.exception("refund failed for %s", order["id"])
        raise
"""

DIV = """Traceback (most recent call last):
  File "/srv/app/checkout.py", line 9, in charge
    return order["total"] / order["items"]
ZeroDivisionError: division by zero"""

KEY = """Traceback (most recent call last):
  File "/srv/app/checkout.py", line 17, in refund
    return gateway.refund(order["id"])
KeyError: 'id'"""


def line(at, message, exc=None, lineno=11, **fields):
    rec = {"timestamp": at.isoformat(), "level": "ERROR" if exc else "INFO", "message": message,
           "pathname": "/srv/app/checkout.py", "lineno": lineno, **fields}  # fmt: skip
    if exc:
        rec["exc_info"] = exc
    return json.dumps(rec)


def project(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "checkout.py").write_text(CHECKOUT)
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "app.log"\n'
        '[alerts]\nslack_webhook = "https://hooks.slack.test/T/B/x"\n'
        '[watch]\nlookback = "15m"\ncooldown_minutes = 60\n'
        '[timeline]\nservice = "checkout"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    cfg = load_config(tmp_path)
    return cfg, index_project(tmp_path), AppFiles.scan(tmp_path)


def run(cfg, sites, app, store, now, sent):
    backend = from_config(cfg.backend)
    return check(cfg, backend, sites, app, store, now,
                 send=lambda url, body: sent.append((url, body)))  # fmt: skip


POSTGRES = os.environ.get("LOGSITE_TEST_POSTGRES")  # postgresql://... of a throwaway database


@pytest.fixture(params=["sqlite", "postgres"])
def store_url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "store.db")
    if not POSTGRES:
        pytest.skip("set LOGSITE_TEST_POSTGRES to a throwaway database to test postgres")
    import psycopg

    with psycopg.connect(POSTGRES, autocommit=True) as db:  # start empty
        for table in ("error_groups", "error_events", "line_counts", "alerts", "store_meta"):
            db.execute(f"drop table if exists {table}")
    return POSTGRES


def test_new_errors_are_stored_and_sent_once(tmp_path, monkeypatch, store_url):
    cfg, sites, app = project(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    log = tmp_path / "app.log"
    # failing since before logsite watched (every 5 minutes): stored, but the first check
    # doesn't page about it
    log.write_text("\n".join(
        [line(t0 + timedelta(minutes=m), "charging ORD-1", lineno=7) for m in range(3)]
        + [line(t0 + timedelta(minutes=m), f"charge failed for ORD-{m}", DIV,
                email="ann@example.com") for m in range(-30, 5, 5)]
    ) + "\n")  # fmt: skip
    store = Store(store_url)
    sent: list = []
    first = run(cfg, sites, app, store, t0 + timedelta(minutes=5), sent)
    assert first.events == 3 and first.stored == 3 and first.alerts == [] and sent == []
    # read again (overlapping windows): nothing is stored twice
    again = run(cfg, sites, app, store, t0 + timedelta(minutes=6), sent)
    assert again.stored == 0
    # a new error starts: one message, to Slack
    with log.open("a") as fh:
        for m in range(30, 33):
            fh.write(line(t0 + timedelta(minutes=m), f"refund failed for ORD-{m}", KEY,
                          lineno=19) + "\n")  # fmt: skip
    new = run(cfg, sites, app, store, t0 + timedelta(minutes=35), sent)
    assert [(g.title, kind) for g, kind in new.alerts] == [("KeyError: 'id'", "new")]
    assert new.sent == ["slack"] and len(sent) == 1
    url, body = sent[0]
    assert url == "https://hooks.slack.test/T/B/x"
    assert body["text"].startswith(":red_circle: *checkout*: 1 new error(s) (last 15 min)")
    assert "*KeyError: 'id'*  ·  3  ·  *new*" in body["text"]
    assert "raised: `app/checkout.py:17`" in body["text"]
    # the same error a minute later: within the cooldown, no second message
    with log.open("a") as fh:
        fh.write(line(t0 + timedelta(minutes=36), "refund failed for ORD-9", KEY,
                      lineno=19) + "\n")  # fmt: skip
    later = run(cfg, sites, app, store, t0 + timedelta(minutes=37), sent)
    assert later.alerts == [] and len(sent) == 1

    groups = {g["title"]: g for g in store.groups()}
    assert groups["KeyError: 'id'"]["events"] == 4
    assert groups["ZeroDivisionError: division by zero"]["raised_at"] == "app/checkout.py:9"
    (div,) = [g for g in store.groups() if g["exc_type"] == "ZeroDivisionError"]
    events = store.events(div["id"])
    assert len(events) == 3 and "ZeroDivisionError" in events[0]["traceback"]  # in the windows
    assert events[0]["fields"]["email"] != "ann@example.com"  # masked before it's stored
    alerts = store._query("select group_id, kind, channels from alerts")
    assert [(a["kind"], a["channels"]) for a in alerts] == [("new", "slack")]
    store.close()


def test_line_counts_per_hour(tmp_path, monkeypatch):
    cfg, sites, app = project(tmp_path, monkeypatch)
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    prev = hour - timedelta(hours=1)
    (tmp_path / "app.log").write_text(
        "\n".join(
            line(prev + timedelta(minutes=m), f"charging ORD-{m}", lineno=7) for m in range(5)
        )
        + "\n"  # fmt: skip
    )
    store = open_store(cfg)
    run(cfg, sites, app, store, hour + timedelta(minutes=2), [])
    rows = store._query("select site, lines, template from line_counts")
    assert rows == [{"site": "app/checkout.py:7", "lines": 5, "template": "charging %s"}]
    store.close()


def test_alert_reasons():
    from logsite.errors import ErrorGroup
    from logsite.history import GroupHistory
    from logsite.watch import WatchConfig, alert_kind

    cfg = WatchConfig()
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def group(count, previous, levels=("error",), **history):
        g = ErrorGroup("f", "E: x", "E", [], count=count, previous=previous)
        g.levels.update(levels)
        g.history = GroupHistory(**history) if history else None
        return g

    assert alert_kind(group(3, 0, known=False, watching_since=since), cfg) == "new"
    assert alert_kind(group(3, 0, known=True, regression=True, watching_since=since),
                      cfg) == "regression"  # fmt: skip
    assert alert_kind(group(80, 5, known=True, watching_since=since), cfg) == "spike"
    assert alert_kind(group(12, 2, known=True, watching_since=since), cfg) is None  # too few
    assert alert_kind(group(9, 8, known=True, watching_since=since), cfg) is None
    assert alert_kind(group(3, 0, levels=("warning",), known=False, watching_since=since),
                      cfg) is None  # a warning with a traceback  # fmt: skip


def test_store_is_idempotent_and_prunes(tmp_path):
    store = Store(tmp_path / "s.db")
    row = {"event_key": "k1", "group_id": "g1", "at": "2026-01-01 00:00:00.000000+00:00",
           "service": None, "level": "error", "release": None, "trace_id": None,
           "message": "boom", "traceback": None, "fields": "{}"}  # fmt: skip
    assert store.write([], [dict(row)]) == 1
    assert store.write([], [dict(row)]) == 0
    assert store.prune(30, datetime(2026, 3, 1, tzinfo=timezone.utc)) == 1
    store.close()


def test_cli_watch_once(tmp_path, monkeypatch, capsys):
    project(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    (tmp_path / "app.log").write_text(
        line(now - timedelta(minutes=2), "charge failed for ORD-1", DIV) + "\n"
    )
    assert main(["watch", "--once", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "logsite watch: file every 5m, errors stored in" in err
    assert "1 error(s) in 1 group(s), 1 new stored" in err
