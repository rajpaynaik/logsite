"""Release health: what a release did to errors, from the error store.

A release is a value of the ``release`` field the logs carry (``release``, ``service.version``,
``git_sha``...), or a git tag. It went out when its first error was stored, or at the tag's
date. The window after it (up to ``window_hours``, or until now) is compared with one as long
just before: each error group is ``new`` (first seen after the release), ``regressed`` (seen
long ago, quiet in the window before, back now), ``spiking`` or ``resolved`` (gone after it),
and the release gets a verdict: healthy, degraded or bad.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .git import Commit, previous_tag, ref_time, resolve_ref, rev_list, site_history, tags_by_date
from .latency import LatencyConfig, format_seconds, overall
from .models import parse_iso
from .report import _cut, short_class, short_place
from .term import Palette
from .watch import WatchConfig

STATUSES = ("new", "regressed", "spiking", "resolved")
VERDICTS = ("healthy", "degraded", "bad")
_ALL = 100_000  # every group in a window
_CANDIDATES = 200  # commits git log -L looks through before keeping the release's


@dataclass
class ReleaseConfig:
    window_hours: float = 24.0  # how long after a release to look, at most
    bad_rate: float = 3.0  # errors per hour this many times the window before: bad
    bad_count: int = 10  # a new or regressed error this many times: bad
    settle_minutes: float = 30.0  # watch judges a release once it has been out this long

    @classmethod
    def from_dict(cls, data: Mapping) -> ReleaseConfig:
        return cls(
            window_hours=float(data.get("window_hours", cls.window_hours)),
            bad_rate=float(data.get("bad_rate", cls.bad_rate)),
            bad_count=int(data.get("bad_count", cls.bad_count)),
            settle_minutes=float(data.get("settle_minutes", cls.settle_minutes)),
        )


@dataclass(frozen=True)
class Release:
    name: str
    at: datetime  # when it went out
    sha: str | None = None  # its commit, when git knows the name
    base: str | None = None  # the release before it, in git


@dataclass
class Change:
    status: str  # one of STATUSES
    group: dict  # the error_groups row
    before: int
    after: int
    commits: list[Commit] = field(default_factory=list)

    @property
    def place(self) -> str | None:
        return self.group.get("raised_at") or self.group.get("logged_at")


@dataclass
class Health:
    release: Release
    start: datetime  # of the window before
    end: datetime  # of the window after
    errors_before: int
    errors_after: int
    lines_before: int | None  # every log line, when the store counts them
    lines_after: int | None
    groups_before: int
    groups_after: int
    changes: list[Change]
    verdict: str
    commit_count: int | None = None  # commits in the release
    latency_before: dict | None = None  # every timed call, as latency.overall() gives it
    latency_after: dict | None = None
    slower: bool = False  # p95 up [latency] ratio times, over enough calls

    @property
    def hours(self) -> float:
        return max((self.end - self.release.at).total_seconds() / 3600, 1 / 60)

    @property
    def rate_before(self) -> float:
        return self.errors_before / self.hours

    @property
    def rate_after(self) -> float:
        return self.errors_after / self.hours

    @property
    def ratio(self) -> float | None:
        """Errors after / before; None when there were none before."""
        return self.errors_after / self.errors_before if self.errors_before else None

    def count(self, status: str) -> int:
        return sum(1 for c in self.changes if c.status == status)


# --- finding a release --------------------------------------------------------------------


def find_release(store, root: str | Path, ref: str | None = None,
                 at: datetime | None = None) -> Release:  # fmt: skip
    """The release ``ref`` names (the latest one when None): from the store's release
    values first, then git. Raises ValueError with a user-facing message."""
    known = {r["release"]: r for r in store.releases(limit=200)}
    if ref is None:
        if known:
            ref = next(iter(known))  # the latest first
        else:
            tags = tags_by_date(root)
            if not tags:
                raise ValueError("no releases yet: errors carry no release field and git has"
                                 " no tags; name one, e.g. logsite release v1.4.0")  # fmt: skip
            ref = tags[-1][0]
    sha = resolve_ref(root, ref)
    when = at
    if when is None and ref in known:
        when = parse_iso(known[ref]["first_at"])
    if when is None and sha:
        when = ref_time(root, ref)
    if when is None:
        raise ValueError(f"unknown release {ref!r}: no error carries it and git doesn't know it")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    base = previous_tag(root, ref) if sha else None
    return Release(name=ref, at=when.astimezone(timezone.utc), sha=sha, base=base)


def recent_releases(store, root: str | Path, limit: int = 10) -> list[Release]:
    """The latest releases, newest first: the store's release values, else git tags."""
    rows = store.releases(limit=limit)
    if rows:
        out = []
        for r in rows:
            sha = resolve_ref(root, r["release"])
            base = previous_tag(root, r["release"]) if sha else None
            out.append(Release(r["release"], parse_iso(r["first_at"]).astimezone(timezone.utc),
                               sha, base))  # fmt: skip
        return out
    tags = tags_by_date(root)[-limit:][::-1]
    return [Release(name, when.astimezone(timezone.utc), resolve_ref(root, name),
                    previous_tag(root, name)) for name, when in tags]  # fmt: skip


