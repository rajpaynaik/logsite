"""logsite inside Databricks: a task that explains why its job failed, on the run's page.

The ``logsite`` task is a notebook (``integrations/databricks``) that runs only when another
task of its job failed. It reads this run's failed tasks, groups their errors by cause, and
shows each with the line of the team's code that raised it, the task and the commit, as a page
on the run. With a history table it also says whether a cause is new for the job, and which
earlier runs failed the same way.

The task runs in the workspace, not in the team's repository, so it can't read their files;
it doesn't need to. The stack traces carry the code's workspace path and line
(``/Workspace/Repos/<user>/<repo>/src/etl.py``, an asset bundle's
``.bundle/<name>/<target>/files/src/etl.py``) and usually the line of code itself:
:class:`WorkspaceFiles` turns those into the repository's paths.

The setup notebook adds the task to the jobs someone picks (:func:`add_to_job`), and removes
it (:func:`remove_from_job`). Nothing here imports Spark: the notebooks read and write the
history table, and pass rows in and out.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from .backends.databricks import failed, task_record
from .errors import AppFiles, ErrorGroup, group_errors
from .tracebacks import Frame, extract_error

TASK_KEY = "logsite"
# Where the team's code sits in the workspace, and what's left is its path in the repository
_WORKSPACE = [
    re.compile(r"^/Workspace/Repos/[^/]+/[^/]+/(?P<path>.+)$"),  # a Git folder (Repos)
    re.compile(r"^/Workspace/Users/[^/]+/\.bundle/[^/]+/[^/]+/files/(?P<path>.+)$"),  # a bundle
    re.compile(r"^/Workspace/(?:Users|Shared)/[^/]+/(?P<path>.+)$"),  # a Git folder elsewhere
]
TABLE_COLUMNS = {  # the history table: one row per cause per failed run
    "failed_at": "TIMESTAMP", "job_id": "BIGINT", "job": "STRING", "run_id": "BIGINT",
    "run_url": "STRING", "fingerprint": "STRING", "cause": "STRING", "raised_at": "STRING",
    "raised_in": "STRING", "tasks": "STRING", "commit": "STRING", "new": "BOOLEAN",
}  # fmt: skip


class WorkspaceFiles(AppFiles):
    """The team's code as the stack traces of a job name it: anything under ``/Workspace``.
    ``/databricks/`` (Spark, the runtime), pip's environments and the standard library are
    libraries."""

    def __init__(self) -> None:
        super().__init__([])

    def resolve(self, frame: Frame) -> str | None:
        path = frame.file.replace("\\", "/")
        if frame.is_library or not path.startswith("/Workspace/"):
            return None
        for rx in _WORKSPACE:
            m = rx.match(path)
            if m:
                return m["path"]
        return None


@dataclass
class Cause:
    """One cause of a run's failure: the tasks it failed, where it was raised."""

    group: ErrorGroup
    tasks: list[str]
    code: str | None = None  # the line that raised it
    new: bool | None = None  # never seen in this job before; None without a history
    earlier_runs: list[int] = field(default_factory=list)  # the latest first


@dataclass
class Explanation:
    job: str
    job_id: int | None
    run_id: int
    run_url: str | None
    ended: datetime | None
    commit: str | None
    git_url: str | None
    causes: list[Cause]
    failed_tasks: int


