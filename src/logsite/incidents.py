"""Incidents: error groups that belong together, with the deploy and commits around them.

The error summary lists groups; during an outage several of them are one story: a timeout
(the cause), the records that then failed and the lag that built up (the effects). Groups
are linked when the same requests ran into both (a shared trace id) or when they started
within a few minutes of each other and at least one of them is new or spiking. Each
incident names its cause (the earliest group with an exception), its effects in the order
they started, the deploy just before it and the commits that touched the code that raised.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .errors import ErrorGroup
from .git import Commit, line_history, tags_by_date
from .lint import LintConfig, is_spike

LINK_MINUTES = 10.0  # groups starting this close together are one incident
DEPLOY_LOOKBACK = timedelta(hours=24)  # a deploy this long before the start can be the cause
SUSPECT_LOOKBACK = timedelta(days=7)  # commits this long before the start, when there's no tag
SUSPECT_LINES = 5  # lines on each side of where it was raised


@dataclass
class Incident:
    groups: list[ErrorGroup]  # the cause first, then the effects in the order they started
    reasons: dict[str, str] = field(default_factory=dict)  # group id -> why it's linked
    deploy: tuple[str, datetime] | None = None  # the release just before it started
    suspects: list[Commit] = field(default_factory=list)

    @property
    def cause(self) -> ErrorGroup:
        return self.groups[0]

    @property
    def effects(self) -> list[ErrorGroup]:
        return self.groups[1:]

    @property
    def started(self) -> datetime | None:
        times = [g.first_seen for g in self.groups if g.first_seen]
        return min(times) if times else None

    @property
    def last_seen(self) -> datetime | None:
        times = [g.last_seen for g in self.groups if g.last_seen]
        return max(times) if times else None

    @property
    def events(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def is_new(self) -> bool:
        return any(g.is_new for g in self.groups)


class _Links:
    """Union-find over groups, remembering why each pair was joined."""

    def __init__(self, groups: list[ErrorGroup]) -> None:
        self.parent = {g.fingerprint: g.fingerprint for g in groups}
        self.why: dict[str, str] = {}

    def find(self, fp: str) -> str:
        while self.parent[fp] != fp:
            self.parent[fp] = self.parent[self.parent[fp]]
            fp = self.parent[fp]
        return fp

    def join(self, a: ErrorGroup, b: ErrorGroup, why: str) -> None:
        ra, rb = self.find(a.fingerprint), self.find(b.fingerprint)
        if ra != rb:
            self.parent[rb] = ra
        self.why.setdefault(a.fingerprint, why)
        self.why.setdefault(b.fingerprint, why)


def _active(g: ErrorGroup, cfg: LintConfig) -> bool:
    """New, or spiking: something that changed, as opposed to background errors."""
    return g.is_new or (g.previous is not None and is_spike(g.count, g.previous, cfg))


def _cause_first(groups: list[ErrorGroup], cfg: LintConfig) -> list[ErrorGroup]:
    """The cause: among the groups that changed (new or spiking; a steady error was there
    before the incident), the earliest with an exception, since a stack trace explains more
    than a line of text; else the earliest. The rest follow in the order they started."""

    def start(g: ErrorGroup):
        return (g.first_seen is None, g.first_seen or datetime.max, -g.count)

    changed = [g for g in groups if _active(g, cfg)] or groups
    pool = [g for g in changed if g.exc_type] or changed
    cause = min(pool, key=lambda g: (start(g), g.raised_at is None))
    return [cause, *sorted((g for g in groups if g is not cause), key=start)]


def _reason(cause: ErrorGroup, g: ErrorGroup, links: _Links) -> str:
    """Why ``g`` belongs with the cause, in relation to the cause itself."""
    shared = cause.trace_ids & g.trace_ids
    if shared:
        return f"same requests ({len(shared):,} shared trace id(s))"
    if cause.first_seen and g.first_seen:
        gap = abs((g.first_seen - cause.first_seen).total_seconds()) / 60
        return "started together" if gap < 1 else f"started {gap:.0f} min apart"
    return links.why.get(g.fingerprint, "")


def find_incidents(
    groups: Iterable[ErrorGroup], cfg: LintConfig, link_minutes: float = LINK_MINUTES
) -> tuple[list[Incident], list[ErrorGroup]]:
    """``(incidents, ongoing)``: linked groups with something new or spiking in them, and
    the remaining steady groups."""
    groups = list(groups)
    links = _Links(groups)
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            shared = a.trace_ids & b.trace_ids
            if shared:
                links.join(a, b, f"same requests ({len(shared):,} shared trace id(s))")
                continue
            # by time only when both changed: a steady background error isn't part of it
            if not (a.first_seen and b.first_seen) or not (_active(a, cfg) and _active(b, cfg)):
                continue
            gap = abs((a.first_seen - b.first_seen).total_seconds()) / 60
            same_service = (
                not a.services or not b.services or bool(a.services.keys() & b.services.keys())
            )
            if gap <= link_minutes and same_service:
                links.join(a, b, "started together" if gap < 1 else f"started {gap:.0f} min apart")
    components: dict[str, list[ErrorGroup]] = {}
    for g in groups:
        components.setdefault(links.find(g.fingerprint), []).append(g)
    incidents, ongoing = [], []
    for members in components.values():
        if not any(_active(g, cfg) for g in members):
            ongoing.extend(members)
            continue
        ordered = _cause_first(members, cfg)
        reasons = {g.id: _reason(ordered[0], g, links) for g in ordered[1:]}
        incidents.append(Incident(ordered, reasons))
    incidents.sort(key=lambda inc: (not inc.is_new, -inc.events))
    return incidents, ongoing


def add_context(incident: Incident, root: Path, now: datetime) -> None:
    """The deploy just before the incident started, and the commits around the line that
    raised its cause (in that release when there is one, else in the week before)."""
    start = incident.started
    if start is None:
        return
    tags = [(t, when) for t, when in tags_by_date(root) if when <= start]
    if tags and start - tags[-1][1] <= DEPLOY_LOOKBACK:
        incident.deploy = tags[-1]
    where = incident.cause.raised_at or (
        incident.cause.logged_at.id if incident.cause.logged_at else None
    )
    if not where:
        return
    path, _, line = where.rpartition(":")
    if not line.isdigit():
        return
    since = start - SUSPECT_LOOKBACK
    if incident.deploy:
        earlier = [when for _, when in tags if when < incident.deploy[1]]
        since = earlier[-1] if earlier else since
    n = int(line)
    commits = line_history(
        root, path, max(1, n - SUSPECT_LINES), n + SUSPECT_LINES, since=since, max_count=3
    )
    incident.suspects = [c for c in commits if c.date <= max(start, now)]


def incident_json(inc: Incident) -> dict:
    from .errors import group_json

    return {
        "started": inc.started.isoformat() if inc.started else None,
        "last_seen": inc.last_seen.isoformat() if inc.last_seen else None,
        "events": inc.events,
        "new": inc.is_new,
        "cause": group_json(inc.cause),
        "effects": [{**group_json(g), "linked_by": inc.reasons.get(g.id)} for g in inc.effects],
        "deploy": {"ref": inc.deploy[0], "at": inc.deploy[1].isoformat()} if inc.deploy else None,
        "suspect_commits": [
            {"sha": c.sha, "author": c.author, "date": c.date.isoformat(), "summary": c.summary}
            for c in inc.suspects
        ],
    }