# --- comparing ----------------------------------------------------------------------------


def health(store, release: Release, *, now: datetime, cfg: ReleaseConfig, spikes: WatchConfig,
           root: str | Path | None = None, commits: int = 1,
           latency: LatencyConfig | None = None) -> Health:  # fmt: skip
    """The release's errors against the window before it. With ``root`` and ``commits``,
    new and regressed errors get the release's commits that touched their code."""
    end = min(now, release.at + timedelta(hours=cfg.window_hours))
    if end <= release.at:
        raise ValueError(f"{release.name} went out at {release.at:%Y-%m-%d %H:%M} UTC,"
                         " after now")  # fmt: skip
    start = release.at - (end - release.at)
    after = {g["id"]: g for g in store.groups(since=release.at, until=end, limit=_ALL)}
    # the store's ranges include both ends: the release's own moment counts after it only
    just_before = release.at - timedelta(microseconds=1)
    before = {g["id"]: g for g in store.groups(since=start, until=just_before, limit=_ALL)}

    changes = []
    for gid, g in after.items():
        n, prev = int(g["recent"]), int(before[gid]["recent"]) if gid in before else 0
        first = parse_iso(g["first_seen"]) if g.get("first_seen") else None
        if first is not None and first >= release.at:
            changes.append(Change("new", g, 0, n))
        elif prev == 0:
            changes.append(Change("regressed", g, 0, n))
        elif n >= spikes.spike_min_count and n >= spikes.spike_ratio * prev:
            changes.append(Change("spiking", g, prev, n))
    changes += [Change("resolved", g, int(g["recent"]), 0)
                for gid, g in before.items() if gid not in after]  # fmt: skip
    order = {s: i for i, s in enumerate(STATUSES)}
    changes.sort(key=lambda c: (order[c.status], -max(c.after, c.before)))

    commit_count = None
    if root is not None and release.sha:
        span = (f"{release.base}..{release.sha}",) if release.base else (release.sha,)
        shas = set(rev_list(root, *span)) if release.base else None
        commit_count = len(shas) if shas is not None else None
        if commits and shas:
            for c in changes:
                if c.status in ("new", "regressed") and c.place:
                    c.commits = _suspects(root, c.place, release, shas, commits)

    out = Health(
        release=release,
        start=start,
        end=end,
        errors_before=sum(int(g["recent"]) for g in before.values()),
        errors_after=sum(int(g["recent"]) for g in after.values()),
        lines_before=store.lines(start, release.at),
        lines_after=store.lines(release.at, end),
        groups_before=len(before),
        groups_after=len(after),
        changes=changes,
        verdict="healthy",
        commit_count=commit_count,
        latency_before=overall(store.latency(start, release.at)),
        latency_after=overall(store.latency(release.at, end)),
    )
    lc = latency or LatencyConfig()
    b, a = out.latency_before, out.latency_after
    out.slower = bool(b and a and min(b["n"], a["n"]) >= lc.min_calls and b["p95"]
                      and a["p95"] >= lc.ratio * b["p95"])  # fmt: skip
    out.verdict = verdict(out, cfg, spikes)
    return out