def explain_run(api, run_id: int, history: list[dict] | None = None) -> Explanation:
    """Why job run ``run_id`` failed. ``api`` is :class:`~logsite.backends.databricks.SdkApi`
    in a notebook; ``history`` the history table's earlier rows for this job, if there's
    one."""
    run = api.get("/api/2.1/jobs/runs/get", run_id=run_id)
    records = []
    for task in run.get("tasks") or [run]:
        if task.get("task_key") == TASK_KEY or not failed(task):
            continue
        output = api.get("/api/2.1/jobs/runs/get-output", run_id=task["run_id"])
        records.append(task_record(run, task, output))
    tasks: dict[str, set[str]] = {}  # fingerprint -> the tasks it failed

    def collect(group, record, *_):
        tasks.setdefault(group.fingerprint, set()).add(record.fields.get("databricks.task") or "")

    found = group_errors(records, [], WorkspaceFiles(), on_event=collect)
    causes = [Cause(g, sorted(t for t in tasks.get(g.fingerprint, ()) if t), _code(g))
              for g in sorted(found.values(), key=lambda g: -g.count)]  # fmt: skip
    if history is not None:
        _with_history(causes, history, run_id)
    git = run.get("git_source") or {}
    return Explanation(
        job=run.get("run_name") or f"job {run.get('job_id')}",
        job_id=run.get("job_id"),
        run_id=run_id,
        run_url=run.get("run_page_url"),
        ended=records[0].timestamp if records else None,
        commit=(git.get("git_snapshot") or {}).get("used_commit") or git.get("git_commit"),
        git_url=git.get("git_url"),
        causes=causes,
        failed_tasks=len(records),
    )


def _code(group: ErrorGroup) -> str | None:
    """The line of code at ``group.raised_at``, from the trace itself."""
    if group.record is None or not group.raised_at:
        return None
    info = extract_error(group.record)
    path, _, line = group.raised_at.rpartition(":")
    files = WorkspaceFiles()
    for exc in info.chain if info else []:
        for f in exc.frames:
            if f.code and str(f.line) == line and files.resolve(f) == path:
                return f.code
    return None


def _with_history(causes: list[Cause], rows: list[dict], run_id: int) -> None:
    seen: dict[str, list[tuple]] = {}
    for r in rows:
        if r.get("run_id") != run_id:
            seen.setdefault(r.get("fingerprint"), []).append((r.get("failed_at"), r["run_id"]))
    for c in causes:
        earlier = sorted(seen.get(c.group.fingerprint, []), key=lambda t: str(t[0]), reverse=True)
        c.new = not earlier
        c.earlier_runs = [rid for _, rid in earlier][:5]


def table_rows(e: Explanation) -> list[dict]:
    """The history table's rows for this run: one per cause."""
    return [{
        "failed_at": e.ended, "job_id": e.job_id, "job": e.job, "run_id": e.run_id,
        "run_url": e.run_url, "fingerprint": c.group.fingerprint, "cause": c.group.title,
        "raised_at": c.group.raised_at, "raised_in": c.group.raised_in,
        "tasks": ", ".join(c.tasks), "commit": e.commit, "new": c.new,
    } for c in e.causes]  # fmt: skip


def create_table_sql(table: str) -> str:
    return f"CREATE TABLE IF NOT EXISTS {table} ({table_schema()})"


# --- the page on the run ----------------------------------------------------------------------


def commit_url(git_url: str | None, sha: str | None) -> str | None:
    if not git_url or not sha:
        return None
    base = git_url.removesuffix(".git").rstrip("/")
    if "gitlab" in base:
        return f"{base}/-/commit/{sha}"
    if "bitbucket" in base:
        return f"{base}/commits/{sha}"
    return f"{base}/commit/{sha}"


