"""The error store: every production error logsite sees, in the team's own database.

``logsite watch`` (and ``logsite app``) write here as they check the logs, so the errors can be
analysed later with SQL, Grafana, Metabase or a notebook, long after the log store has dropped
the lines:

- ``error_groups``: one row per cause (title, where it was raised and logged, first and last
  seen, how many times in all);
- ``error_events``: every occurrence (time, service, release, trace id, the message and stack
  trace, masked, and the record's other fields);
- ``line_counts``: per log call, per hour: how many lines and bytes (volume and cost);
- ``alerts``: what was sent where, and when.

Databases, by ``[store] url``:

====================  =========================================  ============================
SQLite (the default)  a file path, or ``sqlite:///path``          built in
Postgres              ``postgresql://user:pass@host/db``         ``logsite[postgres]``
MySQL / MariaDB       ``mysql://user:pass@host/db``              ``logsite[mysql]``
SQL Server            ``mssql://user:pass@host/db``              ``logsite[mssql]`` + ODBC 18
ClickHouse            ``clickhouse://user:pass@host:8123/db``    ``logsite[clickhouse]``
====================  =========================================  ============================

The tables are created on first use, and upgraded when a new logsite needs more: the
database records its schema version in ``store_meta``. ``logsite db sql`` prints the SQL for a
DBA who'd rather apply it by hand. Writes are idempotent (an error read twice by overlapping
checks is stored once) and written in plain SQL every database accepts: the rows that exist
are looked up in one query, then updated or inserted.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .leaks import LeakConfig, redact
from .models import LogRecord, parse_iso
from .tracebacks import TRACEBACK_FIELDS

MAX_FIELDS_CHARS = 4000  # a record's other fields, as JSON, per event
MAX_TRACEBACK_CHARS = 8000
RELEASE_FIELDS = ("release", "service.version", "version", "app_version", "git_sha")
BATCH = 400  # keys per "which of these exist" query
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class StoreConfig:
    url: str | None = None  # see the table above; default: SQLite under $XDG_DATA_HOME
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


# --- the schema, as data: each dialect turns it into its own DDL ------------------------------

# column types: key (short, indexed text), text (long), ts, json, int, big
TABLES = {
    "error_groups": {
        "columns": [
            ("id", "key", False), ("fingerprint", "key", False), ("title", "text", True),
            ("exc_type", "mid", True), ("root_cause", "text", True), ("raised_at", "mid", True),
            ("raised_in", "mid", True), ("logged_at", "mid", True), ("origin", "mid", True),
            ("first_seen", "ts", True), ("last_seen", "ts", True), ("events", "big", False),
        ],
        "key": ["id"],
        "indexes": [("error_groups_last_seen", ["last_seen"])],
    },
    "error_events": {
        "columns": [
            ("event_key", "key", False), ("group_id", "key", False), ("at", "ts", True),
            ("service", "mid", True), ("level", "key", True), ("release", "mid", True),
            ("trace_id", "mid", True), ("message", "text", True), ("traceback", "text", True),
            ("fields", "json", True),
        ],
        "key": ["event_key"],
        "indexes": [("error_events_group_at", ["group_id", "at"]), ("error_events_at", ["at"])],
    },
    "line_counts": {
        "columns": [
            ("site", "mid", False), ("hour", "ts", False), ("level", "key", True),
            ("template", "text", True), ("lines", "big", True), ("bytes", "big", True),
        ],
        "key": ["site", "hour"],
        "indexes": [],
    },
    "alerts": {
        "columns": [
            ("id", "key", False), ("group_id", "key", True), ("kind", "key", True),
            ("sent_at", "ts", True), ("channels", "mid", True), ("message", "text", True),
        ],
        "key": ["id"],
        "indexes": [("alerts_group", ["group_id", "sent_at"])],
    },
}  # fmt: skip
META = "store_meta"
# schema version -> the tables it creates (a later version would list its ALTERs)
MIGRATIONS: dict[int, list[str]] = {1: list(TABLES)}
SCHEMA_VERSION = max(MIGRATIONS)


class Dialect:
    """What differs between databases: types, DDL, row limits, parameters, connecting."""

    name = "sql"
    param = "?"  # the placeholder the driver wants
    types = {"key": "varchar(64)", "mid": "varchar(500)", "text": "text", "ts": "timestamp",
             "json": "text", "big": "bigint"}  # fmt: skip

    def connect(self, url: str):
        raise NotImplementedError

    def create_table(self, name: str, spec: dict) -> list[str]:
        cols = [f"{c} {self.types[t]}{'' if null else ' not null'}"
                for c, t, null in spec["columns"]]  # fmt: skip
        cols.append(f"primary key ({', '.join(spec['key'])})")
        out = [f"create table if not exists {name} (\n    " + ",\n    ".join(cols) + "\n)"]
        out += [f"create index if not exists {i} on {name} ({', '.join(c)})"
                for i, c in spec["indexes"]]  # fmt: skip
        return out

    def create_meta(self) -> list[str]:
        return [f"create table if not exists {META} (key varchar(100) not null primary key,"
                f" value {self.types['text']})"]  # fmt: skip

    def limit(self, sql: str, n: int) -> str:
        return f"{sql} limit {int(n)}"

    def ts(self, value) -> object:
        """A time as the driver wants it: an aware UTC datetime by default."""
        dt = _dt(value)
        return dt

    def sql(self, statement: str) -> str:
        return statement.replace("?", self.param) if self.param != "?" else statement


class SQLite(Dialect):
    name = "sqlite"
    types = {**Dialect.types, "key": "text", "mid": "text", "ts": "text", "big": "integer"}

    def connect(self, url: str):
        path = Path(url.removeprefix("sqlite:///"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path, timeout=10)

    def ts(self, value) -> object:
        return _ts(value)  # one fixed text format, which sorts and compares as time


class Postgres(Dialect):
    name = "postgres"
    param = "%s"
    types = {**Dialect.types, "key": "text", "mid": "text", "ts": "timestamptz",
             "json": "jsonb"}  # fmt: skip

    def connect(self, url: str):
        psycopg = _need("psycopg", "postgres")
        return psycopg.connect(url, autocommit=False)


class MySQL(Dialect):
    name = "mysql"
    param = "%s"
    # utf8mb4 index keys are limited to 3072 bytes: indexed text is varchar, the rest text
    types = {**Dialect.types, "ts": "datetime(6)", "json": "json", "text": "mediumtext"}

    def connect(self, url: str):
        pymysql = _need("pymysql", "mysql")
        u = urlparse(url)
        return pymysql.connect(
            host=u.hostname or "localhost", port=u.port or 3306,
            user=unquote(u.username or ""), password=unquote(u.password or ""),
            database=u.path.lstrip("/"), charset="utf8mb4", autocommit=False,
        )  # fmt: skip

    def create_table(self, name: str, spec: dict) -> list[str]:
        cols = [f"`{c}` {self.types[t]}{'' if null else ' not null'}"
                for c, t, null in spec["columns"]]  # fmt: skip
        cols.append(f"primary key ({', '.join(f'`{c}`' for c in spec['key'])})")
        cols += [f"index {i} ({', '.join(f'`{c}`' for c in cs)})" for i, cs in spec["indexes"]]
        return [f"create table if not exists {name} (\n    " + ",\n    ".join(cols)
                + "\n) character set utf8mb4"]  # fmt: skip

    def create_meta(self) -> list[str]:
        return [f"create table if not exists {META} (`key` varchar(100) not null primary key,"
                " `value` text) character set utf8mb4"]  # fmt: skip

    def sql(self, statement: str) -> str:
        for word in ("key", "value", "lines", "release", "fields"):  # reserved in MySQL
            statement = _quote_word(statement, word, "`")
        return super().sql(statement)

    def ts(self, value) -> object:
        dt = _dt(value)
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None  # UTC, naive


class SQLServer(Dialect):
    name = "mssql"
    types = {"key": "nvarchar(64)", "mid": "nvarchar(450)", "text": "nvarchar(max)",
             "ts": "datetime2(6)", "json": "nvarchar(max)", "big": "bigint"}  # fmt: skip

    def ts(self, value) -> object:
        dt = _dt(value)  # UTC, without the zone: the driver doesn't carry one reliably
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None

    def connect(self, url: str):
        pyodbc = _need("pyodbc", "mssql")
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        driver = q.pop("driver", "ODBC Driver 18 for SQL Server")
        parts = {
            "DRIVER": "{" + driver + "}",
            "SERVER": f"{u.hostname or 'localhost'},{u.port or 1433}",
            "DATABASE": u.path.lstrip("/"),
            "UID": unquote(u.username or ""),
            "PWD": unquote(u.password or ""),
            **q,  # e.g. TrustServerCertificate=yes, Encrypt=yes
        }
        conn = ";".join(f"{k}={v}" for k, v in parts.items() if v != "")
        return pyodbc.connect(conn, autocommit=False)

    def create_table(self, name: str, spec: dict) -> list[str]:
        cols = [f"[{c}] {self.types[t]}{'' if null else ' not null'}"
                for c, t, null in spec["columns"]]  # fmt: skip
        cols.append(f"primary key ({', '.join(f'[{c}]' for c in spec['key'])})")
        out = [f"if object_id('{name}', 'U') is null create table {name} (\n    "
               + ",\n    ".join(cols) + "\n)"]  # fmt: skip
        out += [f"if not exists (select 1 from sys.indexes where name = '{i}')"
                f" create index {i} on {name} ({', '.join(f'[{c}]' for c in cs)})"
                for i, cs in spec["indexes"]]  # fmt: skip
        return out

    def create_meta(self) -> list[str]:
        return [f"if object_id('{META}', 'U') is null create table {META}"
                " ([key] nvarchar(100) not null primary key, [value] nvarchar(max))"]  # fmt: skip

    def limit(self, sql: str, n: int) -> str:
        return sql.replace("select ", f"select top {int(n)} ", 1)

    def sql(self, statement: str) -> str:
        for word in ("key", "value", "lines", "release", "fields", "level", "hour", "at"):
            statement = _quote_word(statement, word, "[]")
        return statement


DIALECTS = {
    "sqlite": SQLite, "postgresql": Postgres, "postgres": Postgres, "mysql": MySQL,
    "mariadb": MySQL, "mssql": SQLServer, "sqlserver": SQLServer,
}  # fmt: skip


def dialect_for(url: str) -> Dialect:
    scheme = url.split("://", 1)[0].split("+", 1)[0].lower() if "://" in url else "sqlite"
    if scheme not in DIALECTS:
        raise ValueError(f"unknown database {scheme!r} (supported: sqlite, postgresql, mysql, "
                         "mariadb, mssql, clickhouse)")  # fmt: skip
    return DIALECTS[scheme]()


def schema_sql(dialect: Dialect) -> list[str]:
    """Every statement that creates the store, for ``logsite db sql``."""
    out = dialect.create_meta()
    for version in sorted(MIGRATIONS):
        for table in MIGRATIONS[version]:
            out += dialect.create_table(table, TABLES[table])
    return out


def default_path(root: Path) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    key = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return Path(base) / "logsite" / "store" / f"{Path(root).name}-{key}.db"


def connect(url: str | Path) -> Store:
    """The store at ``url``: SQL databases, or ClickHouse."""
    url = str(url)
    if url.lower().startswith(("clickhouse://", "clickhouses://")):
        from .store_clickhouse import ClickHouseStore

        return ClickHouseStore(url)
    return Store(url)


def open_store(cfg) -> Store:
    return connect(cfg.store.url or default_path(cfg.root))


class Store:
    """The error store in a SQL database (see the module docstring for which)."""

    def __init__(self, url: str | Path) -> None:
        url = str(url)
        self.dialect = dialect_for(url)
        self.db = self.dialect.connect(url)
        self.location = _safe(url)
        self.migrate()

    # --- plumbing ---------------------------------------------------------------------------

    def _tx(self):
        store = self

        class Tx:
            def __enter__(self):
                self.cur = store.db.cursor()
                return self

            def execute(self, sql, params=()):
                store._execute(self.cur, sql, params)
                return self.cur

            def ddl(self, sql):  # written for the dialect already: no quoting, no parameters
                self.cur.execute(sql)

            def executemany(self, sql, rows):
                if rows:
                    self.cur.executemany(store.dialect.sql(sql), rows)

            def __exit__(self, kind, *_):
                (store.db.rollback if kind else store.db.commit)()

        return Tx()

    def _execute(self, cur, sql: str, params=()) -> None:
        sql = self.dialect.sql(sql)
        if params:
            cur.execute(sql, params)
        else:  # with parameters, a "%" in the SQL is a placeholder to MySQL's and Postgres' drivers
            cur.execute(sql)

    def _query(self, sql: str, params=()) -> list[dict]:
        cur = self.db.cursor()
        self._execute(cur, sql, params)
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        self.db.commit()  # end the read transaction
        return rows

    def _existing(self, table: str, key: str, values: list[str]) -> set[str]:
        """Which of ``values`` are already in ``table``: one query per BATCH of them."""
        found: set[str] = set()
        for i in range(0, len(values), BATCH):
            chunk = values[i : i + BATCH]
            marks = ",".join("?" * len(chunk))
            rows = self._query(f"select {key} from {table} where {key} in ({marks})", chunk)
            found |= {r[key] for r in rows}
        return found

    def close(self) -> None:
        self.db.close()

    # --- schema ---------------------------------------------------------------------------

    def schema_version(self) -> int:
        try:
            value = self.get_meta("schema_version")
        except Exception:  # no store_meta yet
            self.db.rollback()
            return 0
        return int(value) if value else 0

    def migrate(self) -> int:
        """Create the tables, or upgrade them to this logsite's schema. Returns the version."""
        with self._tx() as tx:
            for statement in self.dialect.create_meta():
                tx.ddl(statement)
        version = self.schema_version()
        for v in sorted(MIGRATIONS):
            if v <= version:
                continue
            with self._tx() as tx:
                for table in MIGRATIONS[v]:
                    for statement in self.dialect.create_table(table, TABLES[table]):
                        tx.ddl(statement)
            self.set_meta("schema_version", str(v))
            version = v
        return version

    # --- writing ----------------------------------------------------------------------------

    def write(self, groups: Iterable, events: Iterable[dict]) -> int:
        """Store a check's groups and events. Returns how many events were new."""
        d = self.dialect
        events = {row["event_key"]: row for row in events}  # one check's duplicates too
        existing = self._existing("error_events", "event_key", list(events))
        new_rows = [row for key, row in events.items() if key not in existing]
        added: dict[str, int] = {}
        for row in new_rows:
            added[row["group_id"]] = added.get(row["group_id"], 0) + 1
        groups = list(groups)
        known = {r["id"]: r for r in self._rows("error_groups", "id", [g.id for g in groups])}
        with self._tx() as tx:
            tx.executemany(
                "insert into error_events (event_key, group_id, at, service, level, release,"
                " trace_id, message, traceback, fields) values (?,?,?,?,?,?,?,?,?,?)",
                [(r["event_key"], r["group_id"], d.ts(r["at"]), r["service"], r["level"],
                  r["release"], r["trace_id"], r["message"], r["traceback"], r["fields"])
                 for r in new_rows],
            )  # fmt: skip
            for g in groups:
                old = known.get(g.id)
                first = _earliest(old and old["first_seen"], g.first_seen)
                last = _latest(old and old["last_seen"], g.last_seen)
                values = (g.title, g.exc_type, g.causes[0] if g.causes else None, g.raised_at,
                          g.raised_in, g.logged_at.id if g.logged_at else None, g.origin,
                          d.ts(first), d.ts(last))  # fmt: skip
                if old is None:
                    tx.execute(
                        "insert into error_groups (id, fingerprint, title, exc_type, root_cause,"
                        " raised_at, raised_in, logged_at, origin, first_seen, last_seen, events)"
                        " values (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (g.id, g.fingerprint, *values, added.get(g.id, 0)),
                    )
                else:
                    tx.execute(
                        "update error_groups set title = ?, exc_type = ?, root_cause = ?,"
                        " raised_at = ?, raised_in = ?, logged_at = ?, origin = ?,"
                        " first_seen = ?, last_seen = ?, events = events + ? where id = ?",
                        (*values, added.get(g.id, 0), g.id),
                    )
        for row in new_rows:
            row["_new"] = True
        return len(new_rows)

    def _rows(self, table: str, key: str, values: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(values), BATCH):
            chunk = values[i : i + BATCH]
            marks = ",".join("?" * len(chunk))
            out += self._query(f"select * from {table} where {key} in ({marks})", chunk)
        return out

    def write_line_counts(self, hour: datetime, usage: Mapping, sites: Mapping) -> None:
        """One hour's lines and bytes per log call (``usage``: site id -> Usage)."""
        h = self.dialect.ts(hour)
        with self._tx() as tx:
            tx.execute("delete from line_counts where hour = ?", (h,))
            tx.executemany(
                "insert into line_counts (site, hour, level, template, lines, bytes)"
                " values (?,?,?,?,?,?)",
                [(sid, h, sites[sid].level if sid in sites else None,
                  sites[sid].template if sid in sites else None, u.count, u.bytes)
                 for sid, u in usage.items()],
            )  # fmt: skip

    def record_alert(self, group_id: str, kind: str, channels: list[str], message: str,
                     at: datetime) -> None:  # fmt: skip
        with self._tx() as tx:
            tx.execute(
                "insert into alerts (id, group_id, kind, sent_at, channels, message)"
                " values (?,?,?,?,?,?)",
                (uuid.uuid4().hex, group_id, kind, self.dialect.ts(at), ",".join(channels),
                 message),
            )  # fmt: skip

    def last_alert(self, group_id: str, kind: str) -> datetime | None:
        rows = self._query(
            "select max(sent_at) as at from alerts where group_id = ? and kind = ?",
            (group_id, kind),
        )
        return _dt(rows[0]["at"]) if rows and rows[0]["at"] else None

    def get_meta(self, key: str) -> str | None:
        rows = self._query(f"select value from {META} where key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as tx:
            tx.execute(f"delete from {META} where key = ?", (key,))
            tx.execute(f"insert into {META} (key, value) values (?, ?)", (key, value))

    def prune(self, retention_days: float, now: datetime) -> int:
        """Delete events (and line counts) older than ``retention_days``; groups stay."""
        cutoff = self.dialect.ts(now - timedelta(days=retention_days))
        with self._tx() as tx:
            n = tx.execute("delete from error_events where at < ?", (cutoff,)).rowcount
            tx.execute("delete from line_counts where hour < ?", (cutoff,))
        return n

    # --- reading (the web page, the API, logsite db status) ---------------------------------

    def _range(self, since, until, service=None, release=None) -> tuple[list[str], list]:
        """``where`` clauses and parameters for events in a time range, filtered."""
        far = datetime(9999, 1, 1, tzinfo=timezone.utc)
        where = ["at >= ?", "at <= ?"]
        params = [self.dialect.ts(since or EPOCH), self.dialect.ts(until or far)]
        if service:
            where.append("service = ?")
            params.append(service)
        if release:
            where.append("release = ?")
            params.append(release)
        return where, params

    def groups(self, since: datetime | None = None, limit: int = 100, *,
               until: datetime | None = None, service: str | None = None,
               release: str | None = None, search: str | None = None) -> list[dict]:  # fmt: skip
        """The groups with events between ``since`` and ``until`` (and the service, release
        and text asked for), the latest first, each with ``recent``: how many there."""
        where, params = self._range(since, until, service, release)
        sql = (
            "select g.*, r.n as recent, r.first_in, r.last_in from error_groups g join"
            " (select group_id, count(*) as n, min(at) as first_in, max(at) as last_in"
            f" from error_events where {' and '.join(where)} group by group_id) r"
            " on r.group_id = g.id"
        )
        if search:
            sql += (" where (lower(g.title) like ? or lower(coalesce(g.root_cause, '')) like ?"
                    " or lower(coalesce(g.raised_at, '')) like ?)")  # fmt: skip
            params += [f"%{search.lower()}%"] * 3
        sql = self.dialect.limit(sql + " order by r.last_in desc", limit)
        return [_clean(r) for r in self._query(sql, params)]

    def facets(self, since: datetime | None = None, until: datetime | None = None) -> dict:
        """The services and releases with errors in a range, the most first: the filters."""
        where, params = self._range(since, until)
        out = {}
        for column, name in (("service", "services"), ("release", "releases")):
            rows = self._query(self.dialect.limit(
                f"select {column} as value, count(*) as n from error_events"
                f" where {' and '.join(where)} and {column} is not null"
                f" group by {column} order by n desc", 50), params)  # fmt: skip
            out[name] = [{"value": r["value"], "n": int(r["n"])} for r in rows]
        return out

    def group(self, group_id: str) -> dict | None:
        rows = self._query("select * from error_groups where id = ?", (group_id,))
        return _clean(rows[0]) if rows else None

    def find(self, prefix: str, limit: int = 10) -> list[dict]:
        """The groups whose id starts with ``prefix``, the latest first."""
        prefix = "".join(c for c in prefix if c not in "%_[\\")  # wildcards in some databases
        sql = "select * from error_groups where id like ? order by last_seen desc"
        sql = self.dialect.limit(sql, limit)
        return [_clean(r) for r in self._query(sql, (prefix + "%",))]

    def breakdown(self, group_id: str) -> list[dict]:
        """A group's occurrences per release and service, the most first: ``release``,
        ``service``, ``n``, ``first_at`` and ``last_at``."""
        rows = self._query(
            "select release, service, count(*) as n, min(at) as first_at, max(at) as last_at"
            " from error_events where group_id = ? group by release, service",
            (group_id,),
        )
        return sorted(({**_clean(r), "n": int(r["n"])} for r in rows), key=lambda r: -r["n"])

    def events(self, group_id: str, limit: int = 50, *, since: datetime | None = None,
               until: datetime | None = None, service: str | None = None,
               release: str | None = None) -> list[dict]:  # fmt: skip
        where, params = self._range(since, until, service, release)
        sql = self.dialect.limit(
            f"select * from error_events where group_id = ? and {' and '.join(where)}"
            " order by at desc", limit)  # fmt: skip
        return [_clean(r) for r in self._query(sql, [group_id, *params])]

    def hourly(self, group_id: str, since: datetime) -> list[dict]:
        """``[{"hour": "2026-10-22 08", "n": 3}]``, counted here: hours are written differently
        in every SQL dialect."""
        rows = self._query(
            "select at from error_events where group_id = ? and at >= ?",
            (group_id, self.dialect.ts(since)),
        )
        counts: dict[str, int] = {}
        for r in rows:
            at = _dt(r["at"])
            if at is not None:
                hour = at.astimezone(timezone.utc).strftime("%Y-%m-%d %H")
                counts[hour] = counts.get(hour, 0) + 1
        return [{"hour": h, "n": n} for h, n in sorted(counts.items())]

    def releases(self, limit: int = 20) -> list[dict]:
        """The releases errors were tagged with, the latest first: ``release``,
        ``first_at`` (its first error: about when it went out), ``last_at`` and ``n``."""
        rows = self._query(self.dialect.limit(
            "select release, min(at) as first_at, max(at) as last_at, count(*) as n"
            " from error_events where release is not null group by release"
            " order by first_at desc", limit))  # fmt: skip
        return [{**_clean(r), "n": int(r["n"])} for r in rows]

    def lines(self, since: datetime, until: datetime) -> int | None:
        """Log lines counted between ``since`` and ``until`` (every level), or None when
        no hour in that range was counted."""
        rows = self._query(
            "select sum(lines) as n, count(*) as counted from line_counts"
            " where hour >= ? and hour < ?",
            (self.dialect.ts(since), self.dialect.ts(until)),
        )
        return int(rows[0]["n"] or 0) if rows and int(rows[0]["counted"] or 0) else None

    def counts(self) -> dict[str, int]:
        return {t: self._query(f"select count(*) as n from {t}")[0]["n"] for t in TABLES}


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
        "service": (service or "")[:500] or None,
        "level": (level or "")[:64] or None,
        "release": (release or "")[:500] or None,
        "trace_id": (trace_id or "")[:500] or None,
        "message": redact(first, leaks)[:1000],
        "traceback": redact(text, leaks) if text else None,
        "fields": body,
    }


# --- helpers ----------------------------------------------------------------------------------


def _need(module: str, extra: str):
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise RuntimeError(
            f"a {extra} store needs {module}: pip install 'logsite[{extra}]'"
        ) from exc


def _quote_word(sql: str, word: str, quotes: str) -> str:
    """Quote a column name that's a reserved word, outside string literals: ``key`` ->
    ```key``` (MySQL) or ``[key]`` (SQL Server)."""
    import re

    left, right = (quotes[0], quotes[-1])
    return re.sub(rf"(?<![\w`\[.']){word}(?![\w`\]'(])", f"{left}{word}{right}", sql)


def _ts(value) -> str | None:
    dt = _dt(value)
    # one fixed format: compares as text in SQLite, parses as a timestamp everywhere
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00") if dt else None


def _dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    ts = parse_iso(str(value).replace(" ", "T"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _earliest(a, b) -> datetime | None:
    a, b = _dt(a), _dt(b)
    return min(x for x in (a, b) if x) if (a or b) else None


def _latest(a, b) -> datetime | None:
    a, b = _dt(a), _dt(b)
    return max(x for x in (a, b) if x) if (a or b) else None


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
