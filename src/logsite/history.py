"""History store: logsite's memory of the error groups it has seen.

Each run of ``logsite errors`` / ``investigate`` / ``alert-server`` (and ``logsite sync`` on a
schedule) records the groups it found: hourly counts, first and last seen, the release at
the time. Only summaries are kept (masked titles, counts, places in the code), never log
lines. From them logsite can tell a group that's new for real from one it hasn't watched
long enough to know, and a regression: a group that stopped (with the release and commits
that probably fixed it) and came back.

One SQLite file per project, by default under ``$XDG_DATA_HOME/logsite/history``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ErrorGroup
from .models import parse_iso

RESOLVE_AFTER_HOURS = 24.0  # watched this long without a sighting: the group is gone
FIX_WINDOW = timedelta(hours=6)  # a release this soon after the last sighting fixed it
SCHEMA = """
create table if not exists groups (
    fingerprint text primary key,
    title text, exc_type text, raised_at text, logged_at text,
    first_seen text, last_seen text, first_release text,
    status text default 'active',              -- active | resolved
    resolved_at text, resolved_release text, fixed_by text,
    regressions integer default 0, reopened_at text,
    previous_fix text                          -- the fix before the last regression (json)
);
create table if not exists hours (
    fingerprint text, hour text, count integer,
    primary key (fingerprint, hour)
);
create table if not exists watched (start text, "end" text);
"""


@dataclass
class HistoryConfig:
    enabled: bool = True
    path: str | None = None  # default: under $XDG_DATA_HOME/logsite/history
    resolve_after_hours: float = RESOLVE_AFTER_HOURS

    @classmethod
    def from_dict(cls, data: Mapping) -> HistoryConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown [history] key(s) {sorted(unknown)}")
        return cls(**dict(data))


@dataclass
class GroupHistory:
    """What the history knows about one group, before this run was recorded."""

    known: bool  # recorded before
    first_seen: datetime | None = None  # ever, as far as the history goes
    first_release: str | None = None
    total: int = 0
    days_seen: int = 0
    regression: bool = False  # it had stopped and it's back
    resolved_at: datetime | None = None  # when it stopped, for a regression
    resolved_release: str | None = None
    fixed_by: list[dict] = field(default_factory=list)  # commits that probably fixed it
    regressions: int = 0  # how many times it came back before
    watching_since: datetime | None = None  # when the history began
    first_in_window: bool = False  # recorded, but only by runs over this same window

    @property
    def new_ever(self) -> bool:
        """Never seen before this window, and the history was watching long enough before
        it to know."""
        return (not self.known or self.first_in_window) and self.watching_since is not None


def default_path(root: Path) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    key = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return Path(base) / "logsite" / "history" / f"{Path(root).name}-{key}.db"


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


class History:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- reading ---------------------------------------------------------------------------
    def watching_since(self) -> datetime | None:
        row = self.db.execute("select min(start) from watched").fetchone()
        return _dt(row[0]) if row and row[0] else None

    def lookup(
        self, groups: Iterable[ErrorGroup], window_start: datetime
    ) -> dict[str, GroupHistory]:
        """What the history knew about each group before this run."""
        since = self.watching_since()
        # we can only call a group "new ever" if we were watching before this window
        watching = (
            since if since is not None and since < window_start - timedelta(hours=1) else None
        )
        out = {}
        for g in groups:
            row = self.db.execute(
                "select * from groups where fingerprint = ?", (g.fingerprint,)
            ).fetchone()
            if row is None:
                out[g.fingerprint] = GroupHistory(known=False, watching_since=watching)
                continue
            total, days = self.db.execute(
                "select coalesce(sum(count), 0), count(distinct substr(hour, 1, 10)) "
                "from hours where fingerprint = ?",
                (g.fingerprint,),
            ).fetchone()
            regression = row["status"] == "resolved"
            first = _dt(row["first_seen"])
            fix = {
                "resolved_at": row["resolved_at"],
                "release": row["resolved_release"],
                "fixed_by": json.loads(row["fixed_by"] or "[]"),
            }
            reopened = _dt(row["reopened_at"])
            if not regression and reopened is not None and reopened >= window_start:
                regression = True  # came back in this window, recorded by an earlier run
                fix = json.loads(row["previous_fix"] or "null") or fix
            out[g.fingerprint] = GroupHistory(
                known=True,
                first_seen=_dt(row["first_seen"]),
                first_release=row["first_release"],
                total=total,
                days_seen=days,
                regression=regression,
                resolved_at=_dt(fix["resolved_at"]) if regression else None,
                resolved_release=fix["release"] if regression else None,
                fixed_by=fix["fixed_by"] if regression else [],
                regressions=row["regressions"] - (1 if regression and reopened else 0),
                watching_since=watching,
                first_in_window=bool(first and first >= window_start) and not regression,
            )
        return out

    # --- writing ---------------------------------------------------------------------------
    def record(
        self,
        groups: Iterable[ErrorGroup],
        start: datetime,
        end: datetime,
        release_at=lambda ts: None,
    ) -> None:
        """Save what a run saw in ``start..end``. A resolved group that's seen again is
        reopened (a regression); its fix is kept as the previous fix."""
        with self.db:
            for g in groups:
                row = self.db.execute(
                    "select * from groups where fingerprint = ?", (g.fingerprint,)
                ).fetchone()
                first, last = _iso(g.first_seen or start), _iso(g.last_seen or end)
                if row is None:
                    self.db.execute(
                        "insert into groups (fingerprint, title, exc_type, raised_at, logged_at, "
                        "first_seen, last_seen, first_release) values (?, ?, ?, ?, ?, ?, ?, ?)",
                        (g.fingerprint, g.title, g.exc_type, g.raised_at,
                         g.logged_at.id if g.logged_at else None, first, last,
                         release_at(g.first_seen or start)),
                    )  # fmt: skip
                else:
                    reopened = row["status"] == "resolved" and last > (row["resolved_at"] or "")
                    self.db.execute(
                        "update groups set title = ?, raised_at = coalesce(?, raised_at), "
                        "logged_at = coalesce(?, logged_at), "
                        "first_seen = min(first_seen, ?), last_seen = max(last_seen, ?) "
                        "where fingerprint = ?",
                        (g.title, g.raised_at, g.logged_at.id if g.logged_at else None,
                         first, last, g.fingerprint),
                    )  # fmt: skip
                    if reopened:
                        previous = {
                            "resolved_at": row["resolved_at"],
                            "release": row["resolved_release"],
                            "fixed_by": json.loads(row["fixed_by"] or "[]"),
                        }
                        self.db.execute(
                            "update groups set status = 'active', reopened_at = ?, "
                            "regressions = regressions + 1, previous_fix = ?, resolved_at = null, "
                            "resolved_release = null, fixed_by = null where fingerprint = ?",
                            (first, json.dumps(previous), g.fingerprint),
                        )
                for hour, n in g.hourly.items():
                    # max, not sum: overlapping runs see the same records again
                    self.db.execute(
                        "insert into hours (fingerprint, hour, count) values (?, ?, ?) "
                        "on conflict (fingerprint, hour) "
                        "do update set count = max(count, excluded.count)",
                        (g.fingerprint, hour, n),
                    )
            self.db.execute(
                'insert into watched (start, "end") values (?, ?)', (_iso(start), _iso(end))
            )

    def _watched(self) -> list[tuple[datetime, datetime]]:
        """The time ranges runs have covered, merged."""
        rows = self.db.execute('select start, "end" from watched order by start').fetchall()
        merged: list[list[datetime]] = []
        for a, b in rows:
            a, b = _dt(a), _dt(b)
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    def resolve(self, now: datetime, resolve_after_hours: float, find_fix) -> list[str]:
        """Mark active groups as resolved once runs have watched for ``resolve_after_hours``
        after their last sighting without seeing them (gaps between runs don't count), with
        the release and commits that probably fixed them.
        ``find_fix(group_row) -> (release, commits)``. Returns the resolved fingerprints."""
        watched = self._watched()
        if not watched:
            return []
        need = timedelta(hours=resolve_after_hours)
        rows = self.db.execute("select * from groups where status = 'active'").fetchall()
        done = []
        with self.db:
            for row in rows:
                last = _dt(row["last_seen"])
                if last is None:
                    continue
                covered = sum(
                    (min(b, now) - max(a, last) for a, b in watched if min(b, now) > max(a, last)),
                    timedelta(),
                )
                if covered < need:
                    continue
                release, commits = find_fix(row)
                self.db.execute(
                    "update groups set status = 'resolved', resolved_at = last_seen, "
                    "resolved_release = ?, fixed_by = ? where fingerprint = ?",
                    (release, json.dumps(commits), row["fingerprint"]),
                )
                done.append(row["fingerprint"])
        return done

    def show(self, prefix: str) -> dict | None:
        """One group's history by its id (a fingerprint prefix), for `logsite seen`."""
        row = self.db.execute(
            "select * from groups where fingerprint like ? order by last_seen desc", (prefix + "%",)
        ).fetchone()
        if row is None:
            return None
        days = self.db.execute(
            "select substr(hour, 1, 10) as day, sum(count) from hours where fingerprint = ? "
            "group by day order by day",
            (row["fingerprint"],),
        ).fetchall()
        out = dict(row)
        out["fixed_by"] = json.loads(row["fixed_by"] or "[]")
        out["previous_fix"] = json.loads(row["previous_fix"] or "null")
        out["daily"] = [(d, n) for d, n in days]
        return out


# --- the releases and commits around a group --------------------------------------------------


def release_lookup(root: Path):
    """``ts -> the git tag released at or before ts`` (None without tags)."""
    from .git import tags_by_date

    tags = tags_by_date(root)

    def at(ts: datetime | None) -> str | None:
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        before = [name for name, when in tags if when <= ts]
        return before[-1] if before else None

    return at


def fix_finder(root: Path):
    """``row -> (release, commits)``: the first release soon after the group was last seen,
    and the commits in it (or in the days before, without tags) that changed the code around
    where it was raised."""
    from .git import line_history, tags_by_date

    tags = tags_by_date(root)

    def find(row) -> tuple[str | None, list[dict]]:
        last = _dt(row["last_seen"])
        if last is None:
            return None, []
        after = [(n, w) for n, w in tags if last - timedelta(minutes=30) <= w <= last + FIX_WINDOW]
        release = after[0][0] if after else None
        where = row["raised_at"] or row["logged_at"]
        if not where or ":" not in where:
            return release, []
        path, _, line = where.rpartition(":")
        if not line.isdigit():
            return release, []
        since = last - timedelta(days=3)
        if release:
            earlier = [w for n, w in tags if w < after[0][1]]
            since = earlier[-1] if earlier else since
        n = int(line)
        commits = line_history(root, path, max(1, n - 5), n + 5, since=since, max_count=3)
        until = after[0][1] if after else last + timedelta(hours=1)
        return release, [
            {"sha": c.sha[:8], "author": c.author, "date": c.date.isoformat(), "summary": c.summary}
            for c in commits
            if c.date <= until
        ]

    return find


def open_history(cfg) -> History | None:
    """The project's history store, or None when it's turned off or can't be opened."""
    hcfg = getattr(cfg, "history", None)
    if hcfg is None or not hcfg.enabled:
        return None
    path = Path(hcfg.path) if hcfg.path else default_path(cfg.root)
    if hcfg.path and not path.is_absolute():
        path = cfg.root / path
    try:
        return History(path)
    except (OSError, sqlite3.Error):
        return None


def apply(
    cfg,
    groups: list[ErrorGroup],
    start: datetime,
    end: datetime,
    now: datetime | None = None,
    record: bool = True,
):
    """Look up, record and resolve in one go, as every command that summarizes errors does:
    each group gets its ``history`` (as known *before* this run), then the run is saved
    (unless ``record`` is false, e.g. for a query narrowed to part of the selector)."""
    store = open_history(cfg)
    if store is None:
        return None
    try:
        known = store.lookup(groups, start)
        for g in groups:
            g.history = known[g.fingerprint]
        if not record:
            return store.path
        store.record(groups, start, end, release_lookup(cfg.root))
        store.resolve(now or end, cfg.history.resolve_after_hours, fix_finder(cfg.root))
        return store.path
    except sqlite3.Error:
        return None
    finally:
        store.close()


def history_json(h: GroupHistory | None) -> dict | None:
    if h is None:
        return None
    return {
        "known": h.known,
        "new_ever": h.new_ever,
        "first_seen": _iso(h.first_seen),
        "first_release": h.first_release,
        "total": h.total,
        "days_seen": h.days_seen,
        "regression": h.regression,
        "resolved_at": _iso(h.resolved_at),
        "resolved_release": h.resolved_release,
        "fixed_by": h.fixed_by,
        "times_regressed_before": h.regressions,
        "watching_since": _iso(h.watching_since),
    }
