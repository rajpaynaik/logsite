"""Databricks: failed job runs read through the Jobs API, grouped and mapped to the code."""

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
from test_backends import stub_server

from logsite.backends import from_config
from logsite.backends.base import TimeRange
from logsite.cli import main

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ms(dt):
    return int(dt.timestamp() * 1000)


def ago(hours):
    return ms(NOW - timedelta(hours=hours))


# A notebook's error, as runs/get-output returns it: IPython's traceback, with colours
NOTEBOOK = (
    "\x1b[0;31m---------------------------------------------------------------------------\x1b[0m\n"
    "\x1b[0;31mKeyError\x1b[0m                                  Traceback (most recent call last)\n"
    "File \x1b[0;32m/Workspace/Repos/ana@acme.com/etl/src/transform.py:3\x1b[0m, in "
    "\x1b[0;36madd_customer\x1b[0;34m(df)\x1b[0m\n"
    '\x1b[0;32m----> 3\x1b[0m     return df["customer_id"]\n'
    "\x1b[0;31mKeyError\x1b[0m: 'customer_id'"
)
# A Python file task (an asset bundle) whose UDF failed on an executor
PY4J = """Traceback (most recent call last):
  File "/Workspace/Users/ana@acme.com/.bundle/etl/prod/files/src/main.py", line 12, in <module>
    df.write.saveAsTable("orders")
  File "/databricks/spark/python/pyspark/sql/readwriter.py", line 1586, in saveAsTable
    self._jwrite.saveAsTable(name)
py4j.protocol.Py4JJavaError: An error occurred while calling o412.saveAsTable.
: org.apache.spark.SparkException: Job aborted due to stage failure: Task 3 in stage 7.0 failed 4 times, most recent failure: Lost task 3.3 in stage 7.0 (TID 61) (10.139.64.5 executor 1): org.apache.spark.api.python.PythonException: Traceback (most recent call last):
  File "/databricks/spark/python/pyspark/worker.py", line 1247, in main
    process()
  File "/Workspace/Users/ana@acme.com/.bundle/etl/prod/files/src/udfs.py", line 3, in parse
    raise ValueError("bad amount")
ValueError: bad amount

	at org.apache.spark.api.python.BasePythonRunner$ReaderIterator.handlePythonException(PythonRunner.scala:694)
"""  # noqa: E501
STANDARD = """Traceback (most recent call last):
  File "/Workspace/Repos/ana@acme.com/etl/src/transform.py", line 3, in add_customer
    return df["customer_id"]
KeyError: 'customer_id'"""

OUTPUTS = {
    101: {"error": "KeyError: 'customer_id'", "error_trace": NOTEBOOK},
    201: {
        "error": "py4j.protocol.Py4JJavaError: An error occurred while calling o412",
        "error_trace": PY4J,
    },  # fmt: skip
    301: {"error": "KeyError: 'customer_id'", "error_trace": STANDARD},
}


def task(run_id, key, result, ended, **extra):
    return {"run_id": run_id, "task_key": key, "end_time": ms(ended),
            "state": {"life_cycle_state": "TERMINATED", "result_state": result},
            "run_page_url": f"https://dbx.example/#job/11/run/{run_id}", **extra}  # fmt: skip


RUNS = [
    {  # three tasks: one failed, one succeeded, one skipped for the failed one
        "run_id": 100, "job_id": 11, "run_name": "nightly-etl", "end_time": ago(1),
        "tasks": [task(101, "transform", "FAILED", NOW - timedelta(hours=1)),
                  task(102, "load", "SUCCESS", NOW - timedelta(hours=1)),
                  task(103, "report", "UPSTREAM_FAILED", NOW - timedelta(hours=1))],
    },
    {  # a single task that failed on a UDF
        "run_id": 200, "job_id": 11, "run_name": "nightly-etl", "end_time": ago(2),
        "tasks": [task(201, "write", "FAILED", NOW - timedelta(hours=2))],
    },
    {  # the newer status object, a job from git
        "run_id": 300, "job_id": 11, "run_name": "nightly-etl", "end_time": ago(3),
        "git_source": {"git_url": "https://github.com/acme/etl", "git_branch": "main",
                       "git_snapshot": {"used_commit": "a1b2c3d4e5f6"}},
        "tasks": [{"run_id": 301, "task_key": "transform", "end_time": ago(3),
                   "status": {"state": "TERMINATED", "termination_details": {
                       "code": "RUN_EXECUTION_ERROR", "type": "CLIENT_ERROR",
                       "message": "Workload failed"}}}],
    },
    {  # cancelled by a person: not an error
        "run_id": 400, "job_id": 11, "run_name": "nightly-etl", "end_time": ago(4),
        "tasks": [task(401, "transform", "CANCELED", NOW - timedelta(hours=4))],
    },
    {  # ended before the window
        "run_id": 500, "job_id": 11, "run_name": "nightly-etl", "end_time": ago(3 * 24),
        "tasks": [task(501, "transform", "FAILED", NOW - timedelta(days=3))],
    },
]  # fmt: skip


