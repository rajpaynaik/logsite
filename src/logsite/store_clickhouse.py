"""The error store in ClickHouse, for teams with a great many errors.

ClickHouse doesn't update rows, so every write here is an insert: groups, line counts and
settings live in ``ReplacingMergeTree`` tables that keep the newest version of each key, and
reads use ``FINAL`` to see only that. Events are deduplicated on insert (the existing keys are
looked up first), and by the table too. Same interface as :class:`store.Store`.

``clickhouse://user:password@host:8123/database`` (``clickhouses://`` for HTTPS);
``pip install 'logsite[clickhouse]'``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

from .store import BATCH, SCHEMA_VERSION, _clean, _dt, _earliest, _latest, _need, _safe

SCHEMA = [
    """create table if not exists store_meta (
    key String, value String, version UInt64
) engine = ReplacingMergeTree(version) order by key""",
    """create table if not exists error_groups (
    id String, fingerprint String, title Nullable(String), exc_type Nullable(String),
    root_cause Nullable(String), raised_at Nullable(String), raised_in Nullable(String),
    logged_at Nullable(String), origin Nullable(String),
    first_seen Nullable(DateTime64(6, 'UTC')), last_seen Nullable(DateTime64(6, 'UTC')),
    events UInt64, version UInt64
) engine = ReplacingMergeTree(version) order by id""",
    """create table if not exists error_events (
    event_key String, group_id String, at DateTime64(6, 'UTC'),
    service Nullable(String), level Nullable(String), release Nullable(String),
    trace_id Nullable(String), message Nullable(String), traceback Nullable(String),
    fields Nullable(String)
) engine = ReplacingMergeTree partition by toYYYYMM(at) order by (group_id, event_key)""",
    """create table if not exists line_counts (
    site String, hour DateTime64(6, 'UTC'), level Nullable(String), template Nullable(String),
    lines UInt64, bytes UInt64
) engine = ReplacingMergeTree order by (site, hour)""",
    """create table if not exists alerts (
    id String, group_id String, kind String, sent_at DateTime64(6, 'UTC'),
    channels String, message String
) engine = MergeTree order by (group_id, sent_at)""",
]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _version() -> int:
    return time.time_ns()


def _utc(value) -> datetime | None:
    dt = _dt(value)
    return dt.astimezone(timezone.utc) if dt else None


class ClickHouseStore:
    def __init__(self, url: str) -> None:
        clickhouse_connect = _need("clickhouse_connect", "clickhouse")
        u = urlparse(url)
        secure = u.scheme.lower() == "clickhouses"
        self.client = clickhouse_connect.get_client(
            host=u.hostname or "localhost", port=u.port or (8443 if secure else 8123),
            username=unquote(u.username or "default"), password=unquote(u.password or ""),
            database=u.path.lstrip("/") or "default", secure=secure,
        )  # fmt: skip
        self.location = _safe(url)
        self.dialect = None
        self.migrate()

    def _query(self, sql: str, params: Mapping | None = None) -> list[dict]:
        result = self.client.query(sql, parameters=params or {})
        return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]

    def close(self) -> None:
        self.client.close()

    # --- schema -------------------------------------------------------------------------

    def schema_version(self) -> int:
        value = self.get_meta("schema_version")
        return int(value) if value else 0

    def migrate(self) -> int:
        for statement in SCHEMA:
            self.client.command(statement)
        if self.schema_version() < SCHEMA_VERSION:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        return SCHEMA_VERSION

    # --- writing ------------------------------------------------------------------------

    def write(self, groups: Iterable, events: Iterable[dict]) -> int:
        events = {row["event_key"]: row for row in events}
        keys = list(events)
        existing: set[str] = set()
        for i in range(0, len(keys), BATCH):
            rows = self._query(
                "select event_key from error_events where event_key in {k:Array(String)}",
                {"k": keys[i : i + BATCH]},
            )
            existing |= {r["event_key"] for r in rows}
        new_rows = [row for key, row in events.items() if key not in existing]
        if new_rows:
            self.client.insert(
                "error_events",
                [[r["event_key"], r["group_id"], _utc(r["at"]) or EPOCH, r["service"],
                  r["level"], r["release"], r["trace_id"], r["message"], r["traceback"],
                  r["fields"]] for r in new_rows],
                column_names=["event_key", "group_id", "at", "service", "level", "release",
                              "trace_id", "message", "traceback", "fields"],
            )  # fmt: skip
        added: dict[str, int] = {}
        for row in new_rows:
            added[row["group_id"]] = added.get(row["group_id"], 0) + 1
            row["_new"] = True
        groups = list(groups)
        if groups:
            known = {r["id"]: r for r in self._query(
                "select * from error_groups final where id in {ids:Array(String)}",
                {"ids": [g.id for g in groups]})}  # fmt: skip
            version = _version()
            rows = []
            for g in groups:
                old = known.get(g.id) or {}
                rows.append([
                    g.id, g.fingerprint, g.title, g.exc_type,
                    g.causes[0] if g.causes else None, g.raised_at, g.raised_in,
                    g.logged_at.id if g.logged_at else None, g.origin,
                    _utc(_earliest(old.get("first_seen"), g.first_seen)),
                    _utc(_latest(old.get("last_seen"), g.last_seen)),
                    int(old.get("events") or 0) + added.get(g.id, 0), version,
                ])  # fmt: skip
            self.client.insert(
                "error_groups", rows,
                column_names=["id", "fingerprint", "title", "exc_type", "root_cause",
                              "raised_at", "raised_in", "logged_at", "origin", "first_seen",
                              "last_seen", "events", "version"],
            )  # fmt: skip
        return len(new_rows)

    def write_line_counts(self, hour: datetime, usage: Mapping, sites: Mapping) -> None:
        if not usage:
            return
        h = _utc(hour)
        self.client.insert(
            "line_counts",
            [[sid, h, sites[sid].level if sid in sites else None,
              sites[sid].template if sid in sites else None, u.count, u.bytes]
             for sid, u in usage.items()],
            column_names=["site", "hour", "level", "template", "lines", "bytes"],
        )  # fmt: skip

    def record_alert(self, group_id: str, kind: str, channels: list[str], message: str,
                     at: datetime) -> None:  # fmt: skip
        self.client.insert(
            "alerts", [[uuid.uuid4().hex, group_id, kind, _utc(at), ",".join(channels),
                        message or ""]],
            column_names=["id", "group_id", "kind", "sent_at", "channels", "message"],
        )  # fmt: skip

    def last_alert(self, group_id: str, kind: str) -> datetime | None:
        rows = self._query(
            "select max(sent_at) as at, count() as n from alerts"
            " where group_id = {g:String} and kind = {k:String}",
            {"g": group_id, "k": kind},
        )
        return _utc(rows[0]["at"]) if rows and rows[0]["n"] else None

    def get_meta(self, key: str) -> str | None:
        rows = self._query("select value from store_meta final where key = {k:String}",
                           {"k": key})  # fmt: skip
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.client.insert("store_meta", [[key, value, _version()]],
                           column_names=["key", "value", "version"])  # fmt: skip

    def prune(self, retention_days: float, now: datetime) -> int:
        cutoff = _utc(now - timedelta(days=retention_days))
        n = self._query("select count() as n from error_events where at < {c:DateTime64(6)}",
                        {"c": cutoff})[0]["n"]  # fmt: skip
        if n:
            self.client.command("alter table error_events delete where at < {c:DateTime64(6)}",
                                parameters={"c": cutoff})  # fmt: skip
            self.client.command("alter table line_counts delete where hour < {c:DateTime64(6)}",
                                parameters={"c": cutoff})  # fmt: skip
        return int(n)

    # --- reading ------------------------------------------------------------------------

    def _range(self, since, until, service=None, release=None) -> tuple[str, dict]:
        far = datetime(2200, 1, 1, tzinfo=timezone.utc)
        where = "at >= {since:DateTime64(6)} and at <= {until:DateTime64(6)}"
        params: dict = {"since": _utc(since or EPOCH), "until": _utc(until or far)}
        if service:
            where += " and service = {service:String}"
            params["service"] = service
        if release:
            where += " and release = {release:String}"
            params["release"] = release
        return where, params

    def groups(self, since: datetime | None = None, limit: int = 100, *,
               until: datetime | None = None, service: str | None = None,
               release: str | None = None, search: str | None = None) -> list[dict]:  # fmt: skip
        where, params = self._range(since, until, service, release)
        sql = (
            "select g.*, r.n as recent, r.first_in, r.last_in from"
            " (select * from error_groups final) g inner join"
            " (select group_id, count() as n, min(at) as first_in, max(at) as last_in"
            f" from error_events final where {where} group by group_id) r on r.group_id = g.id"
        )
        if search:
            sql += (" where positionCaseInsensitive(concat(coalesce(g.title, ''), ' ',"
                    " coalesce(g.root_cause, ''), ' ', coalesce(g.raised_at, '')),"
                    " {q:String}) > 0")  # fmt: skip
            params["q"] = search
        params["n"] = int(limit)
        rows = self._query(sql + " order by r.last_in desc limit {n:UInt32}", params)
        return [_clean({k: v for k, v in r.items() if k != "version"}) for r in rows]

    def facets(self, since: datetime | None = None, until: datetime | None = None) -> dict:
        where, params = self._range(since, until)
        out = {}
        for column, name in (("service", "services"), ("release", "releases")):
            rows = self._query(
                f"select {column} as value, count() as n from error_events final"
                f" where {where} and {column} is not null group by {column}"
                " order by n desc limit 50", params)  # fmt: skip
            out[name] = [{"value": r["value"], "n": int(r["n"])} for r in rows]
        return out

    def group(self, group_id: str) -> dict | None:
        rows = self._query("select * from error_groups final where id = {i:String}",
                           {"i": group_id})  # fmt: skip
        return _clean({k: v for k, v in rows[0].items() if k != "version"}) if rows else None

    def find(self, prefix: str, limit: int = 10) -> list[dict]:
        rows = self._query(
            "select * from error_groups final where startsWith(id, {p:String})"
            " order by last_seen desc limit {n:UInt32}",
            {"p": "".join(c for c in prefix if c not in "%_[\\"), "n": int(limit)},  # as in SQL
        )
        return [_clean({k: v for k, v in r.items() if k != "version"}) for r in rows]

    def breakdown(self, group_id: str) -> list[dict]:
        rows = self._query(
            "select release, service, count() as n, min(at) as first_at, max(at) as last_at"
            " from error_events final where group_id = {g:String} group by release, service",
            {"g": group_id},
        )
        return sorted(({**_clean(r), "n": int(r["n"])} for r in rows), key=lambda r: -r["n"])

    def events(self, group_id: str, limit: int = 50, *, since: datetime | None = None,
               until: datetime | None = None, service: str | None = None,
               release: str | None = None) -> list[dict]:  # fmt: skip
        where, params = self._range(since, until, service, release)
        rows = self._query(
            f"select * from error_events final where group_id = {{g:String}} and {where}"
            " order by at desc limit {n:UInt32}",
            {**params, "g": group_id, "n": int(limit)},
        )
        return [_clean(r) for r in rows]

    def hourly(self, group_id: str, since: datetime) -> list[dict]:
        rows = self._query(
            "select formatDateTime(toStartOfHour(at), '%Y-%m-%d %H') as hour, count() as n"
            " from error_events final where group_id = {g:String} and at >= {s:DateTime64(6)}"
            " group by hour order by hour",
            {"g": group_id, "s": _utc(since)},
        )
        return [{"hour": r["hour"], "n": int(r["n"])} for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("error_groups", "error_events", "line_counts", "alerts"):
            final = "" if t == "alerts" else " final"
            out[t] = int(self._query(f"select count() as n from {t}{final}")[0]["n"])
        return out
