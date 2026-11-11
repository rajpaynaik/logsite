"""Databricks adapter: failed job runs, through the Jobs API (stdlib only).

Each failed task of a job run becomes a record: its error and stack trace (``runs/get-output``),
the job and task, when it ended, a link to the run, and the commit it ran when the job runs
from git. They go through the same grouping as errors from logs, so a notebook's IPython
traceback, a Python file's, and Spark's ``Py4JJavaError`` with the UDF's traceback inside are
all mapped to the line in your repository (``/Workspace/Repos/<user>/<repo>/src/etl.py`` or an
asset bundle's ``.bundle/<name>/<target>/files/src/etl.py`` -> ``src/etl.py``).

Needs a personal access token or a service principal's, with **Can View** on the jobs.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from ..models import LogRecord
from .base import LogBackend, TimeRange

# result states of a run or task that failed on its own (not skipped for an upstream one)
FAILED = {"FAILED", "TIMEDOUT", "INTERNAL_ERROR"}
# the same in the newer ``status`` object: termination codes that mean it failed
FAILED_CODES = {
    "RUN_EXECUTION_ERROR", "DRIVER_ERROR", "CLUSTER_ERROR", "LIBRARY_INSTALLATION_ERROR",
    "INTERNAL_ERROR", "TIMEOUT", "STORAGE_ACCESS_ERROR", "CLOUD_FAILURE",
    "INVALID_CLUSTER_REQUEST", "WORKSPACE_RUN_LIMIT_EXCEEDED",
}  # fmt: skip
PAGE = 25  # runs per request: the Jobs API's limit
LONGEST_RUN = timedelta(days=2)  # runs that started this long before a window can end in it


class Api:
    """The workspace's REST API with a token: ``get(path, **params)`` and ``post(path, body)``."""

    def __init__(self, host: str, token: str | None, timeout: float = 30.0) -> None:
        if not token:
            raise ValueError(
                "the databricks backend needs a token: set token_env, e.g. DATABRICKS_TOKEN"
            )
        host = host.strip().rstrip("/")
        self.host = host if host.startswith("http") else f"https://{host}"
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def get(self, path: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{self.host}{path}?{query}", headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def post(self, path: str, body: dict) -> dict:
        headers = {**self.headers, "Content-Type": "application/json"}
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.host}{path}", data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"{}")


class SdkApi:
    """The same, through the Databricks SDK's client: in a notebook or job it signs in as
    whoever runs it, so no token is set up (``WorkspaceClient()``)."""

    def __init__(self, client=None) -> None:
        if client is None:
            from databricks.sdk import WorkspaceClient  # on every Databricks runtime

            client = WorkspaceClient()
        self.client = client

    def get(self, path: str, **params) -> dict:
        query = {k: v for k, v in params.items() if v is not None}
        return self.client.api_client.do("GET", path, query=query) or {}

    def post(self, path: str, body: dict) -> dict:
        return self.client.api_client.do("POST", path, body=body) or {}


class DatabricksBackend(LogBackend):
    name = "databricks"

    def __init__(self, host: str | None = None, token: str | None = None,
                 jobs: list[str] | None = None, timeout: float = 30.0, *,
                 api=None) -> None:  # fmt: skip
        self.api = api or Api(host or "", token, timeout)
        self.jobs = list(jobs or [])
        self._outputs: dict[int, dict] = {}  # task run id -> its output: a run doesn't change

    def _get(self, path: str, **params) -> dict:
        return self.api.get(path, **params)

    def job_ids(self) -> list[int | None]:
        """The ids of the jobs named in ``jobs``, or ``[None]`` for every job."""
        if not self.jobs:
            return [None]
        ids = []
        for name in self.jobs:
            found = self._get("/api/2.1/jobs/list", name=name).get("jobs") or []
            ids += [j["job_id"] for j in found if j.get("job_id") is not None]
        if not ids:
            raise ValueError(f"no Databricks job named {', '.join(self.jobs)}")
        return ids

    def runs(self, time_range: TimeRange, limit: int = 5000) -> list[dict]:
        """Completed runs (with their tasks) that ended in ``time_range``."""
        start = time_range.start - LONGEST_RUN
        out: list[dict] = []
        for job_id in self.job_ids():
            token: str | None = None
            while len(out) < limit:
                page = self._get(
                    "/api/2.1/jobs/runs/list", job_id=job_id, completed_only="true",
                    expand_tasks="true", limit=PAGE, page_token=token,
                    start_time_from=_ms(start), start_time_to=_ms(time_range.end),
                )  # fmt: skip
                for run in page.get("runs") or []:
                    end = _time(run.get("end_time"))
                    if end is not None and time_range.start <= end <= time_range.end:
                        out.append(run)
                token = page.get("next_page_token")
                if not (page.get("has_more") and token):
                    break
        return out[:limit]

    def output(self, task_run_id: int) -> dict:
        if task_run_id not in self._outputs:
            self._outputs[task_run_id] = self._get("/api/2.1/jobs/runs/get-output",
                                                   run_id=task_run_id)  # fmt: skip
        return self._outputs[task_run_id]

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        records = []
        for run in self.runs(time_range, limit):
            for task in run.get("tasks") or [run]:  # a single-task run of the old API: itself
                if failed(task) and len(records) < limit:
                    records.append(task_record(run, task, self.output(task["run_id"])))
        if contains:
            records = [r for r in records if contains in r.message
                       or contains in json.dumps(r.fields, default=str)]  # fmt: skip
        return records

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        return self.query_logs(time_range, limit=limit)  # failed tasks are all errors


def failed(run: dict) -> bool:
    """A run or task that failed itself: by its ``state``, or the newer ``status``."""
    if (run.get("state") or {}).get("result_state") in FAILED:
        return True
    details = (run.get("status") or {}).get("termination_details") or {}
    return details.get("code") in FAILED_CODES


def task_record(run: dict, task: dict, output: dict) -> LogRecord:
    """One failed task as a record: the error, its stack trace, and where it ran."""
    state = task.get("state") or {}
    details = (task.get("status") or {}).get("termination_details") or {}
    error = output.get("error") or details.get("message") or state.get("state_message")
    job = run.get("run_name") or f"job {run.get('job_id')}"
    git = run.get("git_source") or {}
    fields = {
        "level": "error",
        "service": job,
        "databricks.job": job,
        "databricks.job_id": run.get("job_id"),
        "databricks.task": task.get("task_key"),
        "databricks.run_id": task.get("run_id"),
        "url": task.get("run_page_url") or run.get("run_page_url"),
        "git_sha": (git.get("git_snapshot") or {}).get("used_commit") or git.get("git_commit"),
        "git_branch": git.get("git_branch"),
        "exc_text": output.get("error_trace") or None,
        "termination": details.get("code") or state.get("result_state"),
    }
    when = _time(task.get("end_time")) or _time(run.get("end_time"))
    message = (str(error).strip().splitlines() or ["job run failed"])[0]
    return LogRecord(timestamp=when, message=message, fields={
        k: v for k, v in fields.items() if v not in (None, "")})  # fmt: skip


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _time(ms) -> datetime | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
