"""The error store: every production error logsite sees, in the team's own database.

``logsite watch`` (and ``logsite server``) write here as they check the logs, so the errors
can be analysed later with SQL, Grafana, Metabase or a notebook, long after the log store has
dropped the lines:

- ``error_groups``: one row per cause (title, where it was raised and logged, first and last
  seen, how many times in all);
- ``error_events``: every occurrence (time, service, release, trace id, the message and stack
  trace, masked, and the record's other fields);
- ``line_counts``: per log call, per hour: how many lines and bytes (volume and cost);
- ``alerts``: what was sent where, and when.

SQLite by default (one file, nothing to run); Postgres with ``[store] url =
"postgresql://..."`` (``pip install 'logsite[postgres]'``). Writes are idempotent: an error
read twice by overlapping checks is stored once.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .leaks import LeakConfig, redact
from .models import LogRecord, parse_iso
from .tracebacks import TRACEBACK_FIELDS

MAX_FIELDS_CHARS = 4000  # a record's other fields, as JSON, per event
MAX_TRACEBACK_CHARS = 8000
RELEASE_FIELDS = ("release", "service.version", "version", "app_version", "git_sha")


@dataclass
class StoreConfig:
    url: str | None = None  # postgresql://... ; default: SQLite under $XDG_DATA_HOME
    retention_days: float = 90.0  # events older than this are deleted; groups are kept

    @classmethod
    def from_dict(cls, data: Mapping) -> StoreConfig:
        known = set(cls.__dataclass_fields__) | {"url_env"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown [store] key(s) {sorted(unknown)}")
        values = {k: v for k, v in data.items() if k != "url_env"}
        if data.get("url_env") and not values.get("url"):
            values["url"] = os.environ.get(data["url_env"])
        return cls(**values)


SCHEMA = """
create table if not exists error_groups (
    id text primary key,                 -- logsite's group id (8 characters, stable)
    fingerprint text not null,
    title text, exc_type text, root_cause text,
    raised_at text, raised_in text, logged_at text, origin text,
    first_seen {ts}, last_seen {ts},
    events integer default 0              -- occurrences stored, in all
);
create table if not exists error_events (
    event_key text primary key,          -- the same error read twice is stored once
    group_id text not null,
    at {ts},
    service text, level text, release text, trace_id text,
    message text, traceback text,
    fields {json}
);
create index if not exists error_events_group_at on error_events (group_id, at);
create index if not exists error_events_at on error_events (at);
create table if not exists line_counts (
    site text not null,                  -- the log call, "app/api.py:88"
    hour {ts} not null,
    level text, template text,
    lines integer, bytes bigint,
    primary key (site, hour)
);
create table if not exists alerts (
    id {serial},
    group_id text, kind text,            -- new | regression | spike
    sent_at {ts}, channels text, message text
);
create index if not exists alerts_group on alerts (group_id, sent_at);
create table if not exists store_meta (key text primary key, value text);
"""


def default_path(root: Path) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    key = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return Path(base) / "logsite" / "store" / f"{Path(root).name}-{key}.db"


class Store:
    def __init__(self, url: str | Path) -> None:
        url = str(url)
        self.postgres = url.startswith(("postgresql://", "postgres://"))
        if self.postgres:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "a postgres store needs psycopg: pip install 'logsite[postgres]'"
                ) from exc
            self.db = psycopg.connect(url, autocommit=False)
            schema = SCHEMA.format(json="jsonb", serial="bigserial primary key", ts="timestamptz")
        else:
            path = Path(url.removeprefix("sqlite:///"))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10)
            schema = SCHEMA.format(
                json="text", serial="integer primary key autoincrement", ts="text"
            )
        self.location = _safe(url)
        with self._tx() as cur:
            for statement in schema.split(";"):
                if statement.strip():
                    cur.execute(statement)

    # --- plumbing ---------------------------------------------------------------------------

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def _tx(self):
        store = self

        class Tx:
            def __enter__(self):
                self.cur = store.db.cursor()
                return self

            def execute(self, sql, params=()):
                self.cur.execute(store._sql(sql), params)
                return self.cur

            def executemany(self, sql, rows):
                self.cur.executemany(store._sql(sql), rows)

            def __exit__(self, kind, *_):
                (store.db.rollback if kind else store.db.commit)()

        return Tx()

    def _query(self, sql: str, params=()) -> list[dict]:
        cur = self.db.cursor()
        cur.execute(self._sql(sql), params)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        self.db.commit()  # end the read transaction (postgres)
        return rows

    def close(self) -> None:
        self.db.close()

    # --- writing ----------------------------------------------------------------------------

    def write(self, groups: Iterable, events: Iterable[dict]) -> int:
        """Store a check's groups and events. Returns how many events were new."""
        events = list(events)
        new = 0
        with self._tx() as tx:
            for row in events:
                cur = tx.execute(
                    "insert into error_events (event_key, group_id, at, service, level, release,"
                    " trace_id, message, traceback, fields) values (?,?,?,?,?,?,?,?,?,?)"
                    " on conflict (event_key) do nothing",
                    (row["event_key"], row["group_id"], row["at"], row["service"], row["level"],
                     row["release"], row["trace_id"], row["message"], row["traceback"],
                     row["fields"]),
                )  # fmt: skip
                if cur.rowcount:
                    new += 1
                    row["_new"] = True
            added: dict[str, int] = {}
            for row in events:
                if row.get("_new"):
                    added[row["group_id"]] = added.get(row["group_id"], 0) + 1
            for g in groups:
                tx.execute(
                    "insert into error_groups (id, fingerprint, title, exc_type, root_cause,"
                    " raised_at, raised_in, logged_at, origin, first_seen, last_seen, events)"
                    " values (?,?,?,?,?,?,?,?,?,?,?,?) on conflict (id) do update set"
                    " title = excluded.title, raised_at = excluded.raised_at,"
                    " raised_in = excluded.raised_in, logged_at = excluded.logged_at,"
                    " origin = excluded.origin,"
                    " first_seen = case when error_groups.first_seen is null"
                    "   or excluded.first_seen < error_groups.first_seen"
                    "   then excluded.first_seen else error_groups.first_seen end,"
                    " last_seen = case when error_groups.last_seen is null"
                    "   or excluded.last_seen > error_groups.last_seen"
                    "   then excluded.last_seen else error_groups.last_seen end,"
                    " events = error_groups.events + excluded.events",
                    (g.id, g.fingerprint, g.title, g.exc_type,
                     g.causes[0] if g.causes else None, g.raised_at, g.raised_in,
                     g.logged_at.id if g.logged_at else None, g.origin,
                     _ts(g.first_seen), _ts(g.last_seen), added.get(g.id, 0)),
                )  # fmt: skip
        return new

    def write_line_counts(self, hour: datetime, usage: Mapping, sites: Mapping) -> None:
        """One hour's lines and bytes per log call (``usage``: site id -> Usage)."""
        with self._tx() as tx:
            for site_id, u in usage.items():
                site = sites.get(site_id)
                tx.execute(
                    "insert into line_counts (site, hour, level, template, lines, bytes)"
                    " values (?,?,?,?,?,?) on conflict (site, hour) do update set"
                    " lines = excluded.lines, bytes = excluded.bytes",
                    (site_id, _ts(hour), site.level if site else None,
                     site.template if site else None, u.count, u.bytes),
                )  # fmt: skip

    def record_alert(self, group_id: str, kind: str, channels: list[str], message: str,
                     at: datetime) -> None:  # fmt: skip
        with self._tx() as tx:
            tx.execute(
                "insert into alerts (group_id, kind, sent_at, channels, message)"
                " values (?,?,?,?,?)",
                (group_id, kind, _ts(at), ",".join(channels), message),
            )

    def last_alert(self, group_id: str, kind: str) -> datetime | None:
        rows = self._query(
            "select max(sent_at) as at from alerts where group_id = ? and kind = ?",
            (group_id, kind),
        )
        return _dt(rows[0]["at"]) if rows and rows[0]["at"] else None

    def get_meta(self, key: str) -> str | None:
        rows = self._query("select value from store_meta where key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as tx:
            tx.execute(
                "insert into store_meta (key, value) values (?, ?)"
                " on conflict (key) do update set value = excluded.value",
                (key, value),
            )

    def prune(self, retention_days: float, now: datetime) -> int:
        """Delete events (and line counts) older than ``retention_days``; groups stay."""
        cutoff = _ts(now - timedelta(days=retention_days))
        with self._tx() as tx:
            n = tx.execute("delete from error_events where at < ?", (cutoff,)).rowcount
            tx.execute("delete from line_counts where hour < ?", (cutoff,))
        return n

    # --- reading (the web page, the API) ----------------------------------------------------

    def groups(self, since: datetime | None = None, limit: int = 100) -> list[dict]:
        """Groups seen since ``since`` (all, by default), most recently seen first, each with
        how many times in that range."""
        cutoff = _ts(since or datetime(1970, 1, 1, tzinfo=timezone.utc))
        return [
            _clean(r)
            for r in self._query(
                "select g.*, count(e.event_key) as recent from error_groups g"
                " left join error_events e on e.group_id = g.id and e.at >= ?"
                " group by g.id order by g.last_seen desc limit ?",
                (cutoff, limit),
            )
        ]

    def group(self, group_id: str) -> dict | None:
        rows = self._query("select * from error_groups where id = ?", (group_id,))
        return _clean(rows[0]) if rows else None

    def events(self, group_id: str, limit: int = 50) -> list[dict]:
        return [
            _clean(r)
            for r in self._query(
                "select * from error_events where group_id = ? order by at desc limit ?",
                (group_id, limit),
            )
        ]

    def hourly(self, group_id: str, since: datetime) -> list[dict]:
        hour = "substr(cast(at as text), 1, 13)"
        return self._query(
            f"select {hour} as hour, count(*) as n from error_events"
            f" where group_id = ? and at >= ? group by {hour} order by hour",
            (group_id, _ts(since)),
        )


# --- events from records ----------------------------------------------------------------------


def event_row(group, record: LogRecord, info, service: str | None, level: str | None,
              trace_id: str | None, leaks: LeakConfig | None) -> dict:  # fmt: skip
    """One occurrence, ready to store: masked, with the record's other fields as JSON."""
    first = record.message.split("\n", 1)[0]
    text = info.text if info is not None and info.text else None
    if text and len(text) > MAX_TRACEBACK_CHARS:
        text = "…" + text[-MAX_TRACEBACK_CHARS:]
    fields = {k: v for k, v in record.fields.items()
              if k not in TRACEBACK_FIELDS and v is not None}  # fmt: skip
    release = next((str(fields[k]) for k in RELEASE_FIELDS if fields.get(k)), None)
    try:
        body = redact(json.dumps(fields, default=str, ensure_ascii=False), leaks)
    except (TypeError, ValueError):
        body = "{}"
    if len(body) > MAX_FIELDS_CHARS:
        body = json.dumps({"_truncated": body[:MAX_FIELDS_CHARS]})
    at = _ts(record.timestamp)
    key = hashlib.sha1(
        f"{group.fingerprint}|{at}|{first}|{trace_id or ''}".encode("utf-8", "replace")
    ).hexdigest()
    return {
        "event_key": key,
        "group_id": group.id,
        "at": at,
        "service": service,
        "level": level,
        "release": release,
        "trace_id": trace_id,
        "message": redact(first, leaks)[:1000],
        "traceback": redact(text, leaks) if text else None,
        "fields": body,
    }


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # one fixed format: compares as text in SQLite, parses as timestamptz in Postgres
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def _dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    ts = parse_iso(str(value).replace(" ", "T"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _clean(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = _ts(v)
        if k == "fields" and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def _safe(url: str) -> str:
    """The store's location without a password, for messages."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://…@{rest.split('@', 1)[1]}"
    return url


def open_store(cfg) -> Store:
    return Store(cfg.store.url or default_path(cfg.root))
