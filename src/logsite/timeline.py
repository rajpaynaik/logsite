"""Request timeline: every log line for one request (trace_id, request_id...), in order,
across services, with lines from this codebase linked to the code that wrote them."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .backends import LogBackend, TimeRange
from .context import get_field
from .leaks import LeakConfig, redact
from .matching import Matcher
from .models import LogRecord, LogSite
from .tracebacks import ERROR_WORDS, LEVEL_FIELDS, WARNING_WORDS

# Where log stores and shippers usually put the emitting service's name.
SERVICE_FIELDS = (
    "service", "service.name", "service_name", "app", "application", "k8s.container.name",
    "kubernetes.container_name", "container", "container_name", "job", "logStreamName",
)  # fmt: skip


@dataclass
class Entry:
    timestamp: datetime | None
    service: str | None
    level: str | None
    message: str  # redacted
    site: LogSite | None  # the log call in this codebase, if it came from here

    @property
    def is_error(self) -> bool:
        return (self.level or "").lower() in ERROR_WORDS

    @property
    def is_problem(self) -> bool:
        return self.is_error or (self.level or "").lower() in WARNING_WORDS


@dataclass
class Timeline:
    field: str
    value: str
    entries: list[Entry] = field(default_factory=list)

    @property
    def services(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.entries:
            seen.setdefault(e.service or "?", None)
        return list(seen)

    @property
    def duration(self) -> timedelta | None:
        times = [e.timestamp for e in self.entries if e.timestamp]
        return max(times) - min(times) if len(times) > 1 else None

    @property
    def first_error(self) -> Entry | None:
        return next((e for e in self.entries if e.is_error), None)

    @property
    def first_problem(self) -> Entry | None:
        """The first warning or error: often where the cause shows up, before the failure."""
        return next((e for e in self.entries if e.is_problem), None)


# Plain text: "2026-10-18 14:22:31.812 ERROR [payment-service] Logger - ..." or Spring
# Sleuth's "[order-service,traceId=..,spanId=..]": the first bracket after the level, when it
# names a service rather than a thread ("[main]", "[http-nio-8080-exec-17]").
_TEXT_HEAD = re.compile(
    r"^\S+[ T]\S+\s+(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL)\s+"
    r"(?:\[(?P<svc>[A-Za-z][\w.-]*?)(?:,[^\]]*)?\])?"
)
_THREADISH = re.compile(r"(^main$|^restartedMain$|nio|exec|thread|pool|scheduling|-\d+$|\d$)", re.I)


def text_head(record: LogRecord) -> tuple[str | None, str | None]:
    """``(level, service)`` from a plain-text line's prefix, for records without fields."""
    m = _TEXT_HEAD.match(record.message)
    if not m:
        return None, None
    svc = m["svc"]
    if svc and _THREADISH.search(svc):
        svc = None
    return m["level"].lower(), svc


def _first(record: LogRecord, keys: Iterable[str]) -> str | None:
    for key in keys:
        value = record.fields.get(key)  # dotted keys from flattened stores, e.g. service.name
        if value is None:
            value = get_field(record, key)  # nested, or under a synonym
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    return None


def build_timeline(
    records: Iterable[LogRecord],
    sites: Iterable[LogSite],
    field_name: str,
    value: str,
    here: str | None = None,
    leaks: LeakConfig | None = None,
) -> Timeline:
    """Order records by time and attribute each to a service and, if it's ours, a log call.

    ``here`` names this codebase's service for records that carry no service field but
    match one of its log calls.
    """
    matcher = Matcher(sites)
    tl = Timeline(field=field_name, value=value)
    for r in records:
        site = matcher.match(r)
        text_level, text_service = text_head(r)
        level = _first(r, LEVEL_FIELDS) or text_level or (site.level if site else None)
        service = _first(r, SERVICE_FIELDS) or text_service or (here if site else None)
        tl.entries.append(Entry(r.timestamp, service, level, redact(r.message, leaks), site))
    tl.entries.sort(key=lambda e: (e.timestamp is None, e.timestamp or datetime.min))
    return tl


def find_timeline(
    backend: LogBackend,
    sites: Iterable[LogSite],
    field_name: str,
    value: str,
    time_range: TimeRange,
    limit: int = 1000,
    here: str | None = None,
    leaks: LeakConfig | None = None,
) -> Timeline:
    records = backend.find_records(field_name, value, time_range, limit=limit)
    return build_timeline(records, sites, field_name, value, here=here, leaks=leaks)


def timeline_json(tl: Timeline) -> dict:
    """JSON-ready form of a timeline (CLI --format json, MCP)."""
    return {
        "field": tl.field,
        "value": tl.value,
        "services": tl.services,
        "duration_seconds": tl.duration.total_seconds() if tl.duration else None,
        "entries": [
            {
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "service": e.service,
                "level": e.level,
                "message": e.message,
                "site": e.site.id if e.site else None,
                "template": e.site.template if e.site else None,
            }
            for e in tl.entries
        ],
    }
