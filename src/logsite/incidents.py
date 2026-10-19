"""Incidents: error groups that belong together, with the deploy and commits around them.

The error summary lists groups; during an outage several of them are one story: a timeout
(the cause), the records that then failed and the lag that built up (the effects). Groups
are linked when the same requests ran into both (a shared trace id) or when they started
within a few minutes of each other and at least one of them is new or spiking. Each
incident names its cause (the earliest group with an exception), its effects in the order
they started, the deploy just before it and the commits that touched the code that raised.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .errors import ErrorGroup, normalize
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
    diagnosis: object | None = None  # diagnose.Diagnosis, once it's been explained
    related: list[Related] = field(default_factory=list)  # warnings etc. in the same requests
    orphans: list[dict] = field(default_factory=list)  # work that went on after the caller quit
    leadership: list[dict] = field(default_factory=list)  # leader claims around the start

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


@dataclass
class Related:
    """A line that isn't an error but was logged in the same requests: a pool exhausted
    warning, a retry, the request that ended in a 500."""

    level: str  # warning | info
    message: str
    trace_id: str
    count: int = 1  # how many of the requests looked at had it


RELATED_TRACES = 3  # requests to look at
_LEVEL_BRACKETS = re.compile(
    r"^(?:(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING)\s+)?(?:\[[^\]]*\]\s*)*", re.I
)
_WARN = re.compile(r"\b(WARN|WARNING)\b")
_IMPACT = re.compile(r"\bstatus[=:]\s*\"?5\d\d\b|\bduration(_ms)?[=:]|\btook\b|\blatency", re.I)


def _clean(record) -> str:
    from .errors import _LOGGER_PREFIX, _PLAIN_PREFIX

    first = _PLAIN_PREFIX.sub("", record.message.split("\n", 1)[0])
    first = _LEVEL_BRACKETS.sub("", first)  # "WARN  [svc,traceId=..] [thread] "
    return _LOGGER_PREFIX.sub("", first).strip()[:300]


def add_related(inc: Incident, backend, window, trace_field: str = "trace_id") -> None:
    """The warnings (and the request-completion lines) logged in a few of the incident's
    requests: evidence the error lines themselves don't carry."""
    from .tracebacks import is_error, level_of

    traces: list[str] = []
    for g in inc.groups:
        ids = g.sample.get("ids") or {}
        for key in (trace_field, "trace_id"):
            if ids.get(key) and ids[key] not in traces:
                traces.append(ids[key])
        traces += sorted(t for t in g.trace_ids if t not in traces)[:RELATED_TRACES]
    found: dict[str, Related] = {}
    orphans: dict[str, dict] = {}
    looked = 0
    for tid in traces[:RELATED_TRACES]:
        try:
            records = backend.find_records(trace_field, tid, window, limit=200)
        except Exception:
            continue
        looked += 1
        _orphaned(records, tid, orphans)
        seen_here = set()
        for r in records:
            if is_error(r):
                continue  # already in the error groups
            first = r.message.split("\n", 1)[0]
            level = (level_of(r) or "").lower()
            warn = level in ("warn", "warning") or (not level and _WARN.search(first[:120]))
            if not (warn or _IMPACT.search(first)):
                continue
            text = _clean(r)
            key = normalize(text)
            if key in seen_here:
                continue
            seen_here.add(key)
            if key in found:
                found[key].count += 1
            else:
                found[key] = Related("warning" if warn else "info", text, tid)
    inc.related = sorted(found.values(), key=lambda x: (x.level != "warning", -x.count))[:6]
    for o in orphans.values():
        o["looked"] = looked
    inc.orphans = sorted(orphans.values(), key=lambda o: -o["seconds"])


GAVE_UP = re.compile(
    r"timed? ?out|timeout|deadline exceeded|context canceled|cancelled|canceled|"
    r"ReadTimeout|SocketTimeoutException|client disconnected|broken pipe",
    re.I,
)
ORPHAN_GAP = 1.0  # seconds after the caller gave up


def _orphaned(records, tid: str, out: dict) -> None:
    """Lines one service logged for a request after the service that called it had already
    given up on it (timed out, cancelled): the work went on with nobody waiting."""
    from .errors import _service
    from .tracebacks import is_error

    timed = [(r.timestamp, _service(r), r) for r in records if r.timestamp is not None]

    def text(r) -> str:  # the message and the fields (exc_info, error, ...)
        values = " ".join(str(v)[:1000] for v in r.fields.values() if isinstance(v, str))
        return f"{r.message[:2000]} {values}"

    gave_up = [(t, s) for t, s, r in timed if s and is_error(r) and GAVE_UP.search(text(r))]
    if not gave_up:
        return
    t0, caller = min(gave_up, key=lambda x: x[0])
    for t, s, r in timed:
        if not s or s == caller:
            continue
        if is_error(r) and GAVE_UP.search(text(r)):
            continue  # giving up too (waiting on someone), not working
        late = (t - t0).total_seconds()
        if late <= ORPHAN_GAP:
            continue
        o = out.setdefault(s, {"service": s, "caller": caller, "seconds": 0.0, "traces": set(),
                               "example": ""})  # fmt: skip
        o["traces"].add(tid)
        if late > o["seconds"]:
            o["seconds"], o["example"] = late, _clean(r)[:200]


