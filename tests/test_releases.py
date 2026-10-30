"""logsite release / releases: a release's effect on errors, from the error store."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import git

from logsite.cli import main
from logsite.config import load_config
from logsite.errors import ErrorGroup
from logsite.latency import Timing
from logsite.models import Usage
from logsite.releases import ReleaseConfig, find_release, health
from logsite.store import connect

NOW = datetime.now(timezone.utc).replace(microsecond=0)
DEPLOY = NOW - timedelta(hours=6)  # v1.1; the window before it is the 6 hours before

CODE = """\
def charge(order):
    rate = RATES[order["currency"]]
    return order["amount"] * rate
"""


class Group(ErrorGroup):
    def __init__(self, fingerprint, title, first, last, raised_at="payments.py:2"):
        super().__init__(fingerprint, title, title.split(":")[0], [title])
        self.first_seen, self.last_seen = first, last
        self.raised_at, self.raised_in = raised_at, "charge"


def events(gid, times, release):
    return [{"event_key": f"{gid}-{t.isoformat()}", "group_id": gid, "at": t.isoformat(),
             "service": "payments", "level": "error", "release": release, "trace_id": None,
             "message": "failed", "traceback": None, "fields": "{}"} for t in times]  # fmt: skip


def spread(start, n, minutes=10):
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "payments.py").write_text(CODE)
    (tmp_path / "logsite.toml").write_text(
        f'[backend]\ntype = "file"\npath = "app.log"\n\n'
        f'[store]\nurl = "sqlite:///{tmp_path / "store.db"}"\n'
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "Payments", date=(DEPLOY - timedelta(days=9)).isoformat())
    git(tmp_path, "tag", "v1.0")
    (tmp_path / "payments.py").write_text(CODE.replace('["currency"]]', '["currency"]]  # new'))
    git(tmp_path, "commit", "-qam", "Look rates up directly",
        date=(DEPLOY - timedelta(hours=2)).isoformat())  # fmt: skip
    git(tmp_path, "tag", "v1.1")

    store = connect(f"sqlite:///{tmp_path / 'store.db'}")
    before, after = DEPLOY - timedelta(hours=5), DEPLOY + timedelta(minutes=1)
    long_ago = DEPLOY - timedelta(days=5)
    v10, v11 = "v1.0", "v1.1"
    groups = [
        # first seen after the release, 12 times: bad
        (Group("new0000000", "KeyError: 'JPY'", after, NOW),
         events("new00000", spread(after, 12), v11)),
        # seen 5 days ago, quiet in the 6 hours before, back now
        (Group("back000000", "ValueError: bad sku", long_ago, NOW, "catalog.py:9"),
         events("back0000", [long_ago], "v0.9") + events("back0000", spread(after, 3), "v1.1")),
        # 3 before, 25 after
        (Group("spik000000", "ConnectionError: redis", long_ago, NOW, "cache.py:4"),
         events("spik0000", spread(before, 3), v10)
         + events("spik0000", spread(after, 25, minutes=5), v11)),
        # 4 before, none after
        (Group("gone000000", "TimeoutError: slow", long_ago, DEPLOY, "api.py:7"),
         events("gone0000", spread(before, 4), "v1.0")),
        # the same before and after: not listed
        (Group("same000000", "OSError: disk", long_ago, NOW, "disk.py:3"),
         events("same0000", spread(before, 5), v10) + events("same0000", spread(after, 5), v11)),
    ]  # fmt: skip
    for g, rows in groups:
        store.write([g], rows)
    for h in range(12):  # 1,000 lines an hour, before and after
        store.write_line_counts(before + timedelta(hours=h), {"app.py:1": Usage(count=1000)}, {})
    top = DEPLOY.replace(minute=0, second=0, microsecond=0)
    for h in range(1, 5):  # p95 200ms in the hours before, 500ms after
        site = ("payments.py:2", "POST /charge")
        store.write_latency(top - timedelta(hours=h), {site: Timing(100, 0.1, 0.2, 0.3, 0.4)})
        store.write_latency(top + timedelta(hours=h), {site: Timing(100, 0.2, 0.5, 0.8, 1.0)})
    store.close()
    return tmp_path


def _health(root, ref=None, **cfg):
    store = connect(f"sqlite:///{root / 'store.db'}")
    try:
        rel = find_release(store, root, ref)
        return health(store, rel, now=NOW, cfg=ReleaseConfig(**cfg),
                      spikes=load_config(root).watch, root=root)  # fmt: skip
    finally:
        store.close()


def test_the_latest_release_by_its_errors(project):
    h = _health(project)
    assert h.release.name == "v1.1" and h.release.base == "v1.0"
    assert h.release.at == DEPLOY + timedelta(minutes=1)  # its first error
    by = {c.group["id"]: (c.status, c.before, c.after) for c in h.changes}
    assert by == {"new00000": ("new", 0, 12), "back0000": ("regressed", 0, 3),
                  "spik0000": ("spiking", 3, 25), "gone0000": ("resolved", 4, 0)}  # fmt: skip
    assert [c.status for c in h.changes] == ["new", "regressed", "spiking", "resolved"]
    assert (h.errors_before, h.errors_after) == (12, 45)
    assert h.groups_before == 3 and h.groups_after == 4
    assert h.lines_before and h.lines_after
    assert h.commit_count == 1
    assert [c.summary for c in h.changes[0].commits] == ["Look rates up directly"]
    assert h.verdict == "bad"  # a new error 12 times
    assert h.latency_before["p95"] == pytest.approx(0.2)
    assert h.latency_after["p95"] == pytest.approx(0.5) and h.slower


def test_verdicts(project):
    assert _health(project, bad_count=100, bad_rate=10).verdict == "degraded"
    # the release before: its errors are steady against nothing, but all are new
    assert _health(project, "v1.0").verdict in ("bad", "degraded")


def test_a_git_tag_with_no_errors_tagged(project):
    store = connect(f"sqlite:///{project / 'store.db'}")
    try:
        with pytest.raises(ValueError, match="unknown release 'v9'"):
            find_release(store, project, "v9")
    finally:
        store.close()


def test_cli_release_table(project, capsys):
    code = main(["--config-dir", str(project), "release", "--color", "never",
                 "--fail-on", "bad"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1  # --fail-on bad, and it is
    assert out.startswith("Release v1.1 · BAD")
    assert "1 commit since v1.0" in out
    assert "Errors/hour" in out and "Error rate" in out
    assert next(x for x in out.splitlines() if x.startswith("p95 latency")).split()[2:] == [
        "200ms", "500ms", "×2.5"]  # fmt: skip
    lines = out.splitlines()
    head = next(i for i, x in enumerate(lines) if "STATUS" in x)
    rows = lines[head + 2 : head + 6]
    assert [r.split()[1] for r in rows] == ["new", "regressed", "spiking", "resolved"]
    assert "0 → 12" in rows[0] and "KeyError: 'JPY'" in rows[0] and "payments.py:2" in rows[0]
    assert "4 → 0" in rows[3]
    assert "Suspect commits" in out and "Look rates up directly" in out
    assert max(len(x) for x in lines) <= 120


def test_cli_release_json(project, capsys):
    assert main(["--config-dir", str(project), "release", "v1.1", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["release"] == "v1.1" and data["verdict"] == "bad" and data["previous"] == "v1.0"
    assert data["groups"]["new"] == 1 and data["groups"]["resolved"] == 1
    assert data["errors"]["before"] == 12 and data["errors"]["after"] == 45
    assert data["changes"][0]["commits"][0]["summary"] == "Look rates up directly"


def test_cli_releases_list(project, capsys):
    assert main(["--config-dir", str(project), "releases", "--color", "never"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["RELEASE", "DEPLOYED", "ERRORS/H", "NEW", "BACK", "FIXED",
                                "VERDICT"]  # fmt: skip
    names = [x.split()[0] for x in lines[2:]]
    assert names[0] == "v1.1" and "v1.0" in names
    assert lines[2].split()[-1] == "BAD"


def test_mcp_release_health(project):
    pytest.importorskip("mcp")
    from test_mcp_store import call

    err, out = call(project, "release_health")
    assert not err and out["release"] == "v1.1" and out["verdict"] == "bad"
    assert [c["status"] for c in out["changes"]] == ["new", "regressed", "spiking", "resolved"]
    err, out = call(project, "release_health", {"release": "v9"})
    assert err and "unknown release 'v9'" in out


def test_watch_posts_a_bad_release_once(project):
    from logsite.watch import _release_alert

    cfg = load_config(project)
    cfg.alerts.slack_webhook = "https://hooks.example/slack"
    sent = []
    store = connect(f"sqlite:///{project / 'store.db'}")
    try:
        text = _release_alert(cfg, store, NOW, lambda url, body: sent.append(body))
        assert text.startswith(f"{project.name}: release v1.1 looks bad")
        (body,) = sent
        assert body["text"].startswith(":red_circle: ")
        slack = body["text"]
        assert "```" in slack and "Errors/hour" in slack and "0 → 12" in slack
        assert max(len(x) for x in slack.split("```")[1].splitlines()) <= 80
        # the next check: already sent, nothing new
        assert _release_alert(cfg, store, NOW + timedelta(minutes=5), sent.append) is None
        assert len(sent) == 1
        # too soon after a release: not judged yet
        cfg.release.settle_minutes = 7 * 60
        store.close()
        store = connect(f"sqlite:///{project / 'store.db'}")
        with store._tx() as tx:
            tx.execute("delete from alerts")
        assert _release_alert(cfg, store, NOW, sent.append) is None
    finally:
        store.close()