def verdict(h: Health, cfg: ReleaseConfig, spikes: WatchConfig) -> str:
    """``bad``: errors up ``bad_rate`` times (and at least ``[watch] spike_min_count`` of
    them), or a new or regressed error ``bad_count`` times; ``degraded``: any new, regressed
    or spiking error, or p95 latency up ``[latency] ratio`` times; else ``healthy``."""
    worse = [c for c in h.changes if c.status in ("new", "regressed")]
    jump = h.errors_after >= spikes.spike_min_count and h.errors_after >= cfg.bad_rate * max(
        h.errors_before, 1
    )
    if jump or any(c.after >= cfg.bad_count for c in worse):
        return "bad"
    if worse or h.slower or any(c.status == "spiking" for c in h.changes):
        return "degraded"
    return "healthy"


def _suspects(root, place: str, release: Release, shas: set[str], n: int) -> list[Commit]:
    path, _, line = place.rpartition(":")
    if not path or not line.isdigit():
        return []
    candidates = site_history(root, path, int(line), max_count=_CANDIDATES,
                              revs=f"{release.base}..{release.sha}")  # fmt: skip
    return [c for c in candidates if c.sha in shas][:n]


# --- output -------------------------------------------------------------------------------


def to_json(h: Health) -> dict:
    return {
        "release": h.release.name,
        "deployed_at": h.release.at.isoformat(),
        "sha": h.release.sha,
        "previous": h.release.base,
        "commits": h.commit_count,
        "verdict": h.verdict,
        "window": {
            "before": h.start.isoformat(),
            "deployed": h.release.at.isoformat(),
            "after": h.end.isoformat(),
            "hours": round(h.hours, 2),
        },  # fmt: skip
        "errors": {
            "before": h.errors_before,
            "after": h.errors_after,
            "per_hour_before": round(h.rate_before, 2),
            "per_hour_after": round(h.rate_after, 2),
            "ratio": round(h.ratio, 2) if h.ratio is not None else None,
        },  # fmt: skip
        "lines": {"before": h.lines_before, "after": h.lines_after},
        "latency": {"before": h.latency_before, "after": h.latency_after, "slower": h.slower},
        "groups": {
            "before": h.groups_before,
            "after": h.groups_after,
            **{s: h.count(s) for s in STATUSES},
        },  # fmt: skip
        "changes": [
            {
                "status": c.status,
                "id": c.group["id"],
                "title": c.group.get("title"),
                "raised_at": c.group.get("raised_at"),
                "logged_at": c.group.get("logged_at"),
                "before": c.before,
                "after": c.after,
                "commits": [
                    {"sha": x.sha, "summary": x.summary, "author": x.author} for x in c.commits
                ],
            }  # fmt: skip
            for c in h.changes
        ],
    }


def _verdict_text(v: str, pal: Palette) -> str:
    return {"bad": pal.bold_red, "degraded": pal.bold_yellow, "healthy": pal.bold_green}[v](
        v.upper()
    )


def _rate(per_hour: float) -> str:
    return f"{per_hour:.1f}" if per_hour < 10 else f"{per_hour:,.0f}"


def _share(errors: int, lines: int | None) -> str:
    return f"{errors / lines:.1%}" if lines else "—"


def _times(after: float, before: float) -> str:
    if not before:
        return "new" if after else "—"
    return f"×{after / before:.1f}"


