"""Export to a data warehouse: each check's new errors, appended to BigQuery or Snowflake.

The error store answers the app's pages; a warehouse is where analytics teams join errors with
everything else. After each ``logsite watch`` check, the new occurrences are appended to
``logsite_error_events``, and a snapshot of each group seen to ``logsite_error_groups`` (one row
per group per check: take the latest ``exported_at`` per ``id``). The dataset or schema and
the tables are created when missing. An export that fails is reported and retried on nothing:
the errors stay in the error store, which ``logsite db`` can read.

::

    [export]
    type = "bigquery"                # credentials: Application Default Credentials
    project = "acme-analytics"
    dataset = "logsite"

    [export]
    type = "snowflake"
    account = "acme-xy12345"
    user = "LOGSITE"
    password_env = "SNOWFLAKE_PASSWORD"
    warehouse = "COMPUTE_WH"
    database = "ANALYTICS"
    schema = "LOGSITE"
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .store import _dt, _need

EVENTS = "logsite_error_events"
GROUPS = "logsite_error_groups"
EVENT_COLUMNS = [
    ("event_key", "STRING"), ("group_id", "STRING"), ("at", "TIMESTAMP"),
    ("service", "STRING"), ("level", "STRING"), ("release", "STRING"),
    ("trace_id", "STRING"), ("message", "STRING"), ("traceback", "STRING"),
    ("fields", "STRING"), ("project", "STRING"), ("exported_at", "TIMESTAMP"),
]  # fmt: skip
GROUP_COLUMNS = [
    ("id", "STRING"), ("title", "STRING"), ("exc_type", "STRING"), ("root_cause", "STRING"),
    ("raised_at", "STRING"), ("raised_in", "STRING"), ("logged_at", "STRING"),
    ("first_seen", "TIMESTAMP"), ("last_seen", "TIMESTAMP"), ("count_in_check", "INT64"),
    ("project", "STRING"), ("exported_at", "TIMESTAMP"),
]  # fmt: skip
SNOWFLAKE_TYPES = {"STRING": "VARCHAR", "TIMESTAMP": "TIMESTAMP_TZ", "INT64": "NUMBER"}


@dataclass
class ExportConfig:
    type: str | None = None  # bigquery | snowflake; None: no export
    settings: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping) -> ExportConfig:
        data = dict(data)
        kind = data.pop("type", None)
        if kind not in (None, "bigquery", "snowflake"):
            raise ValueError(f"[export] type must be bigquery or snowflake, not {kind!r}")
        for key in [k for k in data if k.endswith("_env")]:
            data.setdefault(key[: -len("_env")], os.environ.get(data.pop(key)) or None)
        need = {"bigquery": ("project", "dataset"),
                "snowflake": ("account", "user", "database", "schema")}.get(kind, ())  # fmt: skip
        missing = [k for k in need if not data.get(k)]
        if missing:
            raise ValueError(f"[export] {kind} needs {', '.join(missing)}")
        return cls(kind, data)


def rows(project: str, events: Iterable[dict], groups: Iterable, now: datetime):
    """The rows to append: ``(event rows, group rows)``, as plain values."""
    at = now.astimezone(timezone.utc)
    ev = [{**{c: e.get(c) for c, _ in EVENT_COLUMNS[:10]}, "at": _dt(e.get("at")),
           "project": project, "exported_at": at} for e in events]  # fmt: skip
    gr = [{
        "id": g.id, "title": g.title, "exc_type": g.exc_type,
        "root_cause": g.causes[0] if g.causes else None, "raised_at": g.raised_at,
        "raised_in": g.raised_in, "logged_at": g.logged_at.id if g.logged_at else None,
        "first_seen": g.first_seen, "last_seen": g.last_seen, "count_in_check": g.count,
        "project": project, "exported_at": at,
    } for g in groups]  # fmt: skip
    return ev, gr


class BigQueryExport:
    def __init__(self, settings: dict, client=None) -> None:
        self.project, self.dataset = settings["project"], settings["dataset"]
        self.location = settings.get("location")
        if client is None:
            bigquery = _need("google.cloud.bigquery", "bigquery")
            client = bigquery.Client(project=self.project)
        self.client = client
        self._ready = False

    def _table(self, name: str) -> str:
        return f"{self.project}.{self.dataset}.{name}"

    def _ensure(self) -> None:
        if self._ready:
            return
        from google.cloud import bigquery

        dataset = bigquery.Dataset(f"{self.project}.{self.dataset}")
        if self.location:
            dataset.location = self.location
        self.client.create_dataset(dataset, exists_ok=True)
        for name, cols in ((EVENTS, EVENT_COLUMNS), (GROUPS, GROUP_COLUMNS)):
            schema = [bigquery.SchemaField(c, t) for c, t in cols]
            table = bigquery.Table(self._table(name), schema=schema)
            self.client.create_table(table, exists_ok=True)
        self._ready = True

    def append(self, events: list[dict], groups: list[dict]) -> None:
        self._ensure()
        from google.cloud import bigquery

        config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
        for name, data in ((EVENTS, events), (GROUPS, groups)):
            if data:  # a load job: free, unlike streaming inserts
                self.client.load_table_from_json(
                    [_jsonable(r) for r in data], self._table(name), job_config=config
                ).result()


class SnowflakeExport:
    def __init__(self, settings: dict, connection=None) -> None:
        self.schema = settings["schema"]
        if connection is None:
            snowflake = _need("snowflake.connector", "snowflake")
            connection = snowflake.connect(
                account=settings["account"], user=settings["user"],
                password=settings.get("password"), authenticator=settings.get("authenticator"),
                private_key_file=settings.get("private_key_file"),
                warehouse=settings.get("warehouse"), database=settings["database"],
                role=settings.get("role"), paramstyle="qmark",
            )  # fmt: skip
        self.db = connection
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        cur = self.db.cursor()
        cur.execute(f"create schema if not exists {self.schema}")
        for name, cols in ((EVENTS, EVENT_COLUMNS), (GROUPS, GROUP_COLUMNS)):
            body = ", ".join(f"{c} {SNOWFLAKE_TYPES[t]}" for c, t in cols)
            cur.execute(f"create table if not exists {self.schema}.{name} ({body})")
        self._ready = True

    def append(self, events: list[dict], groups: list[dict]) -> None:
        self._ensure()
        cur = self.db.cursor()
        for name, cols, data in ((EVENTS, EVENT_COLUMNS, events), (GROUPS, GROUP_COLUMNS, groups)):
            if data:
                names = [c for c, _ in cols]
                cur.executemany(
                    f"insert into {self.schema}.{name} ({', '.join(names)})"
                    f" values ({', '.join('?' * len(names))})",
                    [[r.get(c) for c in names] for r in data],
                )
        self.db.commit()


_open: dict[str, object] = {}  # one client per settings, kept between checks


def exporter(cfg: ExportConfig):
    if cfg.type is None:
        return None
    key = f"{cfg.type}:{sorted(cfg.settings.items())}"
    if key not in _open:
        _open[key] = (BigQueryExport if cfg.type == "bigquery" else SnowflakeExport)(cfg.settings)
    return _open[key]


def _jsonable(row: dict) -> dict:
    return {k: (v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items()}  # fmt: skip
