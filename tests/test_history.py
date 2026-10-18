import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from conftest import git

from logsite import history
from logsite.cli import main
from logsite.errors import ErrorGroup, hour_of
from logsite.history import GroupHistory, History, fix_finder
from logsite.report import history_rows
from logsite.term import palette

T0 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def group(fp, at, count=10, raised="app/pay.py:7"):
    g = ErrorGroup(fp, f"ValueError: {fp}", "ValueError", [], count=count, previous=0)
    g.first_seen, g.last_seen, g.raised_at = at, at + 10 * timedelta(minutes=1), raised
    g.hourly[hour_of(at)] = count
    return g


def run(store, groups, start, end):
    """One run, as history.apply does it: look up, then record and resolve."""
    known = store.lookup(groups, start)
    store.record(groups, start, end)
    store.resolve(end, 24, lambda row: ("v1.3", [{"sha": "a1b2c3d4", "summary": "retry"}]))
    return known


def test_first_time_ever_needs_history_from_before(tmp_path):
    store = History(tmp_path / "h.db")
    (first,) = run(store, [group("a", T0)], T0, T0 + H).values()
    assert not first.known and not first.new_ever  # the first run can't know

    later = T0 + 3 * H
    seen = run(store, [group("a", later), group("b", later)], later, later + H)
    assert seen["a"].known and not seen["a"].new_ever
    assert seen["a"].first_seen == T0 and seen["a"].total == 10
    assert seen["b"].new_ever and seen["b"].watching_since == T0
    # a second run over the same window still calls it new
    again = run(store, [group("b", later)], later, later + H)
    assert again["b"].known and again["b"].new_ever


def test_gone_after_watching_a_day_then_back_is_a_regression(tmp_path):
    store = History(tmp_path / "h.db")
    run(store, [group("a", T0)], T0, T0 + H)
    # an hour without it isn't "gone"
    run(store, [], T0 + H, T0 + 2 * H)
    assert store.show("a")["status"] == "active"
    # a full day watched without it is
    run(store, [], T0 + 2 * H, T0 + 30 * H)
    gone = store.show("a")
    assert gone["status"] == "resolved" and gone["resolved_release"] == "v1.3"

    back = T0 + timedelta(days=30)
    (h,) = run(store, [group("a", back)], back, back + H).values()
    assert h.regression and h.resolved_release == "v1.3" and h.regressions == 0
    assert h.fixed_by == [{"sha": "a1b2c3d4", "summary": "retry"}]
    row = store.show("a")
    assert row["status"] == "active" and row["regressions"] == 1
    assert row["previous_fix"]["release"] == "v1.3"
    # the next run over the same window still sees the regression
    (h,) = run(store, [group("a", back)], back, back + H).values()
    assert h.regression and h.resolved_release == "v1.3"


def test_gaps_between_runs_dont_resolve(tmp_path):
    store = History(tmp_path / "h.db")
    run(store, [group("a", T0)], T0, T0 + H)
    week = T0 + timedelta(days=7)  # nobody ran logsite for a week: only 1h watched since
    run(store, [], week, week + H)
    assert store.show("a")["status"] == "active"


def test_overlapping_runs_dont_double_count(tmp_path):
    store = History(tmp_path / "h.db")
    run(store, [group("a", T0)], T0, T0 + H)
    run(store, [group("a", T0)], T0, T0 + H)
    assert store.show("a")["daily"] == [("2026-08-03", 10)]


def test_fix_finder_names_the_release_and_commits(repo):
    # conftest's repo: app/pay.py committed on 2026-01-01, changed on 2026-02-01
    git(repo, "tag", "-a", "v1.3", "-m", "v1.3", date="2026-01-02T00:00:00+00:00")
    git(repo, "tag", "-a", "v1.4", "-m", "v1.4", "HEAD", date="2026-02-01T10:00:00+00:00")
    store = History(repo / "h.db")
    last = datetime(2026, 2, 1, 9, 50, tzinfo=timezone.utc)  # stopped at the v1.4 deploy
    run_start = last - H
    store.record([group("a", last - timedelta(minutes=10))], run_start, last)
    store.record([], last, last + 30 * H)
    store.resolve(last + 30 * H, 24, fix_finder(repo))
    row = store.show("a")
    assert row["resolved_release"] == "v1.4"
    assert [c["summary"] for c in row["fixed_by"]] == ["double the total"]


