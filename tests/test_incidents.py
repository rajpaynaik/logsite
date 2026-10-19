import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from conftest import git

from logsite.cli import main
from logsite.errors import ErrorGroup
from logsite.incidents import add_context, find_incidents, incident_json
from logsite.lint import LintConfig

T0 = datetime(2026, 10, 17, 19, 22, tzinfo=timezone.utc)
CFG = LintConfig()


def group(fp, minutes, count=100, previous=0, exc=None, raised=None, traces=(), services=()):
    g = ErrorGroup(fp, f"title {fp}", exc, [], count=count, previous=previous)
    g.first_seen = T0 + timedelta(minutes=minutes)
    g.last_seen = g.first_seen + timedelta(minutes=30)
    g.raised_at = raised
    g.trace_ids = set(traces)
    for s in services:
        g.services[s] += 1
    return g


def test_groups_that_start_together_are_one_incident_led_by_the_exception():
    lag = group("lag", 0)  # a text line that happens to come first
    timeout = group("timeout", 1, exc="java.net.SocketTimeoutException", raised="a.java:58")
    failed = group("failed", 2)
    later = group("later", 90, exc="KeyError")  # 90 min later: a separate incident
    (first, second), ongoing = find_incidents([lag, timeout, failed, later], CFG)
    assert first.cause is timeout  # the earliest group with an exception, not the earliest line
    assert [g.fingerprint for g in first.effects] == ["lag", "failed"]
    assert first.reasons == {"lag": "started 1 min apart", "failed": "started 1 min apart"}
    assert first.started == T0 and first.events == 300 and first.is_new
    assert second.cause is later and ongoing == []


def test_shared_trace_ids_link_even_steady_groups_and_steady_alone_is_ongoing():
    new = group("new", 0, traces={"t1", "t2"})
    steady_linked = group("linked", -600, count=50, previous=50, traces={"t2"})
    steady = group("steady", 5, count=50, previous=50)  # started close by, but not new
    (inc,), ongoing = find_incidents([new, steady_linked, steady], CFG)
    assert {g.fingerprint for g in inc.groups} == {"new", "linked"}
    assert inc.reasons["linked"] == "same requests (1 shared trace id(s))"
    assert ongoing == [steady]


def test_different_services_starting_together_are_not_linked():
    a = group("a", 0, services=["checkout"])
    b = group("b", 1, services=["search"])
    incidents, _ = find_incidents([a, b], CFG)
    assert len(incidents) == 2


def test_deploy_and_suspect_commits_from_git(repo):
    # conftest's repo: app/pay.py committed on 2026-01-01, changed on 2026-02-01
    git(repo, "tag", "-a", "v1.3", "-m", "v1.3", date="2026-01-02T00:00:00+00:00")
    git(repo, "tag", "-a", "v1.4", "-m", "v1.4", "HEAD", date="2026-02-01T10:00:00+00:00")
    started = datetime(2026, 2, 1, 10, 30, tzinfo=timezone.utc)
    cause = group("c", 0, exc="ValueError", raised="app/pay.py:7")
    cause.first_seen = started
    (inc,), _ = find_incidents([cause], CFG)
    add_context(inc, repo, now=started)
    assert inc.deploy == ("v1.4", datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc))
    assert [c.summary for c in inc.suspects] == ["double the total"]  # in the release
    out = incident_json(inc)
    assert (
        out["deploy"]["ref"] == "v1.4"
        and out["suspect_commits"][0]["summary"] == "double the total"
    )

    # without a deploy in the 24h before, suspects come from the week before
    old = group("o", 0, exc="ValueError", raised="app/pay.py:7")
    old.first_seen = datetime(2026, 2, 5, tzinfo=timezone.utc)
    (inc,), _ = find_incidents([old], CFG)
    add_context(inc, repo, now=old.first_seen)
    assert inc.deploy is None and [c.summary for c in inc.suspects] == ["double the total"]


TRACE = """java.lang.IllegalStateException: Shipping label request failed
\tat com.example.shipping.CarrierApiClient.requestLabel(CarrierApiClient.java:3)
Caused by: java.net.SocketTimeoutException: Read timed out
\tat java.base/sun.nio.ch.NioSocketImpl.timedRead(NioSocketImpl.java:288)"""


