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
from datetime import datetime
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
from .tracebacks import ErrorInfo, Frame, extract_error, level_of

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


class AppFiles:
    """The project's Python files, to tell its own frames from library ones and to turn a
    deployed path (``/srv/app/checkout.py``) into a project path (``app/checkout.py``)."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._by_name: dict[str, list[str]] = {}
        for p in paths:
            self._by_name.setdefault(p.rsplit("/", 1)[-1], []).append(p)

    @classmethod
    def scan(cls, root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> AppFiles:
        root = root.resolve()
        return cls(f.relative_to(root).as_posix() for f in iter_source_files(root, excludes))

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
    services: Counter[str] = field(default_factory=Counter)
    levels: Counter[str] = field(default_factory=Counter)
    sample: dict = field(default_factory=dict)
    wrapped_in: list[str] = field(default_factory=list)  # framework wrappers around it
    trace_ids: set[str] = field(default_factory=set, repr=False)  # up to MAX_TRACE_IDS
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


@dataclass
class ErrorSummary:
    window: TimeRange
    previous_window: TimeRange | None
    groups: list[ErrorGroup]
    events: int
    previous_events: int | None
    truncated: bool  # the record limit was reached: counts are a lower bound
    resolved: int = 0  # groups seen in the previous window but not in this one


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
    return hashlib.sha1(key.encode()).hexdigest()


def _service(record: LogRecord) -> str | None:
    for key in SERVICE_FIELDS:
        value = record.fields.get(key)
        if value is None:
            value = get_field(record, key)
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    return None


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
        if level:
            group.levels[level.lower()] += 1
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


def _trace_id(record: LogRecord) -> str | None:
    """The record's trace / request id: a field, or ``traceId=...`` in plain text."""
    for name in ("trace_id", "request_id"):
        value = get_field(record, name)
        if value is not None and not isinstance(value, (dict, list)):
            return str(value)
    m = _TEXT_ID.search(record.message.split("\n", 1)[0])
    return m.group(2) if m and "span" not in m.group(1).lower() else None


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
        spiking = g.previous is not None and is_spike(g.count, g.previous, cfg)
        # ties: a group with a stack trace explains more than a line of text
        return (0 if g.is_new else 1 if spiking else 2, -g.count, g.exc_type is None, g.title)

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
    if previous is not None:
        before_records = backend.query_errors(previous, limit=limit)
        before = group_errors(before_records, sites, app, leaks, sample_fields)
        for fp, g in groups.items():
            g.previous = before[fp].count if fp in before else 0
        previous_events = len(before_records)
        resolved = len(before.keys() - groups.keys())
        truncated = truncated or len(before_records) >= limit
    return ErrorSummary(
        window=window,
        previous_window=previous,
        groups=rank(groups.values(), cfg),
        events=len(records),
        previous_events=previous_events,
        truncated=truncated,
        resolved=resolved,
    )


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
    }


def summary_json(s: ErrorSummary, top: int | None = None) -> dict:
    return {
        "window": {"start": s.window.start.isoformat(), "end": s.window.end.isoformat()},
        "events": s.events,
        "previous_events": s.previous_events,
        "groups_total": len(s.groups),
        "resolved": s.resolved,
        "truncated": s.truncated,
        "groups": [group_json(g) for g in s.groups[:top]],
    }
