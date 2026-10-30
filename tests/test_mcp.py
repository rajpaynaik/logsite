import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("mcp")

from conftest import V2, git  # noqa: E402
from mcp import Client  # noqa: E402

from logsite.config import load_config  # noqa: E402
from logsite.mcp_server import build_server  # noqa: E402


def call(root, tool, args=None):
    """Call one tool in-process; return (is_error, structured result or error text)."""

    async def run():
        async with Client(build_server(load_config(root))) as client:
            r = await client.call_tool(tool, args or {})
            if r.is_error:
                return True, r.content[0].text
            out = r.structured_content
            # Non-object results (lists) come wrapped as {"result": ...}
            return False, out["result"] if set(out) == {"result"} else out

    return asyncio.run(run())


@pytest.fixture
def project(repo):
    """The git repo from conftest plus a file backend: charge() spiked, refund() is flat."""
    now = datetime.now(timezone.utc)

    def line(minutes_ago, lineno, msg):
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        return json.dumps(
            {"timestamp": ts, "message": msg, "pathname": "/srv/app/pay.py", "lineno": lineno}
        )

    lines = [line(10 + i, 8, f"charging order {i}") for i in range(20)]  # current hour
    lines += [line(90, 8, "charging order 99")]  # previous hour
    lines += [line(15, 13, "refunding order 1")] * 5 + [line(80, 13, "refunding order 2")] * 5
    (repo / "prod.log").write_text("\n".join(lines) + "\n")
    (repo / "logsite.toml").write_text(
        'window_hours = 1\n[backend]\ntype = "file"\npath = "prod.log"\n'
        "[lint]\nspike_ratio = 5\nspike_min_count = 10\n"
    )
    # A change to charge() inside the comparison window: the likely culprit.
    (repo / "app" / "pay.py").write_text(V2.replace("* 2", "* 3"))
    recent = (now - timedelta(minutes=30)).isoformat()
    git(repo, "commit", "-qam", "triple the total", date=recent)
    return repo


def test_tools_are_read_only(project):
    async def run():
        async with Client(build_server(load_config(project))) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(run())}
    assert set(tools) == {
        "production_context",
        "reproduction_case",
        "list_log_calls",
        "list_rules",
        "lint",
        "log_counts",
        "log_costs",
        "context_coverage",
        "structure_score",
        "spikes",
        "compare_deploy",
        "request_timeline",
        "find_leaks",
        "error_summary",
        "investigate",
        "log_history",
        "stored_errors",
        "error_case",
        "error_occurrences",
        "error_trend",
        "release_health",
    }
    assert all(t.annotations.read_only_hint for t in tools.values())


def test_list_log_calls_and_counts(project):
    err, calls = call(project, "list_log_calls", {"path_contains": "pay"})
    assert not err and [c["id"] for c in calls] == ["app/pay.py:8", "app/pay.py:13"]
    err, counts = call(project, "log_counts")
    assert not err
    assert [(c["id"], c["count"]) for c in counts] == [("app/pay.py:8", 20), ("app/pay.py:13", 5)]


def test_spikes_include_recent_commits(project):
    err, spikes = call(project, "spikes")
    assert not err
    (spike,) = spikes  # refund() is flat
    assert spike["id"] == "app/pay.py:8"
    assert (spike["count"], spike["previous"], spike["trend"]) == (20, 1, "20.0x")
    assert [c["summary"] for c in spike["commits"]] == ["triple the total"]
    # overrides beat the config
    assert call(project, "spikes", {"min_count": 1000}) == (False, [])


def test_lint_volume_reports_spike(project):
    err, diags = call(project, "lint", {"volume": True})
    assert not err
    assert ("app/pay.py", 8) in [(d["path"], d["line"]) for d in diags if d["code"] == "LL102"]


def test_log_history_and_errors(project):
    err, commits = call(project, "log_history", {"target": "app/pay.py:8", "max_count": 2})
    assert not err
    assert [c["summary"] for c in commits] == ["triple the total", "double the total"]
    err, msg = call(project, "log_history", {"target": "app/pay.py"})
    assert err and "expected PATH:LINE" in msg and "'app/pay.py'" in msg
    err, msg = call(project, "lint", {"paths": ["../elsewhere"]})
    assert err and "outside the project root" in msg


def test_backend_tools_fail_cleanly_without_backend(repo):
    err, msg = call(repo, "log_counts")
    assert err and "no [backend] configured" in msg
