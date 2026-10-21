"""Error summary: production errors grouped by cause, so thousands of tracebacks become a
short list a person can read.

Records with the same exception types and the same frames in the project's own code are one
group: line numbers, library frames and the values in the message (ids, amounts, timeouts)
don't split it. Error records without a traceback group by the log call that wrote them, or
by their message with the values masked.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .backends import LogBackend, TimeRange
from .context import get_field
from .deploys import resolve_deploy
from .index import DEFAULT_EXCLUDES, iter_source_files
from .leaks import LeakConfig, redact
from .lint import LintConfig, describe_trend, is_spike
from .matching import Matcher
from .models import LogRecord, LogSite, parse_iso
from .timeline import SERVICE_FIELDS
from .tracebacks import ErrorInfo, Frame, extract_error, is_trace_piece, level_of

ID_FIELDS = ("trace_id", "request_id", "span_id")
MAX_TRACE_IDS = 500  # per group, to link groups that the same requests ran into
# Exceptions that only say "something inside failed": the headline is the first real one.
WRAPPERS = {
    "ListenerExecutionFailedException", "UndeclaredThrowableException",
    "InvocationTargetException", "ExecutionException", "CompletionException",
    "UncheckedExecutionException", "NestedServletException", "ExceptionInInitializerError",
    "UncheckedIOException",
}  # fmt: skip
_JDK_INTERNALS = ("sun.", "jdk.", "com.sun.")
# "2026-10-14 10:00:05,123 ERROR " at the start of a plain-text line: noise in a title.
_PLAIN_PREFIX = re.compile(
    r"^\[?\d{4}-\d{2}-\d{2}[ T][\d:.,]+\S*\]?\s+(?:\[?(?:ERROR|CRITICAL|FATAL|EXCEPTION)\]?:?\s+)?"
)
# What Logback / Spring Boot put before the message: "[app,trace,span] [thread] 1234 ---
# com.acme.Logger : " (or " - ").
_LOGGER_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*|\d+\s+---\s+)*(?:[\w$]+\.)+[\w$]+\s+[:-]\s+")
# trace / request ids written into plain text: "traceId=8fa23bc1", "request_id: abc"
_TEXT_ID = re.compile(
    r"\b(trace_?id|span_?id|request_?id|correlation_?id)[=:]\s*([\w-]{6,})", re.IGNORECASE
)
# Spring Sleuth's positional "[app,traceId,spanId]" ("[inventory-service,7f3a0000c2,a1]")
_SLEUTH = re.compile(r"\[[A-Za-z][\w.-]*,(?P<trace>[0-9a-f]{8,32}),(?P<span>[0-9a-f]{1,32})[,\]]")
TRACEBACK_CHARS = 4000  # of the sample traceback kept for display

_VALUES = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<id>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+(?:\.\d+)*"), "<n>"),  # also "3.5" in "3.5s", "1" in "sda1"
]


def normalize(message: str) -> str:
    """``"timeout after 3.5s for order 8812"`` -> ``"timeout after <n>s for order <n>"``."""
    for rx, placeholder in _VALUES:
        message = rx.sub(placeholder, message)
    return message


TRACE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".kt", ".scala", ".groovy", ".rb",
                  ".cs", ".rs", ".php")  # fmt: skip


class AppFiles:
    """The project's Python files, to tell its own frames from library ones and to turn a
    deployed path (``/srv/app/checkout.py``) into a project path (``app/checkout.py``)."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._by_name: dict[str, list[str]] = {}
        for p in paths:
            self._by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)

    @classmethod
    def scan(
        cls, root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES, tests: bool = False
    ) -> AppFiles:
        from .lang import EXTENSIONS

        root = root.resolve()
        # stack frames come from any language, not only the ones whose log calls are indexed
        suffixes = (*EXTENSIONS, *TRACE_SUFFIXES)
        files = iter_source_files(root, excludes, suffixes=suffixes, tests=tests)
        return cls(f.relative_to(root).as_posix() for f in files)

    def resolve(self, frame: Frame) -> str | None:
        """The project path of a frame, or None if it's not the project's code."""
        if frame.is_library:
            return None
        path = frame.file.replace("\\", "/")
        candidates = self._by_name.get(path.rsplit("/", 1)[-1], [])
        best, best_len = None, 0
        for c in candidates:
            if path == c or path.endswith("/" + c):
                if len(c) > best_len:
                    best, best_len = c, len(c)
        if best is None and len(candidates) == 1:
            best = candidates[0]  # same file name, deployed under another layout
        return best