def test_card_rows():
    pal = palette("never", sys.stdout)
    g = group("a", T0)
    g.history = GroupHistory(
        known=True,
        first_seen=T0,
        regression=True,
        resolved_at=T0 + timedelta(days=31),
        resolved_release="v1.3",
        fixed_by=[{"sha": "a1b2c3d4", "date": "2026-09-03T10:00:00+00:00", "author": "Maya",
                   "summary": "Retry carrier timeouts"}],
    )  # fmt: skip
    assert history_rows(g, pal) == [
        ("History", "↩ came back · seen 3 Aug 2026 → 3 Sep 2026, gone after v1.3"),
        ("Fixed by", "a1b2c3d4 2026-09-03 Maya: Retry carrier timeouts"),
    ]
    g.history = GroupHistory(known=False, watching_since=T0)
    assert history_rows(g, pal) == [("History", "first time ever  (history since 3 Aug 2026)")]
    g.history = GroupHistory(known=True, first_seen=T0, first_release="v1.2", total=41230,
                             days_seen=12, watching_since=T0)  # fmt: skip
    assert history_rows(g, pal) == [
        ("History", "since 3 Aug 2026 (v1.2) · 41,230 errors on 12 day(s) before this")
    ]


# --- the commands --------------------------------------------------------------------------------


def project(tmp_path, *lines_at):
    lines = []
    for at in lines_at:
        lines += [
            f"{at:%Y-%m-%d %H:%M:%S} ERROR [shop] c.e.Shelf : lookup failed",
            "java.lang.IllegalStateException: Shelf is empty",
            "\tat com.example.Shelf.next(Shelf.java:3)",
        ]
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "app.log"\n\n[history]\npath = "h.db"\n'
    )


def test_cli_sync_backfill_then_errors_and_seen(tmp_path, monkeypatch, capsys):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    project(tmp_path, now - timedelta(days=20), now - timedelta(minutes=30))
    monkeypatch.chdir(tmp_path)
    assert main(["sync", "--backfill", "30d", "--step", "1d"]) == 0
    assert capsys.readouterr().out.startswith("recorded 1 error group(s) over 30d →")
    assert (tmp_path / "h.db").exists()

    # 20 days ago, then quiet for 19 days: gone; back 30 min ago: a regression
    assert main(["errors", "--format", "json"]) == 0
    (g,) = json.loads(capsys.readouterr().out)["groups"]
    assert g["history"]["regression"] and g["history"]["first_seen"]

    assert main(["errors", "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert "1 back" in out.splitlines()[0]
    assert "History      ↩ came back · seen" in out

    assert main(["seen", g["id"], "--color", "never"]) == 0
    out = capsys.readouterr().out
    assert "Status          active, came back 1 time(s)" in out
    assert sum("█" in line for line in out.splitlines()) == 2  # two days with errors

    with pytest.raises(SystemExit, match="no error group"):
        main(["seen", "ffffffff"])


def test_no_history_and_turned_off(tmp_path, monkeypatch, capsys):
    project(tmp_path, datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--no-history", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["groups"][0]["history"] is None
    assert not (tmp_path / "h.db").exists()
    with open(tmp_path / "logsite.toml", "a") as f:
        f.write("enabled = false\n")
    assert main(["errors", "--format", "json"]) == 0
    assert not (tmp_path / "h.db").exists()


def test_default_path_is_outside_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    path = history.default_path(tmp_path / "shop")
    assert path.parent == tmp_path / "data" / "logsite" / "history"
    assert path.name.startswith("shop-") and path.suffix == ".db"


def test_events_for_annotations(tmp_path):
    store = History(tmp_path / "h.db")
    run(store, [group("a", T0)], T0, T0 + H)
    run(store, [], T0 + H, T0 + 30 * H)
    back = T0 + timedelta(days=30)
    run(store, [group("a", back)], back, back + H)
    events = store.events(T0 - H, back + 2 * H)
    assert [e["kind"] for e in events] == ["first seen", "gone", "came back"]
    assert events[0]["time"] == int(T0.timestamp() * 1000)
    assert events[1]["text"] == "after v1.3 · a1b2c3d4 retry"
    assert events[2]["text"] == "was gone 2026-08-03, fixed by a1b2c3d4 retry"
    assert [e["kind"] for e in store.events(back - H, back + 2 * H)] == ["came back"]


def test_env_sets_the_path(tmp_path, monkeypatch, capsys):
    project(tmp_path, datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOGSITE_HISTORY", str(tmp_path / "data" / "h.db"))
    assert main(["sync"]) == 0
    assert (tmp_path / "data" / "h.db").exists() and not (tmp_path / "h.db").exists()