LEADER_CLAIM = re.compile(
    r"(became|elected( as)?|is now( the)?|acquired( the)?|promoted to|won( the)? election.*?)"
    r"\s*(the )?(leader|primary|master)\b|leadership acquired|acquired (the )?lease",
    re.I,
)
STALE = re.compile(
    r"stale (term|epoch|leader|fencing)|fencing token|NotLeader|not the leader|"
    r"not (the )?primary|READONLY You can't write|term mismatch|epoch mismatch|"
    r"lost (the )?lease|split.?brain|conflicting write|write conflict",
    re.I,
)
_NODE = re.compile(r"\b(?:node|member|instance|host|replica|broker)[=: _-]*([\w.-]+)", re.I)


def add_leadership(inc: Incident, backend, window) -> None:
    """When the errors are about a stale leader or fencing, the leadership claims logged
    around the start: two nodes claiming at once is a split brain."""
    text = " ".join(" ".join([g.title, *g.causes]) for g in inc.groups)
    if not STALE.search(text) or inc.started is None:
        return
    from .errors import _service

    claims = []
    for word in ("leader", "primary", "lease"):
        try:
            records = backend.query_logs(window, contains=word, limit=500)
        except Exception:
            continue
        for r in records:
            first = r.message.split("\n", 1)[0]
            if not LEADER_CLAIM.search(first) or r.timestamp is None:
                continue
            m = _NODE.search(first)
            node = (m.group(1) if m else None) or r.fields.get("node") or _service(r)
            claims.append({"at": r.timestamp, "node": str(node), "message": _clean(r)[:200]})
    seen = set()
    for c in sorted(claims, key=lambda c: c["at"]):
        key = (c["at"], c["node"])
        if key not in seen:
            seen.add(key)
            inc.leadership.append(c)


class _Links:
    """Union-find over groups, remembering why each pair was joined."""

    def __init__(self, groups: list[ErrorGroup]) -> None:
        self.parent = {g.fingerprint: g.fingerprint for g in groups}
        self.starts = {g.fingerprint: [g.first_seen] if g.first_seen else [] for g in groups}
        self.why: dict[str, str] = {}

    def find(self, fp: str) -> str:
        while self.parent[fp] != fp:
            self.parent[fp] = self.parent[self.parent[fp]]
            fp = self.parent[fp]
        return fp

    def span_with(self, a: ErrorGroup, b: ErrorGroup) -> float:
        """Minutes between the earliest and latest start if a's and b's incidents merged."""
        starts = [t for g in (a, b) for t in self.starts[self.find(g.fingerprint)]]
        return (max(starts) - min(starts)).total_seconds() / 60 if starts else 0.0

    def join(self, a: ErrorGroup, b: ErrorGroup, why: str) -> None:
        ra, rb = self.find(a.fingerprint), self.find(b.fingerprint)
        if ra != rb:
            self.parent[rb] = ra
            self.starts[ra] = self.starts[ra] + self.starts.pop(rb)
        self.why.setdefault(a.fingerprint, why)
        self.why.setdefault(b.fingerprint, why)


def _active(g: ErrorGroup, cfg: LintConfig) -> bool:
    """New, or spiking: something that changed, as opposed to background errors."""
    return g.is_new or (g.baseline is not None and is_spike(g.count, g.baseline, cfg))


def _stems(g: ErrorGroup) -> set[str]:
    """``inventory-service`` -> {"inventory-service", "inventory"}: how others name it."""
    out = set()
    for s in g.services:
        s = s.lower()
        out.add(s)
        stem = re.sub(r"[-_.](service|svc|api|app|server|worker|consumer)$", "", s)
        if len(stem) >= 4:
            out.add(stem)
    return out


def _calls(a: ErrorGroup, b: ErrorGroup) -> bool:
    """Whether ``a``'s error is about calling ``b``'s service (it names it)."""
    if not a.services or not b.services or a.services.keys() & b.services.keys():
        return False
    text = " ".join([a.title, *a.causes, a.sample.get("message") or "",
                     a.sample.get("traceback") or ""]).lower()  # fmt: skip
    return any(re.search(rf"\b{re.escape(stem)}", text) for stem in _stems(b))


def _cause_first(groups: list[ErrorGroup], cfg: LintConfig) -> list[ErrorGroup]:
    """The cause: among the groups that changed (new or spiking; a steady error was there
    before the incident), the earliest with an exception, since a stack trace explains more
    than a line of text; else the earliest. The rest follow in the order they started."""

    def start(g: ErrorGroup):
        return (g.first_seen is None, g.first_seen or datetime.max, -g.count)

    changed = [g for g in groups if _active(g, cfg)] or groups
    # a caller's error names what it called ("inventory call failed"): the callee is
    # upstream, even though its own error is often logged later (the caller timed out first)
    upstream = [g for g in changed if not any(_calls(g, o) for o in groups if o is not g)]
    changed = upstream or changed
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
            # no chains: A and B 3 min apart, B and C 6 min apart don't make A and C one
            # incident; the whole incident has to start within link_minutes
            spread = links.span_with(a, b)
            if gap <= link_minutes and same_service and spread <= link_minutes:
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
        "diagnosis": _diagnosis_json(inc),
        "related": [
            {"level": r.level, "message": r.message, "trace_id": r.trace_id, "count": r.count}
            for r in inc.related
        ],
        **_shape(inc),
    }


def _shape(inc: Incident) -> dict:
    from .diagnose import impact, shared_root

    shared = shared_root(inc)
    return {
        "shared_root_cause": {"cause": shared[0], "entry_points": shared[1]} if shared else None,
        "impact": impact(inc),
    }


def _diagnosis_json(inc: Incident) -> dict | None:
    from .diagnose import diagnosis_json

    return diagnosis_json(inc.diagnosis) if inc.diagnosis is not None else None