@dataclass
class ErrorGroup:
    fingerprint: str
    title: str  # the exception (or message) as people will recognise it
    exc_type: str | None
    causes: list[str]  # earlier exceptions in the chain, root cause first
    count: int = 0
    previous: int | None = None  # in the window before; None if not compared
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    raised_at: str | None = None  # "app/inventory.py:88": deepest project frame of the cause
    raised_in: str | None = None  # function name there
    origin: str | None = None  # the library the cause was raised in, e.g. "httpx"
    logged_at: LogSite | None = None  # the log call that logged it most often
    path: list[str] = field(default_factory=list)  # project functions, outermost first
    frames_at: list[str] = field(default_factory=list)  # "path:line" of those functions
    services: Counter[str] = field(default_factory=Counter)
    levels: Counter[str] = field(default_factory=Counter)
    sample: dict = field(default_factory=dict)
    wrapped_in: list[str] = field(default_factory=list)  # framework wrappers around it
    trace_ids: set[str] = field(default_factory=set, repr=False)  # up to MAX_TRACE_IDS
    hourly: Counter[str] = field(default_factory=Counter, repr=False)  # "2026-10-17T19" -> n
    history: object | None = None  # history.GroupHistory, when a history store is in use
    traffic_ratio: float | None = None  # the service's traffic vs the window before
    tenants: Counter[str] = field(default_factory=Counter, repr=False)  # up to MAX_TENANTS
    _messages: set = field(default_factory=set, repr=False)  # message (chain) variants
    _logged: Counter[str] = field(default_factory=Counter, repr=False)
    _sites: dict[str, LogSite] = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        return self.fingerprint[:8]

    @property
    def variants(self) -> int:
        """How many different messages the group has (values differ: ids, amounts...)."""
        return len(self._messages)

    @property
    def is_new(self) -> bool:
        return self.previous == 0

    @property
    def trend(self) -> str | None:
        return None if self.previous is None else describe_trend(self.count, self.previous)

    @property
    def severe(self) -> bool:
        """Fatal-level, or the kind of failure that's never noise: out of memory, a crash,
        data corruption. Ranked above ordinary steady errors however rare it is."""
        if self.levels.keys() & {"fatal", "critical", "crit", "emerg", "alert", "panic"}:
            return True
        return bool(SEVERE.search(" ".join([self.title, *self.causes])))

    @property
    def top_tenant(self) -> tuple[str, float, int] | None:
        """``(tenant, share of the errors, tenants seen)`` when the records carry one."""
        if not self.tenants:
            return None
        tenant, n = self.tenants.most_common(1)[0]
        return tenant, n / sum(self.tenants.values()), len(self.tenants)

    @property
    def baseline(self) -> float | None:
        """What the window before would predict at today's traffic: ``previous`` scaled by
        the traffic ratio. Spikes are measured against it, so twice the errors at twice the
        traffic isn't one."""
        if self.previous is None:
            return None
        return self.previous * self.traffic_ratio if self.traffic_ratio else float(self.previous)

    @property
    def rate_ratio(self) -> float | None:
        """The error rate now vs the window before (errors per log event of the service)."""
        if not self.previous or not self.traffic_ratio:
            return None
        return self.count / (self.previous * self.traffic_ratio)


