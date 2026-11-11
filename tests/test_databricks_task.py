"""The logsite task inside Databricks: a failed run explained on its own page, and the setup
that adds the task to jobs."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_databricks import NOTEBOOK, PY4J, STANDARD, ago

from logsite import databricks as dbx
from logsite.tracebacks import Frame

NOTEBOOKS = Path(__file__).parent.parent / "integrations" / "databricks"


class FakeApi:
    """The workspace API, from canned replies; records what was sent."""

    def __init__(self, replies: dict):
        self.replies, self.sent = replies, []

    def get(self, path, **params):
        key = (path, params.get("run_id") or params.get("job_id") or params.get("page_token"))
        return self.replies.get(key) or self.replies.get((path, None)) or {}

    def post(self, path, body):
        self.sent.append((path, body))
        return {}


def task(run_id, key, result):
    return {"run_id": run_id, "task_key": key, "end_time": ago(1),
            "state": {"result_state": result}}  # fmt: skip


RUN = {
    "run_id": 900, "job_id": 11, "run_name": "nightly-etl",
    "run_page_url": "https://dbx.example/#job/11/run/900",
    "git_source": {"git_url": "https://github.com/acme/etl.git",
                   "git_snapshot": {"used_commit": "a1b2c3d4e5f6"}},
    "tasks": [task(901, "transform", "FAILED"), task(902, "load", "FAILED"),
              task(903, "write", "FAILED"), task(904, "report", "UPSTREAM_FAILED"),
              task(905, "logsite", "SUCCESS")],
}  # fmt: skip
API = {
    ("/api/2.1/jobs/runs/get", 900): RUN,
    ("/api/2.1/jobs/runs/get-output", 901): {"error": "KeyError", "error_trace": NOTEBOOK},
    ("/api/2.1/jobs/runs/get-output", 902): {"error": "KeyError", "error_trace": STANDARD},
    ("/api/2.1/jobs/runs/get-output", 903): {"error": "Py4JJavaError", "error_trace": PY4J},
}


def test_workspace_paths_are_the_repositorys():
    files = dbx.WorkspaceFiles()
    for path, expected in [
        ("/Workspace/Repos/ana@acme.com/etl/src/transform.py", "src/transform.py"),
        ("/Workspace/Users/ana@acme.com/.bundle/etl/prod/files/src/udfs.py", "src/udfs.py"),
        ("/Workspace/Users/ana@acme.com/etl/src/load.py", "etl/src/load.py"),
        ("/databricks/spark/python/pyspark/worker.py", None),
        ("/local_disk0/.ephemeral_nfs/envs/pythonEnv-1/lib/python3.11/site-packages/x.py", None),
        ("<command-1234>", None),
    ]:  # fmt: skip
        assert files.resolve(Frame(path, 3, "f")) == expected, path


def test_a_failed_run_explained():
    e = dbx.explain_run(FakeApi(API), 900)
    assert (e.job, e.job_id, e.failed_tasks, e.commit) == ("nightly-etl", 11, 3, "a1b2c3d4e5f6")
    key, udf = e.causes
    # the notebook's and the Python file's KeyError: one cause, two tasks, the repo's line
    assert key.group.title == "KeyError: 'customer_id'" and key.tasks == ["load", "transform"]
    assert key.group.raised_at == "src/transform.py:3"
    assert key.code == 'return df["customer_id"]'
    # the UDF's error out of Spark's wrapper, at the UDF's line in the bundle
    assert udf.group.title == "ValueError: bad amount" and udf.tasks == ["write"]
    assert udf.group.raised_at == "src/udfs.py:3" and udf.group.wrapped_in == ["Py4JJavaError"]
    assert key.new is None  # no history table


def test_new_or_seen_before_from_the_history():
    first = dbx.explain_run(FakeApi(API), 900)
    earlier = datetime.now(timezone.utc) - timedelta(days=2)
    fingerprint = first.causes[0].group.fingerprint
    history = [{"fingerprint": fingerprint, "run_id": 812, "failed_at": earlier},
               *dbx.table_rows(first)]  # fmt: skip
    e = dbx.explain_run(FakeApi(API), 900, history)  # this run's own rows don't count
    key, udf = e.causes
    assert (key.new, key.earlier_runs) == (False, [812])
    assert (udf.new, udf.earlier_runs) == (True, [])


def test_the_page_on_the_run():
    page = dbx.render_html(dbx.explain_run(FakeApi(API), 900, []))
    assert "🔴 nightly-etl failed: 2 causes" in page
    assert "<code>src/transform.py:3</code> in <code>add_customer()</code>" in page
    assert "return df[&quot;customer_id&quot;]" in page  # escaped
    assert '<a href="https://github.com/acme/etl/commit/a1b2c3d4e5f6">a1b2c3d4</a>' in page
    assert "New: the first time this job failed this way" in page
    assert "(inside Py4JJavaError)" in page
    text = dbx.render_text(dbx.explain_run(FakeApi(API), 900))
    assert "raised at   src/udfs.py:3  in parse" in text


def test_history_table_rows_match_its_columns():
    e = dbx.explain_run(FakeApi(API), 900, [])
    values = dbx.table_values(e)
    assert len(values) == 2 and all(len(v) == len(dbx.TABLE_COLUMNS) for v in values)
    assert dbx.create_table_sql("main.ops.logsite_errors").startswith(
        "CREATE TABLE IF NOT EXISTS main.ops.logsite_errors (`failed_at` TIMESTAMP, "
    )


def test_the_task_runs_after_every_other_only_on_failure():
    settings = {"tasks": [
        {"task_key": "transform", "job_cluster_key": "etl"},
        {"task_key": "load", "depends_on": [{"task_key": "transform"}], "job_cluster_key": "etl"},
    ]}  # fmt: skip
    t = dbx.logsite_task(settings, "/Workspace/Shared/logsite/logsite_explain", "main.ops.errors")
    assert t["task_key"] == "logsite" and t["run_if"] == "AT_LEAST_ONE_FAILED"
    assert t["depends_on"] == [{"task_key": "transform"}, {"task_key": "load"}]
    assert t["job_cluster_key"] == "etl"  # the compute the job already has
    assert t["notebook_task"]["base_parameters"] == {"run_id": "{{job.run_id}}",
                                                     "table": "main.ops.errors"}  # fmt: skip
    serverless = dbx.logsite_task({"tasks": [{"task_key": "a"}]}, "/p")
    assert "job_cluster_key" not in serverless and "existing_cluster_id" not in serverless
    assert "table" not in serverless["notebook_task"]["base_parameters"]


def test_add_and_remove_the_task():
    job = {"settings": {"tasks": [{"task_key": "transform"}]}}
    api = FakeApi({("/api/2.1/jobs/get", 11): job})
    dbx.add_to_job(api, 11, "/p")
    ((path, body),) = api.sent
    assert path == "/api/2.1/jobs/update"
    assert [t["task_key"] for t in body["new_settings"]["tasks"]] == ["logsite"]  # merged by key
    assert dbx.remove_from_job(api, 11) is False  # it isn't there
    job["settings"]["tasks"].append({"task_key": "logsite"})
    assert dbx.remove_from_job(api, 11) is True
    assert api.sent[-1][1] == {"job_id": 11, "fields_to_remove": ["tasks/logsite"]}
    empty = FakeApi({("/api/2.1/jobs/get", 12): {"settings": {"tasks": []}}})
    with pytest.raises(ValueError, match="no tasks"):
        dbx.add_to_job(empty, 12, "/p")


def test_listing_and_applying_a_choice():
    orders = {"name": "orders", "tasks": [{"task_key": "logsite"}]}
    nightly = {"name": "Nightly", "tasks": [{"task_key": "a"}]}
    page1 = {"jobs": [{"job_id": 2, "settings": orders}], "has_more": True,
             "next_page_token": "p2"}  # fmt: skip
    page2 = {"jobs": [{"job_id": 1, "settings": nightly}]}
    api = FakeApi({("/api/2.1/jobs/list", None): page1, ("/api/2.1/jobs/list", "p2"): page2,
                   ("/api/2.1/jobs/get", 1): {"settings": nightly},
                   ("/api/2.1/jobs/get", 2): {"settings": orders}})  # fmt: skip
    jobs = dbx.list_jobs(api)
    assert [(j["name"], j["watched"]) for j in jobs] == [("Nightly", False), ("orders", True)]
    assert dbx.apply(api, jobs, {1}, True, "/p") == [("Nightly", "added")]  # orders untouched
    assert dbx.apply(api, jobs, {2}, False, "/p") == [("orders", "removed")]


@pytest.mark.parametrize("name", ["logsite_explain.py", "logsite_setup.py"])
def test_the_notebooks_are_python_and_use_what_exists(name):
    source = (NOTEBOOKS / name).read_text()
    assert source.startswith("# Databricks notebook source\n")
    tree = ast.parse(source)  # the MAGIC cells are comments
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("logsite"):
            module = __import__(node.module, fromlist=["x"])
            for alias in node.names:
                assert hasattr(module, alias.name), f"{node.module}.{alias.name}"


def test_a_failure_webhook_reaches_slack_in_seconds():
    """A job's failure notification (a webhook destination) at alert-server's /databricks."""
    import threading

    from test_alerts import call

    from logsite.alerts import AlertsConfig, Enricher, make_server

    sent = []

    def explain(run_id):
        e = dbx.explain_run(FakeApi(API), run_id)
        return dbx.render_text(e), dbx.render_slack(e)

    cfg = AlertsConfig(slack_webhook="https://hooks.slack.example/x")
    enricher = Enricher(cfg, None, send=lambda url, body: sent.append(body), explain_run=explain)
    srv = make_server("127.0.0.1", 0, enricher, token="t0ken")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/databricks"
    job = {"job_id": "11", "name": "nightly-etl"}
    failure = {"event_type": "jobs.on_failure", "workspace_id": "123",
               "run": {"run_id": "900"}, "job": job}  # fmt: skip
    try:
        assert call(url, failure)[0] == 401  # the token, as for Grafana's alerts
        status, body = call(url, failure, token="t0ken")
        assert status == 200 and body["sent"] == ["slack"] and body["run_id"] == 900
        (slack,) = sent
        assert slack["text"].startswith(":red_circle: *nightly-etl* failed: 2 causes")
        assert "<https://dbx.example/#job/11/run/900|open the run>" in slack["text"]
        assert "raised at   src/udfs.py:3  in parse" in slack["text"]
        started = {**failure, "event_type": "jobs.on_start"}
        assert call(url, started, token="t0ken")[1]["skipped"] is True
        assert len(sent) == 1
        enricher.explain_run = None  # no [backend] type = "databricks"
        status, body = call(url, failure, token="t0ken")
        assert status == 400 and 'type = "databricks"' in body["error"]
    finally:
        srv.shutdown()