def respond(path, body):
    url = urllib.parse.urlparse(path)
    q = dict(urllib.parse.parse_qsl(url.query))
    if url.path == "/api/2.1/jobs/list":
        return {"jobs": [{"job_id": 11}]} if q.get("name") == "nightly-etl" else {}
    if url.path == "/api/2.1/jobs/runs/list":
        assert q["completed_only"] == "true" and q["expand_tasks"] == "true"
        if q.get("page_token") == "p2":
            return {"runs": RUNS[2:], "has_more": False}
        return {"runs": RUNS[:2], "has_more": True, "next_page_token": "p2"}
    if url.path == "/api/2.1/jobs/runs/get-output":
        return OUTPUTS[int(q["run_id"])]
    raise AssertionError(path)


@pytest.fixture
def databricks():
    with stub_server(respond) as (url, requests):
        yield url, requests


def last_day():
    return TimeRange(NOW - timedelta(hours=24), NOW)


def test_failed_tasks_become_records(databricks):
    url, requests = databricks
    backend = from_config({"type": "databricks", "host": url, "token": "dapi-x",
                           "jobs": "nightly-etl"})  # fmt: skip
    records = backend.query_errors(last_day())
    assert [r.fields["databricks.run_id"] for r in records] == [101, 201, 301]
    first = records[0]
    assert first.message == "KeyError: 'customer_id'" and first.fields["level"] == "error"
    assert (
        first.fields["service"] == "nightly-etl" and first.fields["databricks.task"] == "transform"
    )
    assert first.fields["url"] == "https://dbx.example/#job/11/run/101"
    assert first.timestamp == NOW - timedelta(hours=1)
    assert records[2].fields["git_sha"] == "a1b2c3d4e5f6"  # release health can use it
    assert records[2].fields["termination"] == "RUN_EXECUTION_ERROR"
    assert all(h.get("Authorization") == "Bearer dapi-x" for _, h, _ in requests)
    # a run's output is read once: the next check doesn't ask again
    outputs = sum("get-output" in p for p, _, _ in requests)
    backend.query_errors(last_day())
    assert sum("get-output" in p for p, _, _ in requests) == outputs


def test_a_token_and_known_jobs_are_needed(databricks):
    url, _ = databricks
    with pytest.raises(ValueError, match="needs a token"):
        from_config({"type": "databricks", "host": url})
    backend = from_config({"type": "databricks", "host": url, "token": "x", "jobs": "nope"})
    with pytest.raises(ValueError, match="no Databricks job named nope"):
        backend.query_errors(last_day())


def test_errors_grouped_and_mapped_to_the_repository(databricks, tmp_path, monkeypatch, capsys):
    url, _ = databricks
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "transform.py").write_text('def add_customer(df):\n    x = 1\n'
                                                   '    return df["customer_id"]\n')  # fmt: skip
    udfs = 'def parse(v):\n    if not v:\n        raise ValueError("bad amount")\n'
    (tmp_path / "src" / "udfs.py").write_text(udfs)
    (tmp_path / "src" / "main.py").write_text("from udfs import parse\n")
    (tmp_path / "logsite.toml").write_text(
        f'[backend]\ntype = "databricks"\nhost = "{url}"\ntoken_env = "DBX_TOKEN"\n'
    )
    monkeypatch.setenv("DBX_TOKEN", "dapi-x")
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--format", "json", "--no-history"]) in (0, 1)
    groups = {g["title"]: g for g in json.loads(capsys.readouterr().out)["groups"]}
    # the notebook's IPython traceback and the Repos file: one cause, at the line in the repo
    key = groups["KeyError: 'customer_id'"]
    assert key["count"] == 2 and key["raised_at"] == "src/transform.py:3"
    # the UDF's error inside Spark's Py4JJavaError, from an asset bundle's path
    udf = groups["ValueError: bad amount"]
    assert udf["raised_at"] == "src/udfs.py:3" and udf["wrapped_in"] == ["Py4JJavaError"]