@dataclass
class ErrorSummary:
    window: TimeRange
    previous_window: TimeRange | None
    groups: list[ErrorGroup]
    events: int
    previous_events: int | None
    truncated: bool  # the record limit was reached: counts are a lower bound
    resolved: int = 0  # groups seen in the previous window but not in this one
    traffic: int | None = None  # log events that aren't errors: the service's activity
    previous_traffic: int | None = None
    split_traces: int = 0  # records that are a piece of a stack trace: the shipper split them

    @property
    def traffic_ratio(self) -> float | None:
        if self.traffic is None or not self.previous_traffic:
            return None
        if self.previous_traffic < MIN_TRAFFIC or self.traffic < MIN_TRAFFIC:
            return None  # too little to compare
        return self.traffic / self.previous_traffic


MIN_TRAFFIC = 100  # log events per window below which traffic isn't compared
MAX_TENANTS = 200  # tenants counted per group
TENANT_FIELDS = ("tenant", "tenant_id", "tenantId", "customer_id", "customerId", "org_id",
                 "orgId", "workspace_id", "account_id", "client_id")  # fmt: skip
SEVERE = re.compile(
    r"OutOfMemoryError|out of memory|Segmentation fault|SIGSEGV|core dumped|fatal error:|"
    r"data corruption|corrupt(ed)?\b|checksum mismatch|integrity (check|violation) failed|"
    r"data loss|lost (messages|writes)|split.?brain|StackOverflowError",
    re.I,
)


def tenant_of(record: LogRecord) -> str | None:
    for key in TENANT_FIELDS:
        value = record.fields.get(key)
        if value is None:
            value = get_field(record, key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)[:80]
    m = re.search(r"\b(?:tenant|tenantId|tenant_id)[=:]\s*\"?([\w.-]+)", record.message[:400])
    return m.group(1) if m else None


def _fingerprint(info: ErrorInfo | None, record: LogRecord, site: LogSite | None, app) -> str:
    if info is not None:
        parts = []
        for exc in info.chain:
            frames = [(app.resolve(f), f.function) for f in exc.frames]
            own = [f"{p}:{fn}" for p, fn in frames if p]
            if not own:  # nothing from the project: the last few frames identify it
                own = [f"{f.file.rsplit('/', 1)[-1]}:{f.function}" for f in exc.frames[-3:]]
            parts.append(exc.type + "|" + ",".join(own))
            if not exc.frames:  # type and message fields only: the message tells them apart
                parts.append(normalize(exc.message))
        key = "exc:" + ";".join(parts)
    elif site is not None:
        key = f"site:{site.id}"
    else:
        key = "msg:" + normalize(record.message.split("\n", 1)[0])
    err = error_field(record)
    if info is None and err:  # one log call, different errors: "no space" vs "open files"
        key += "|err:" + normalize(err)
    return hashlib.sha1(key.encode()).hexdigest()


ERROR_FIELDS = ("error", "err", "error.message", "error_message", "reason", "cause")