def project(tmp_path, *, new=True):
    src = tmp_path / "src" / "main" / "java" / "com" / "example" / "shipping"
    src.mkdir(parents=True)
    (src / "CarrierApiClient.java").write_text("class C {\n\n  Label requestLabel() { ... }\n}\n")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=40)
    lines = []
    for i in range(10):
        t = (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        lines += [
            f"{t} ERROR [shipping] [traceId=tr-{i:04d}] c.e.s.Listener : failed",
            *TRACE.split("\n"),
        ]
        lines.append(f"{t} ERROR [shipping] c.e.s.Listener : Record processing failed offset={i}")
    if not new:  # the same errors in the window before too
        before = start - timedelta(hours=24)
        stamp = f"{before:%Y-%m-%d %H:%M:%S}"
        lines += [f"{stamp} ERROR [shipping] [traceId=tr-old] c.e.s.Listener : failed"]
        lines += TRACE.split("\n")
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "app.log"\n')


def test_cli_investigate(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["investigate", "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "1 incident(s) in the last 24h · 20 errors in 2 group(s)"
    assert "Cause        IllegalStateException: Shipping label request failed  (10)" in out
    assert "Root cause   SocketTimeoutException: Read timed out  (java.net)" in out
    assert "Raised at    src/main/java/com/example/shipping/CarrierApiClient.java:3" in out
    assert "Then" in out and "Record processing failed offset=<n>" in out
    assert "Example      logsite trace tr-0009" in out

    assert main(["investigate", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (inc,) = data["incidents"]
    assert inc["cause"]["raised_at"].endswith("CarrierApiClient.java:3") and inc["new"]
    assert inc["effects"][0]["linked_by"] == "started together"


def test_ci_gate_fails_on_new_groups(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--fail-on-new", "--brief"]) == 1
    assert "logsite: FAIL: 2 new error group(s) in the last 24h" in capsys.readouterr().err
    assert main(["errors", "--brief"]) == 0  # no gate: always 0
    assert main(["errors", "--fail-on-new", "--format", "json"]) == 1


def test_ci_gate_passes_when_nothing_is_new(tmp_path, monkeypatch, capsys):
    project(tmp_path, new=False)
    monkeypatch.chdir(tmp_path)
    # the record failures are new, but drop them: only the exception was there before
    (tmp_path / "app.log").write_text(
        "\n".join(x for x in (tmp_path / "app.log").read_text().splitlines() if "Record" not in x)
        + "\n"
    )
    assert main(["errors", "--fail-on-new", "--brief"]) == 0
    assert main(["errors", "--fail-on-spike", "--brief"]) == 0  # 10 vs 1: under spike_min_count


def test_mcp_investigate(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    project(tmp_path)
    err, out = call(tmp_path, "investigate", {})
    assert not err, out
    (inc,) = out["incidents"]
    assert inc["cause"]["root_cause"] == "java.net.SocketTimeoutException: Read timed out"
    assert len(inc["effects"]) == 1


def test_git_missing_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cause = group("c", 0, exc="ValueError", raised="x.py:1")
    (inc,), _ = find_incidents([cause], CFG)
    add_context(inc, tmp_path, now=T0)  # not a git repo: no deploy, no suspects, no error
    assert inc.deploy is None and inc.suspects == []


def test_time_links_dont_chain():
    # each starts 6 min after the last: within link_minutes pairwise, 18 min end to end
    a, b, c, d = (group(x, m) for x, m in (("a", 0), ("b", 6), ("c", 12), ("d", 18)))
    incidents, _ = find_incidents([a, b, c, d], CFG)
    assert [[g.fingerprint for g in i.groups] for i in incidents] == [["a", "b"], ["c", "d"]]


def test_service_in_plain_text_brackets_keeps_services_apart():
    from logsite.backends.base import parse_line
    from logsite.timeline import text_head

    def head(line):
        return text_head(parse_line(line))

    assert head("2026-10-18 14:22:31 ERROR [stock-svc] Repo - x") == ("error", "stock-svc")
    assert head("2026-10-18 21:41:52 ERROR [order-service,traceId=t1] [exec-1] X - y") == (
        "error", "order-service",
    )  # fmt: skip
    assert head("2026-10-18 21:41:53 WARN  [kafka-consumer-3] c.e.Y - z") == ("warn", None)
    assert head("2026-10-18 10:00:00 INFO [main] App - started") == ("info", None)