def render(h: Health, pal: Palette, width: int, limit: int = 20) -> list[str]:
    r = h.release
    n = h.commit_count
    since = f" · {n} commit{'s' * (n != 1)} since {r.base}" if r.base and n else ""
    sha = f" · {r.sha[:8]}" if r.sha else ""
    lines = [
        f"{pal.bold('Release ' + r.name)} · {_verdict_text(h.verdict, pal)}",
        pal.gray(f"deployed {r.at:%Y-%m-%d %H:%M} UTC{sha}{since}"),
        pal.gray(f"{h.hours:.0f}h after it, against {h.hours:.0f}h before")
        if h.hours >= 1 else pal.gray(f"{h.hours * 60:.0f} min after it, against as long before"),
        "",
    ]  # fmt: skip
    groups = ", ".join(f"{h.count(s)} {w}" for s, w in
                       (("new", "new"), ("regressed", "back"), ("resolved", "fixed"))
                       if h.count(s))  # fmt: skip
    summary = [
        ("", "BEFORE", "AFTER", "CHANGE"),
        ("Errors/hour", _rate(h.rate_before), _rate(h.rate_after),
         _times(h.rate_after, h.rate_before)),
        ("Error rate", _share(h.errors_before, h.lines_before),
         _share(h.errors_after, h.lines_after),
         _times(h.errors_after / h.lines_after, h.errors_before / h.lines_before)
         if h.lines_before and h.lines_after else "—"),
        ("Groups", str(h.groups_before), str(h.groups_after), groups or "no change"),
    ]  # fmt: skip
    b, a = h.latency_before, h.latency_after
    if b or a:
        summary.append(("p95 latency", format_seconds(b and b["p95"]),
                        format_seconds(a and a["p95"]),
                        _times(a["p95"], b["p95"]) if a and b else "—"))  # fmt: skip
    widths = [max(len(row[c]) for row in summary) for c in range(3)]
    for i, row in enumerate(summary):
        text = (row[0].ljust(widths[0]) + "   " + row[1].rjust(max(widths[1], 6)) + "   "
                + row[2].rjust(max(widths[2], 6)) + "   " + row[3])  # fmt: skip
        lines.append(pal.bold(pal.gray(text)) if i == 0 else text)

    shown = h.changes[:limit]
    if not shown:
        lines += ["", pal.gray("No error changed.")]
        return lines
    head = ("#", "STATUS", "COUNT", "ERROR", "RAISED AT")
    rows = [(str(i), c.status, f"{c.before:,} → {c.after:,}", short_class(c.group.get("title")
             or ""), short_place(c.place)) for i, c in enumerate(shown, 1)]  # fmt: skip
    w = [max(len(head[c]), *(len(row[c]) for row in rows)) for c in range(len(head))]
    fixed = sum(w) - w[3] + 2 * (len(head) - 1) + 2
    w[3] = max(min(w[3], width - fixed), 20)
    paint = {"new": pal.bold_red, "regressed": pal.bold_magenta, "spiking": pal.bold_yellow,
             "resolved": pal.green}  # fmt: skip

    def cells(row) -> list[str]:
        return [(_cut(t, n).rjust(n) if c == 0 else _cut(t, n).ljust(n))
                for c, (t, n) in enumerate(zip(row, w, strict=True))]  # fmt: skip

    lines += ["", "  " + pal.bold(pal.gray("  ".join(cells(head)).rstrip())),
              "  " + pal.gray("─" * (sum(w) + 2 * (len(head) - 1)))]  # fmt: skip
    for c, row in zip(shown, rows, strict=True):
        n, status, count, error, raised = cells(row)
        lines.append("  " + "  ".join([
            pal.gray(n), paint[c.status](status), pal.bold_white(count), pal.bold_white(error),
            pal.gray(raised) if raised.strip() == "—" else pal.bold_cyan(raised),
        ]).rstrip())  # fmt: skip
    if len(h.changes) > limit:
        lines.append(pal.gray(f"  {len(h.changes) - limit} more; raise --top to see them"))

    suspects = [(i, x) for i, c in enumerate(shown, 1) for x in c.commits]
    if suspects:
        lines += ["", pal.bold("Suspect commits")]
        room = max(width - 2 - len(str(len(shown))) - 2 - 8 - 2 - 2 - 24, 20)
        room = min(room, max(len(x.summary) for _, x in suspects))
        for i, x in suspects:
            lines.append(f"  {pal.gray(str(i).rjust(len(str(len(shown)))))}  "
                         f"{pal.yellow(x.short_sha)}  {_cut(x.summary, room).ljust(room)}  "
                         f"{pal.gray(_cut(x.author, 22))}".rstrip())  # fmt: skip
    return lines