def error_field(record: LogRecord) -> str | None:
    """The error a structured record carries in a field instead of a stack trace (zerolog's
    and zap's ``error``, logrus' ``error``, ECS's ``error.message``)."""
    for key in ERROR_FIELDS:
        value = record.fields.get(key)
        if value is None:
            value = get_field(record, key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return None


def _service(record: LogRecord) -> str | None:
    for key in SERVICE_FIELDS:
        value = record.fields.get(key)
        if value is None:
            value = get_field(record, key)
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    from .timeline import text_head

    return text_head(record)[1]  # "ERROR [payment-service] ..."


def _describe(group: ErrorGroup, info: ErrorInfo | None, app: AppFiles) -> None:
    """Fill in where the group's error was raised, from one representative record."""
    if info is None:
        return
    root = info.root
    # Where it was raised in the project: the root cause's deepest own frame, or, when the
    # root cause is all library code (a database driver), the nearest cause that has one.
    own: list = []
    for exc in info.chain:
        own = [(p, f) for p, f in ((app.resolve(f), f) for f in exc.frames) if p]
        if own:
            break
    if own and not root.logged_only:
        p, f = own[-1]
        group.raised_at, group.raised_in = f"{p}:{f.line}", f.function
    if root.frames and (root.frames[-1].is_library or app.resolve(root.frames[-1]) is None):
        group.origin = root.frames[-1].library  # the driver / client that threw it
        if group.origin and group.origin.startswith(_JDK_INTERNALS) and "." in root.type:
            group.origin = root.type.rsplit(".", 1)[0]  # sun.nio -> java.net: what it means
    # The cause's frames run from the entry point to where it was raised.
    group.path = [f"{Path(p).stem}.{f.function}" for p, f in own]
    group.frames_at = [f"{p}:{f.line}" for p, f in own]  # every frame of yours, outermost first
    if group.origin:
        group.path.append(group.origin)


def group_errors(
    records: Iterable[LogRecord],
    sites: Iterable[LogSite],
    app: AppFiles,
    leaks: LeakConfig | None = None,
    sample_fields: Iterable[str] = ID_FIELDS,
) -> dict[str, ErrorGroup]:
    """Group error records by cause. Returns ``{fingerprint: group}``, unordered."""
    matcher = Matcher(sites)
    groups: dict[str, ErrorGroup] = {}
    for record in records:
        info = extract_error(record)
        site = matcher.match(record)
        fp = _fingerprint(info, record, site, app)
        group = groups.get(fp)
        first_line = record.message.split("\n", 1)[0]
        if group is None:
            wrapped: list[str] = []
            if info is not None:
                # chain is root cause first; skip the wrappers at the outer end
                head = len(info.chain) - 1
                while head > 0 and info.chain[head].type.rsplit(".", 1)[-1] in WRAPPERS:
                    wrapped.append(info.chain[head].type.rsplit(".", 1)[-1])
                    head -= 1
                title = info.chain[head].title
                causes = [e.title for e in info.chain[:head]]
                exc_type = info.chain[head].type
            else:
                title = _LOGGER_PREFIX.sub("", _PLAIN_PREFIX.sub("", first_line))
                err = error_field(record)
                if err and err not in title:
                    title = f"{title}: {err}"
                causes, exc_type = [], None
            group = groups[fp] = ErrorGroup(fp, redact(title, leaks), exc_type, causes)
            group.causes = [redact(c, leaks) for c in causes]
            group.wrapped_in = wrapped
            _describe(group, info, app)
            group.sample = _sample(record, info, leaks, sample_fields)
        group.count += 1
        group._messages.add(tuple(e.message for e in info.chain) if info else first_line)
        ts = record.timestamp
        if ts is not None:
            group.hourly[hour_of(ts)] += 1
            if group.first_seen is None or ts < group.first_seen:
                group.first_seen = ts
            if group.last_seen is None or ts > group.last_seen:
                group.last_seen = ts
                group.sample = _sample(record, info, leaks, sample_fields)  # the latest
        if site is not None:
            group._logged[site.id] += 1
            group._sites[site.id] = site
        if len(group.trace_ids) < MAX_TRACE_IDS:
            trace = _trace_id(record)
            if trace:
                group.trace_ids.add(trace)
        service = _service(record)
        if service:
            group.services[service] += 1
        level = level_of(record)
        if not level:
            from .timeline import text_head

            level = text_head(record)[0]
        if level:
            group.levels[level.lower()] += 1
        tenant = tenant_of(record)
        if tenant and (tenant in group.tenants or len(group.tenants) < MAX_TENANTS):
            group.tenants[tenant] += 1
    for group in groups.values():
        if group._logged:
            group.logged_at = group._sites[group._logged.most_common(1)[0][0]]
        if group.variants > 1 and group.exc_type is None:
            group.title = redact(normalize(group.title), leaks)
        elif group.variants > 1:  # the values differ: show where they go, not one of them
            group.title = _normalize_title(group.title)
            group.causes = [_normalize_title(c) for c in group.causes]
    return groups


def _normalize_title(title: str) -> str:
    """``"KeyError: 'EUR'"`` -> ``"KeyError: <str>"``: the type stays, values are masked."""
    exc_type, sep, message = title.partition(": ")
    return f"{exc_type}{sep}{normalize(message)}" if sep else title


def hour_of(ts: datetime) -> str:
    """The UTC hour a time falls in, as stored in the history: ``2026-10-17T19``."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H")


def _trace_id(record: LogRecord) -> str | None:
    """The record's trace / request id: a field, or ``traceId=...`` in plain text."""
    for name in ("trace_id", "request_id"):
        value = get_field(record, name)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    first = record.message.split("\n", 1)[0]
    m = _TEXT_ID.search(first)
    if m and "span" not in m.group(1).lower():
        return m.group(2)
    s = _SLEUTH.search(first[:300])
    return s["trace"] if s else None


def _sample(
    record: LogRecord, info: ErrorInfo | None, leaks: LeakConfig | None, fields: Iterable[str]
) -> dict:
    ids = {}
    for name in fields:
        value = get_field(record, name)
        if value is not None and not isinstance(value, (dict, list)):
            ids[name] = str(value)
    if not ids:  # plain text: "[order-service,traceId=8fa23bc1,spanId=91ac42]"
        for m in _TEXT_ID.finditer(record.message.split("\n", 1)[0]):
            key = re.sub(r"(?<=[a-z])_?(id)$", "_id", m.group(1).lower())
            ids.setdefault(key, m.group(2))
    if not ids:  # "[inventory-service,7f3a0000c2,a1]"
        s = _SLEUTH.search(record.message.split("\n", 1)[0][:300])
        if s:
            ids["trace_id"], ids["span_id"] = s["trace"], s["span"]
    text = info.text if info and info.text else None
    if text and len(text) > TRACEBACK_CHARS:
        text = "…" + text[-TRACEBACK_CHARS:]  # the end says where it was raised
    return {
        "timestamp": record.timestamp.isoformat() if record.timestamp else None,
        "message": redact(record.message.split("\n", 1)[0], leaks),
        "ids": ids,
        "traceback": redact(text, leaks) if text else None,
    }


def rank(groups: Iterable[ErrorGroup], cfg: LintConfig) -> list[ErrorGroup]:
    """New groups first, then spiking ones, then the biggest."""

    def key(g: ErrorGroup):
        spiking = g.baseline is not None and is_spike(g.count, g.baseline, cfg)
        # severe but rare (2 "data corruption" a day) must not sink under a steady storm
        order = 0 if g.is_new else 1 if spiking else 2 if g.severe else 3
        # ties: a group with a stack trace explains more than a line of text
        return (order, -g.count, g.exc_type is None, g.title)

    return sorted(groups, key=key)


def error_window(
    root: Path,
    hours: float,
    since_deploy: str | None = None,
    deployed_at: str | None = None,
    now: datetime | None = None,
) -> tuple[TimeRange, str]:
    """The window to summarize and how to describe it: the last ``hours``, or from a deploy
    (a git ref) until now. Raises ValueError with a user-facing message."""
    if not since_deploy:
        if deployed_at:
            raise ValueError("deployed_at needs since_deploy")
        window = TimeRange.last(hours)
        return window, f"in the last {hours:g}h"
    at = parse_iso(deployed_at) if deployed_at else None
    deploy = resolve_deploy(root, since_deploy, at=at)
    end = now or TimeRange.last(0).end
    if deploy.at >= end:
        when = f"{deploy.at:%Y-%m-%d %H:%M} UTC"
        raise ValueError(f"{deploy.ref} was deployed at {when}, in the future")
    return TimeRange(deploy.at, end), f"since {deploy.ref} ({deploy.at:%Y-%m-%d %H:%M} UTC)"


def summarize_errors(
    backend: LogBackend,
    sites: Iterable[LogSite],
    app: AppFiles,
    window: TimeRange,
    previous: TimeRange | None,
    cfg: LintConfig,
    leaks: LeakConfig | None = None,
    limit: int = 5000,
    sample_fields: Iterable[str] = ID_FIELDS,
) -> ErrorSummary:
    """Fetch the errors in ``window`` (and ``previous``, for trends) and group them."""
    sites = list(sites)
    records = backend.query_errors(window, limit=limit)
    groups = group_errors(records, sites, app, leaks, sample_fields)
    previous_events = None
    resolved = 0
    truncated = len(records) >= limit
    traffic = previous_traffic = None
    if previous is not None:
        before_records = backend.query_errors(previous, limit=limit)
        before = group_errors(before_records, sites, app, leaks, sample_fields)
        for fp, g in groups.items():
            g.previous = before[fp].count if fp in before else 0
        previous_events = len(before_records)
        resolved = len(before.keys() - groups.keys())
        truncated = truncated or len(before_records) >= limit
        traffic = _activity(backend, window, len(records))
        previous_traffic = _activity(backend, previous, previous_events)
    summary = ErrorSummary(
        window=window,
        previous_window=previous,
        groups=[],
        split_traces=sum(1 for r in records if is_trace_piece(r.message)),
        events=len(records),
        previous_events=previous_events,
        truncated=truncated,
        resolved=resolved,
        traffic=traffic,
        previous_traffic=previous_traffic,
    )
    for g in groups.values():
        g.traffic_ratio = summary.traffic_ratio
    summary.groups = rank(groups.values(), cfg)
    return summary


def _activity(backend: LogBackend, window: TimeRange, errors: int) -> int | None:
    """Log events in ``window`` that aren't errors: a stand-in for the service's traffic
    (requests log as they're handled). None when the backend can't count them."""
    try:
        total = backend.count_total(window)
    except Exception:  # traffic is extra evidence; the summary works without it
        return None
    return None if total is None else max(total - errors, 0)


def group_json(g: ErrorGroup) -> dict:
    ids = g.sample.get("ids") or {}
    trace = ids.get("trace_id") or next((v for k, v in ids.items() if "trace" in k.lower()), None)
    return {
        "id": g.id,
        "title": g.title,
        "type": g.exc_type,
        "causes": g.causes,
        "root_cause": g.causes[0] if g.causes else None,  # flat, for tables
        "wrapped_in": g.wrapped_in,
        "example_trace": trace,
        "count": g.count,
        "previous": g.previous,
        "trend": g.trend,
        "traffic_ratio": round(g.traffic_ratio, 2) if g.traffic_ratio else None,
        "severe": g.severe,
        "tenant": _tenant_json(g),
        "rate_ratio": round(g.rate_ratio, 2) if g.rate_ratio is not None else None,
        "new": g.is_new,
        "first_seen": g.first_seen.isoformat() if g.first_seen else None,
        "last_seen": g.last_seen.isoformat() if g.last_seen else None,
        "raised_at": g.raised_at,
        "raised_in": g.raised_in,
        "origin": g.origin,
        "logged_at": g.logged_at.id if g.logged_at else None,
        "logged_template": g.logged_at.template if g.logged_at else None,
        "path": g.path,
        "services": dict(g.services),
        "levels": dict(g.levels),
        "variants": g.variants,
        "sample": g.sample,
        "history": history_json(g.history),
    }


def _tenant_json(g: ErrorGroup) -> dict | None:
    """Only the leading tenant and shares: never other tenants' ids."""
    top = g.top_tenant
    if top is None:
        return None
    return {"top": top[0], "share": round(top[1], 3), "tenants": top[2]}


def history_json(h) -> dict | None:
    from .history import history_json as convert  # history imports this module

    return convert(h)


def summary_json(s: ErrorSummary, top: int | None = None) -> dict:
    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat()},
        "events": s.events,
        "previous_events": s.previous_events,
        "groups_total": len(s.groups),
        "resolved": s.resolved,
        "truncated": s.truncated,
        "split_traces": s.split_traces,
        "groups": [group_json(g) for g in s.groups[:top]],
    }
