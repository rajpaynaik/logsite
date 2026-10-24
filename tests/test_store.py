"""The error store on every database it supports (SQLite here; the others when their
LOGSITE_TEST_* URL is set, as in CI)."""

from datetime import datetime, timedelta, timezone

import pytest

from logsite.cli import main
from logsite.errors import ErrorGroup
from logsite.models import Usage
from logsite.store import SCHEMA_VERSION, connect, dialect_for, schema_sql

T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


class Group(ErrorGroup):
    """An ErrorGroup whose id is set, for these tests."""

    def __init__(self, gid, first, last, title="KeyError: 'id'"):
        super().__init__(f"fp-{gid}", title, "KeyError", ["ValueError: root"])
        self._gid = gid
        self.first_seen, self.last_seen = first, last
        self.raised_at, self.raised_in = "app/checkout.py:17", "refund"

    @property
    def id(self):
        return self._gid


def event(key, gid, at, **extra):
    return {"event_key": key, "group_id": gid, "at": at.isoformat(), "service": "checkout",
            "level": "error", "release": "v1", "trace_id": f"t-{key}", "message": "boom",
            "traceback": "Traceback ...", "fields": '{"order": 1}', **extra}  # fmt: skip


def test_the_store_on_this_database(store_url):
    store = connect(store_url)
    try:
        assert store.schema_version() == SCHEMA_VERSION
        store.close()
        store = connect(store_url)  # opening again changes nothing
        assert store.schema_version() == SCHEMA_VERSION

        g = Group("g1", T0, T0 + timedelta(minutes=5))
        rows = [event(f"e{i}", "g1", T0 + timedelta(minutes=i)) for i in range(5)]
        assert store.write([g], rows) == 5
        assert store.write([g], [dict(r) for r in rows]) == 0  # read again: stored once
        # a later check: one new event, and the group seen earlier and later than before
        later = Group("g1", T0 - timedelta(hours=1), T0 + timedelta(hours=1), "KeyError: <str>")
        assert store.write([later], [event("e9", "g1", T0 + timedelta(hours=1))]) == 1
        stored = store.group("g1")
        assert stored["events"] == 6 and stored["title"] == "KeyError: <str>"
        assert stored["first_seen"].startswith("2026-10-20 07:00:00")
        assert stored["last_seen"].startswith("2026-10-20 09:00:00")
        assert stored["root_cause"] == "ValueError: root"

        (listed,) = store.groups(since=T0 + timedelta(minutes=3))
        assert listed["id"] == "g1" and listed["recent"] == 3  # e3, e4, e9
        events = store.events("g1", limit=2)
        assert [e["event_key"] for e in events] == ["e9", "e4"]  # newest first
        assert events[0]["fields"] == {"order": 1} and events[0]["trace_id"] == "t-e9"
        assert store.hourly("g1", T0 - timedelta(days=1)) == [
            {"hour": "2026-10-20 08", "n": 5}, {"hour": "2026-10-20 09", "n": 1}]  # fmt: skip

        store.write_line_counts(T0, {"app/api.py:88": Usage(10, 2000)}, {})
        store.write_line_counts(T0, {"app/api.py:88": Usage(12, 2400)}, {})  # replaced
        store.set_meta("checked_at", "a")
        store.set_meta("checked_at", "b")
        assert store.get_meta("checked_at") == "b" and store.get_meta("nope") is None

        assert store.last_alert("g1", "new") is None
        store.record_alert("g1", "new", ["slack"], "msg", T0)
        assert store.last_alert("g1", "new") == T0 and store.last_alert("g1", "spike") is None

        assert store.counts() == {"error_groups": 1, "error_events": 6, "line_counts": 1,
                                  "alerts": 1}  # fmt: skip
        assert store.prune(1, T0 + timedelta(days=1, minutes=30)) == 5
    finally:
        store.close()


@pytest.mark.parametrize("url, words", [
    ("store.db", ["create table if not exists error_events", "fields text"]),
    ("postgresql://u@h/db", ["timestamptz", "fields jsonb"]),
    ("mysql://u@h/db", ["`fields` json", "datetime(6)", "character set utf8mb4"]),
    ("mssql://u@h/db", ["if object_id('error_events', 'U') is null", "[at] datetime2(6)"]),
])  # fmt: skip
def test_ddl_for_each_database(url, words):
    sql = "\n".join(schema_sql(dialect_for(url)))
    for word in words:
        assert word in sql


def test_reserved_words_are_quoted_outside_strings():
    mysql, mssql = dialect_for("mysql://h/d"), dialect_for("mssql://h/d")
    q = "select value from store_meta where key = ?"
    assert mysql.sql(q) == "select `value` from store_meta where `key` = %s"
    assert mssql.sql(q) == "select [value] from store_meta where [key] = ?"
    assert mysql.sql("insert into t (a) values (?)") == "insert into t (a) values (%s)"


def test_unknown_database():
    with pytest.raises(ValueError, match="unknown database 'oracle'"):
        dialect_for("oracle://h/db")


def test_cli_db(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    url = str(tmp_path / "s.db")
    assert main(["db", "init", "--url", url]) == 0
    assert f"schema version {SCHEMA_VERSION}" in capsys.readouterr().out
    assert main(["db", "status", "--url", url]) == 0
    assert "error_events" in capsys.readouterr().out
    assert main(["db", "sql", "--url", "clickhouse://h:8123/db"]) == 0
    assert "ReplacingMergeTree" in capsys.readouterr().out
