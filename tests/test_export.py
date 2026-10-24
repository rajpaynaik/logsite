"""Export to BigQuery and Snowflake, with fake client libraries that record every call."""

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from logsite.backends import from_config
from logsite.config import load_config
from logsite.errors import AppFiles
from logsite.export import EVENTS, GROUPS, ExportConfig, _open
from logsite.index import index_project
from logsite.store import open_store
from logsite.watch import check

DIV = """Traceback (most recent call last):
  File "/srv/app/checkout.py", line 2, in charge
    return order["total"] / order["items"]
ZeroDivisionError: division by zero"""


def fake_bigquery(calls):
    class Client:
        def __init__(self, project=None):
            calls.append(("client", project))

        def create_dataset(self, dataset, exists_ok):
            calls.append(("dataset", dataset.id, exists_ok))

        def create_table(self, table, exists_ok):
            calls.append(("table", table.id, [f.name for f in table.schema]))

        def load_table_from_json(self, rows, table, job_config):
            json.dumps(rows)  # what's loaded must be plain JSON
            calls.append(("load", table, rows, job_config.write_disposition))
            return types.SimpleNamespace(result=lambda: None)

    class Named:
        def __init__(self, id, schema=None):
            self.id, self.schema, self.location = id, schema or [], None

    bq = types.ModuleType("google.cloud.bigquery")
    bq.Client, bq.Dataset, bq.Table = Client, Named, Named
    bq.SchemaField = lambda name, kind: types.SimpleNamespace(name=name, kind=kind)
    bq.LoadJobConfig = lambda write_disposition: types.SimpleNamespace(
        write_disposition=write_disposition)  # fmt: skip
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = bq
    google = types.ModuleType("google")
    google.cloud = cloud
    return {"google": google, "google.cloud": cloud, "google.cloud.bigquery": bq}


def fake_snowflake(calls):
    class Cursor:
        def execute(self, sql):
            calls.append(("execute", sql))

        def executemany(self, sql, rows):
            calls.append(("insert", sql, rows))

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            calls.append(("commit",))

    connector = types.ModuleType("snowflake.connector")
    connector.connect = lambda **kw: calls.append(("connect", kw)) or Connection()
    snowflake = types.ModuleType("snowflake")
    snowflake.connector = connector
    return {"snowflake": snowflake, "snowflake.connector": connector}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "checkout.py").write_text(
        'def charge(order):\n    return order["total"] / order["items"]\n'
    )
    now = datetime.now(timezone.utc)
    (tmp_path / "app.log").write_text("\n".join(json.dumps({
        "timestamp": (now - timedelta(minutes=m)).isoformat(), "level": "ERROR",
        "message": "charge failed", "exc_info": DIV}) for m in range(3)) + "\n")  # fmt: skip
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "pw")
    _open.clear()
    yield tmp_path
    _open.clear()


def run(root, export_toml):
    (root / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "app.log"\n[timeline]\nservice = "checkout"\n'
        + export_toml
    )
    cfg = load_config(root)
    store = open_store(cfg)
    try:
        return check(cfg, from_config(cfg.backend), index_project(root), AppFiles.scan(root),
                     store, datetime.now(timezone.utc))  # fmt: skip
    finally:
        store.close()


def test_bigquery(project, monkeypatch):
    calls: list = []
    for name, module in fake_bigquery(calls).items():
        monkeypatch.setitem(sys.modules, name, module)
    result = run(project, '[export]\ntype = "bigquery"\nproject = "acme"\ndataset = "logsite"\n')
    assert result.exported == 3 and result.export_error is None
    assert ("client", "acme") in calls and ("dataset", "acme.logsite", True) in calls
    tables = {c[1]: c[2] for c in calls if c[0] == "table"}
    assert "event_key" in tables[f"acme.logsite.{EVENTS}"]
    loads = {c[1]: c for c in calls if c[0] == "load"}
    events = loads[f"acme.logsite.{EVENTS}"]
    assert len(events[2]) == 3 and events[3] == "WRITE_APPEND"  # a load job, appended
    assert events[2][0]["project"] == "checkout" and "T" in events[2][0]["at"]
    (group,) = loads[f"acme.logsite.{GROUPS}"][2]
    assert group["title"] == "ZeroDivisionError: division by zero" and group["count_in_check"] == 3
    again = run(project, '[export]\ntype = "bigquery"\nproject = "acme"\ndataset = "logsite"\n')
    assert again.exported == 0  # only what's new to the store


def test_snowflake(project, monkeypatch):
    calls: list = []
    for name, module in fake_snowflake(calls).items():
        monkeypatch.setitem(sys.modules, name, module)
    result = run(project, '[export]\ntype = "snowflake"\naccount = "acme-1"\nuser = "LOGSITE"\n'
                          'password_env = "SNOWFLAKE_PASSWORD"\ndatabase = "ANALYTICS"\n'
                          'schema = "LOGSITE"\n')  # fmt: skip
    assert result.exported == 3
    (connect,) = [c[1] for c in calls if c[0] == "connect"]
    assert connect["password"] == "pw" and connect["paramstyle"] == "qmark"
    ddl = [c[1] for c in calls if c[0] == "execute"]
    assert ddl[0] == "create schema if not exists LOGSITE"
    assert any(f"create table if not exists LOGSITE.{EVENTS}" in d and "TIMESTAMP_TZ" in d
               for d in ddl)  # fmt: skip
    inserts = {c[1].split()[2]: c[2] for c in calls if c[0] == "insert"}
    assert len(inserts[f"LOGSITE.{EVENTS}"]) == 3 and ("commit",) in calls


def test_a_failed_export_is_reported_and_watching_goes_on(project, monkeypatch):
    calls: list = []
    modules = fake_bigquery(calls)

    def boom(*a, **k):
        raise RuntimeError("quota exceeded")

    modules["google.cloud.bigquery"].Client.load_table_from_json = boom
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    result = run(project, '[export]\ntype = "bigquery"\nproject = "acme"\ndataset = "logsite"\n')
    assert result.stored == 3 and result.exported == 0
    assert result.export_error == "bigquery: quota exceeded"


def test_export_config():
    assert ExportConfig.from_dict({}).type is None
    with pytest.raises(ValueError, match="bigquery needs dataset"):
        ExportConfig.from_dict({"type": "bigquery", "project": "acme"})
    with pytest.raises(ValueError, match="bigquery or snowflake"):
        ExportConfig.from_dict({"type": "redshift"})