def render_html(e: Explanation) -> str:
    """The explanation as a page, for ``displayHTML`` on the run."""
    esc = html.escape
    n = len(e.causes)
    head = (f"{esc(e.job)} failed: {esc(e.causes[0].group.title)}" if n == 1
            else f"{esc(e.job)} failed: {n} causes" if n else f"{esc(e.job)} failed")  # fmt: skip
    out = [_STYLE, f'<div class="ls"><h2>🔴 {head}</h2>']
    if not n:
        out.append("<p>No failed task left an error to read: open the failed task for its "
                   "output.</p></div>")  # fmt: skip
        return "".join(out)
    commit = e.commit[:8] if e.commit else None
    link = commit_url(e.git_url, e.commit)
    for c in e.causes:
        g = c.group
        badge = ('<span class="new">New: the first time this job failed this way</span>'
                 if c.new else "")  # fmt: skip
        rows = []
        if g.raised_at:
            where = f"<code>{esc(g.raised_at)}</code>"
            if g.raised_in and g.raised_in != "?":
                where += f" in <code>{esc(g.raised_in)}()</code>"
            if g.wrapped_in:
                where += f' <span class="muted">(inside {esc(", ".join(g.wrapped_in))})</span>'
            rows.append(("Where", where))
        else:
            elsewhere = "not in a file of your repository (a notebook cell or a library)"
            rows.append(("Where", f'<span class="muted">{elsewhere}: see the failed task</span>'))
        if c.code:
            rows.append(("Code", f"<code>{esc(c.code)}</code>"))
        if g.causes:
            rows.append(("Caused by", "<br>".join(f"<code>{esc(x)}</code>" for x in g.causes)))
        rows.append(("Tasks", esc(", ".join(c.tasks)) or "—"))
        if commit:
            sha = f'<a href="{esc(link)}">{commit}</a>' if link else commit
            rows.append(("Commit", f"<code>{sha}</code>"))
        if c.earlier_runs:
            runs = ", ".join(str(r) for r in c.earlier_runs)
            rows.append(("Same cause", f"earlier runs {esc(runs)}"))
        table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
        out.append(f"<h3><code>{esc(g.title)}</code></h3>{badge}<table>{table}</table>")
    if e.run_url:
        out.append(f'<p class="muted">Run {e.run_id} · <a href="{esc(e.run_url)}">open the '
                   "run</a> · explained by logsite</p>")  # fmt: skip
    out.append("</div>")
    return "".join(out)


def render_text(e: Explanation) -> str:
    """The same, as plain text: the task's output outside a notebook, and in logs."""
    lines = [f"{e.job} run {e.run_id} failed: {len(e.causes)} cause(s)"]
    for c in e.causes:
        g = c.group
        lines += ["", g.title + ("    new: first time this job failed this way" if c.new else "")]
        if g.raised_at:
            within = f"  in {g.raised_in}" if g.raised_in and g.raised_in != "?" else ""
            lines.append(f"  raised at   {g.raised_at}{within}")
        if c.code:
            lines.append(f"              {c.code}")
        lines.append(f"  tasks       {', '.join(c.tasks) or '—'}")
        if e.commit:
            lines.append(f"  commit      {e.commit[:8]}")
        if c.earlier_runs:
            lines.append(f"  same cause  runs {', '.join(map(str, c.earlier_runs))}")
    return "\n".join(lines)


_STYLE = """<meta charset="utf-8"><style>
.ls{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:900px}
.ls h2{font-size:20px;margin:4px 0 12px}.ls h3{font-size:16px;margin:18px 0 6px}
.ls table{border-collapse:collapse;margin:6px 0}.ls th{text-align:left;color:#6b7280;
font-weight:500;padding:4px 16px 4px 0;vertical-align:top}.ls td{padding:4px 0}
.ls code{font-family:ui-monospace,Menlo,monospace;font-size:13px}.ls .muted{color:#6b7280}
.ls .new{display:inline-block;background:#fee2e2;color:#991b1b;border-radius:4px;
padding:2px 8px;font-size:12px;font-weight:600}
</style>"""


# --- adding the task to jobs ------------------------------------------------------------------


def logsite_task(settings: dict, notebook_path: str, table: str | None = None) -> dict:
    """The ``logsite`` task for a job with these settings: after every other task, only when
    one of them failed, on the compute the job already uses (serverless when it has none)."""
    tasks = [t for t in settings.get("tasks") or [] if t.get("task_key") != TASK_KEY]
    params = {"run_id": "{{job.run_id}}"}
    if table:
        params["table"] = table
    task: dict = {
        "task_key": TASK_KEY,
        "description": "Explains why the job failed (logsite). Runs only when a task failed.",
        "depends_on": [{"task_key": t["task_key"]} for t in tasks],
        "run_if": "AT_LEAST_ONE_FAILED",
        "notebook_task": {
            "notebook_path": notebook_path,
            "source": "WORKSPACE",
            "base_parameters": params,
        },  # fmt: skip
        "timeout_seconds": 600,
    }
    compute = next((t for t in tasks if t.get("job_cluster_key") or t.get("existing_cluster_id")),
                   None)  # fmt: skip
    if compute and compute.get("job_cluster_key"):
        task["job_cluster_key"] = compute["job_cluster_key"]
    elif compute:
        task["existing_cluster_id"] = compute["existing_cluster_id"]
    return task