def render_list(healths: list[Health], pal: Palette) -> list[str]:
    head = ("RELEASE", "DEPLOYED", "ERRORS/H", "NEW", "BACK", "FIXED", "VERDICT")
    rows = [(h.release.name, f"{h.release.at:%b %d %H:%M}",
             f"{_rate(h.rate_before)} → {_rate(h.rate_after)}", str(h.count("new")),
             str(h.count("regressed")), str(h.count("resolved")), h.verdict.upper())
            for h in healths]  # fmt: skip
    w = [max(len(head[c]), *(len(r[c]) for r in rows)) for c in range(len(head))]
    w[0] = min(w[0], 24)
    right = {2, 3, 4, 5}

    def cells(row) -> list[str]:
        return [(_cut(t, n).rjust(n) if c in right else _cut(t, n).ljust(n))
                for c, (t, n) in enumerate(zip(row, w, strict=True))]  # fmt: skip

    lines = ["  " + pal.bold(pal.gray("  ".join(cells(head)).rstrip())),
             "  " + pal.gray("─" * (sum(w) + 2 * (len(head) - 1)))]  # fmt: skip
    for h, row in zip(healths, rows, strict=True):
        rest = cells(row)[:-1]
        lines.append("  " + "  ".join([pal.bold_white(rest[0]), pal.gray(rest[1]), *rest[2:],
                                       _verdict_text(h.verdict, pal)]).rstrip())  # fmt: skip
    return lines


# --- watch: one message per release that got worse ----------------------------------------


def to_alert(store, cfg, now: datetime) -> tuple[Health, str, str] | None:
    """The latest release, if it has settled, is still in its window and has a verdict worse
    than any already sent for it: ``(health, plain text, Slack text)``."""
    rows = store.releases(limit=1)
    if not rows:
        return None
    rel = find_release(store, cfg.root, rows[0]["release"])
    age = now - rel.at
    if age < timedelta(minutes=cfg.release.settle_minutes) or age > timedelta(
        hours=cfg.release.window_hours
    ):
        return None
    h = health(
        store, rel, now=now, cfg=cfg.release, spikes=cfg.watch, root=cfg.root, latency=cfg.latency
    )
    if h.verdict == "healthy":
        return None
    key = alert_key(rel)
    sent = [v for v in VERDICTS[VERDICTS.index(h.verdict) :] if store.last_alert(key, v)]
    if sent:  # this verdict, or a worse one, already went out
        return None
    return h, alert_text(h, cfg, slack=False), alert_text(h, cfg, slack=True)


def alert_key(rel: Release) -> str:
    return f"release:{rel.name}"[:64]


def alert_text(h: Health, cfg, *, slack: bool, top: int = 5) -> str:
    project = cfg.timeline.service or cfg.root.name
    icon = {"bad": ":red_circle: ", "degraded": ":large_yellow_circle: "}.get(h.verdict, "")
    name = f"*{h.release.name}*" if slack else h.release.name
    head = f"{icon if slack else ''}{project}: release {name} looks {h.verdict}"
    table = render(h, Palette(False), 72, limit=top)[3:]  # the head is in the first line
    body = "\n".join(table).strip("\n")
    more = f"\n`logsite release {h.release.name}` for the rest" if len(h.changes) > top else ""
    return f"{head}\n```\n{body}\n```{more}" if slack else f"{head}\n\n{body}{more}"