def add_to_job(api, job_id: int, notebook_path: str, table: str | None = None) -> dict:
    """Add the ``logsite`` task to job ``job_id``, or bring it up to date. Returns it."""
    settings = api.get("/api/2.1/jobs/get", job_id=job_id).get("settings") or {}
    if not [t for t in settings.get("tasks") or [] if t.get("task_key") != TASK_KEY]:
        raise ValueError(f"job {job_id} has no tasks to watch")
    task = logsite_task(settings, notebook_path, table)
    # tasks in new_settings are merged into the job's by task_key: the others stay as they are
    api.post("/api/2.1/jobs/update", {"job_id": job_id, "new_settings": {"tasks": [task]}})
    return task


def remove_from_job(api, job_id: int) -> bool:
    """Take the ``logsite`` task off job ``job_id``. False if it had none."""
    settings = api.get("/api/2.1/jobs/get", job_id=job_id).get("settings") or {}
    if not any(t.get("task_key") == TASK_KEY for t in settings.get("tasks") or []):
        return False
    api.post("/api/2.1/jobs/update", {"job_id": job_id, "fields_to_remove": [f"tasks/{TASK_KEY}"]})
    return True


def watched(settings: dict) -> bool:
    return any(t.get("task_key") == TASK_KEY for t in settings.get("tasks") or [])


def list_jobs(api) -> list[dict]:
    """Every job in the workspace the caller can see: ``job_id``, ``name``, and whether it
    has the ``logsite`` task (``watched``)."""
    out, token = [], None
    while True:
        page = api.get("/api/2.1/jobs/list", limit=100, page_token=token, expand_tasks="true")
        for j in page.get("jobs") or []:
            settings = j.get("settings") or {}
            out.append({"job_id": j["job_id"], "name": settings.get("name") or str(j["job_id"]),
                        "watched": watched(settings)})  # fmt: skip
        token = page.get("next_page_token")
        if not (page.get("has_more") and token):
            return sorted(out, key=lambda j: j["name"].lower())


def apply(api, jobs: list[dict], picked: set[int], add: bool, notebook_path: str,
          table: str | None = None) -> list[tuple[str, str]]:  # fmt: skip
    """Add the task to the ``picked`` jobs (``add``), or take it off them: ``[(job name,
    what happened)]``. Jobs not picked are left as they are."""
    done = []
    for j in jobs:
        if j["job_id"] not in picked:
            continue
        try:
            if add:
                add_to_job(api, j["job_id"], notebook_path, table)
                done.append((j["name"], "updated" if j["watched"] else "added"))
            elif remove_from_job(api, j["job_id"]):
                done.append((j["name"], "removed"))
            else:
                done.append((j["name"], "didn't have it"))
        except Exception as exc:  # one job's permissions mustn't stop the others
            done.append((j["name"], f"failed: {exc}"))
    return done


def table_values(e: Explanation) -> list[tuple]:
    """:func:`table_rows` as tuples in the table's column order, for ``createDataFrame``."""
    return [tuple(r[k] for k in TABLE_COLUMNS) for r in table_rows(e)]


def table_schema() -> str:
    """The table's columns as Spark DDL: ``failed_at TIMESTAMP, job_id BIGINT, ...``."""
    return ", ".join(f"`{k}` {t}" for k, t in TABLE_COLUMNS.items())
