"""The one interface every backend implements. Nothing else in logsite knows the vendor."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..context import mentions
from ..matching import Matcher
from ..models import LogRecord, LogSite, Usage, parse_iso
from ..tracebacks import is_error


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    @classmethod
    def last(cls, hours: float = 24) -> TimeRange:
        end = datetime.now(timezone.utc)
        return cls(start=end - timedelta(hours=hours), end=end)

    def previous(self) -> TimeRange:
        """The range of the same length that ends where this one starts."""
        return TimeRange(start=self.start - (self.end - self.start), end=self.start)


class LogBackend(ABC):
    """Adapter for a log store (Loki, OpenSearch, CloudWatch, a local file, ...)."""

    name: str = "base"

    @abstractmethod
    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        """Return raw log records in ``time_range``, optionally filtered by substring."""

    def count_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, int]:
        """Return ``{site.id: count}`` for the given sites.

        The default implementation fetches records and matches them client-side. Backends
        that can aggregate server-side (e.g. on a ``lineno`` label) should override this.
        """
        return tally(Matcher(sites), {}, self.query_logs(time_range, limit=limit))

    def find_records(
        self, field: str, value: str, time_range: TimeRange, limit: int = 5000
    ) -> list[LogRecord]:
        """Records whose ``field`` (or a synonym, e.g. ``traceId`` for ``trace_id``) equals
        ``value``; records without the field count if their message mentions it.

        The default narrows with the backend's text filter (``contains``) and checks fields
        client-side, which works for every backend whose stored line includes the value.
        """
        records = self.query_logs(time_range, contains=value, limit=limit)
        return [r for r in records if mentions(r, field, value)]

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        """Error records (error level, or carrying an exception) in ``time_range``.

        The default filters :meth:`query_logs` client-side, so other lines count towards
        ``limit``. Backends that can filter server-side override this.
        """
        return [r for r in self.query_logs(time_range, limit=limit) if is_error(r)]

    def count_total(self, time_range: TimeRange) -> int | None:
        """All log events in ``time_range`` (every level), counted server-side; None when
        the backend can't count them cheaply. Used as the service's traffic, so error
        spikes are measured as rates."""
        return None

    def usage_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, Usage]:
        """Return ``{site.id: Usage(count, bytes)}``.

        The default measures the records it fetches, so it's exact but shares the ``limit``
        cap of :meth:`count_by_line`. Backends that aggregate server-side override this.
        """
        return measure(Matcher(sites), self.query_logs(time_range, limit=limit))


def measure(matcher: Matcher, records: Iterable[LogRecord]) -> dict[str, Usage]:
    """Count and sum the stored size of records per matched site."""
    usage: dict[str, Usage] = {}
    for record in records:
        site = matcher.match(record)
        if site is None:
            continue
        u = usage.setdefault(site.id, Usage())
        u.count += 1
        u.bytes += record.size if record.size is not None else len(record.message.encode())
    return usage


def tally(
    matcher: Matcher,
    locations: Mapping[tuple[str, int], int],
    records: Iterable[LogRecord] = (),
) -> dict[str, int]:
    """Turn server-side ``{(file, line): count}`` plus raw records into ``{site.id: count}``."""
    counts: Counter[str] = Counter()
    for (src, line), n in locations.items():
        site = matcher.match_location(src, line)
        if site is not None:
            counts[site.id] += n
    for record in records:
        site = matcher.match(record)
        if site is not None:
            counts[site.id] += 1
    return dict(counts)


# Where JSON loggers put the time: Python, logstash-logback-encoder / ECS (@timestamp), Go's
# slog / zerolog (time), zap (ts).
TIME_KEYS = ("timestamp", "@timestamp", "time", "ts", "asctime", "datetime")


def parse_line(raw: str, timestamp: datetime | None = None) -> LogRecord:
    """Parse a log line: JSON objects become structured records, anything else is plain text."""
    raw = raw.rstrip("\n")
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("record"), dict):
            # The traceback is only in "text", loguru's formatted line.
            return _loguru_record({**data["record"], "_text": data.get("text")}, raw, timestamp)
        if isinstance(data, dict):
            message = next(
                (str(data[k]) for k in ("message", "msg", "event", "log") if k in data), raw
            )
            ts = timestamp or _parse_ts(next((data[k] for k in TIME_KEYS if data.get(k)), None))
            return LogRecord(timestamp=ts, message=message, fields=data, size=_size(raw))
    klog = _KLOG.match(stripped)
    if klog:
        return _klog_record(klog, raw, timestamp)
    data = parse_logfmt(stripped)
    if data:
        message = next((str(data[k]) for k in ("msg", "message") if k in data), raw)
        ts = timestamp or _parse_ts(next((data[k] for k in TIME_KEYS if data.get(k)), None))
        return LogRecord(timestamp=ts, message=message, fields=data, size=_size(raw))
    return LogRecord(timestamp=timestamp or _leading_ts(raw), message=raw, size=_size(raw))


# logfmt (logrus' and go-kit's text format, Grafana's tools, Heroku):
# time="2026-10-25T03:04:02Z" level=error msg="process ended: signal: killed" err="..."
_LOGFMT_PAIR = re.compile(r'([\w][\w.\-/]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)(?:\s+|$)')
_LOGFMT_KEYS = {"level", "lvl", "msg", "message", "severity"}


def parse_logfmt(text: str) -> dict | None:
    """``key=value key="quoted value"`` pairs, or None when the line isn't logfmt: every
    character must be in a pair, with a level or a message among them."""
    data: dict = {}
    pos = 0
    while pos < len(text):
        m = _LOGFMT_PAIR.match(text, pos)
        if not m or m.end() == pos:
            return None
        key, value = m.group(1), m.group(2)
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        data[key] = value
        pos = m.end()
    return data if len(data) >= 2 and _LOGFMT_KEYS & data.keys() else None


# klog, Kubernetes' Go logger: severity letter, MMDD, time, thread, file:line, message
# E1025 03:04:55.030320       1 reflector.go:227] "Failed to watch" err="..." logger="..."
_KLOG = re.compile(
    r"^(?P<sev>[IWEF])(?P<mmdd>\d{4}) (?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+(?P<thread>\d+) "
    r"(?P<file>[^\s:\]]+):(?P<line>\d+)\] (?P<rest>.*)$",
    re.S,
)
_KLOG_LEVELS = {"I": "info", "W": "warning", "E": "error", "F": "fatal"}


def _klog_record(m: re.Match, raw: str, timestamp: datetime | None) -> LogRecord:
    rest = m["rest"]
    fields: dict = {"level": _KLOG_LEVELS[m["sev"]], "caller": f"{m['file']}:{m['line']}",
                    "thread": m["thread"]}  # fmt: skip
    message = rest
    if rest.startswith('"'):  # structured: "message" key=value ...
        q = re.match(r'"((?:[^"\\]|\\.)*)"\s*', rest)
        if q:
            try:
                message = json.loads(f'"{q.group(1)}"')
            except json.JSONDecodeError:
                message = q.group(1)
            fields.update(parse_logfmt(f"msg=1 {rest[q.end() :]}".strip()) or {})
            fields.pop("msg", None)
    fields["msg"] = message
    if timestamp is None:  # klog leaves the year out: this year's
        try:
            stamp = f"{datetime.now(timezone.utc).year}{m['mmdd']} {m['time'][:15]}"
            timestamp = datetime.strptime(stamp, "%Y%m%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        except ValueError:
            timestamp = None
    return LogRecord(timestamp=timestamp, message=message, fields=fields, size=_size(raw))


# "2026-10-14 10:00:05,123 ERROR ...": logging's default asctime, or ISO 8601.
_LEADING_TS = re.compile(
    r"^\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{3}(?:\d{3})?)?(?:Z|[+-]\d{2}:\d{2})?)"
)


def _leading_ts(raw: str) -> datetime | None:
    """The timestamp a plain-text line starts with, if any (naive means UTC)."""
    m = _LEADING_TS.match(raw)
    if not m:
        return None
    try:
        ts = parse_iso(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _loguru_record(rec: dict, raw: str, timestamp: datetime | None) -> LogRecord:
    """loguru's ``serialize=True`` output: ``{"text": ..., "record": {"message", "file",
    "line", "level", "extra", "time", ...}}``. Flattened so file/line matching and context
    checks see the same fields as any other JSON log."""
    file = rec.get("file") if isinstance(rec.get("file"), dict) else {}
    level = rec.get("level") if isinstance(rec.get("level"), dict) else {}
    time = rec.get("time") if isinstance(rec.get("time"), dict) else {}
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    exception = rec.get("exception") if isinstance(rec.get("exception"), dict) else {}
    text = rec.get("_text") if isinstance(rec.get("_text"), str) else ""
    fields = {
        **extra,
        "exc_type": exception.get("type"),
        "exc_message": exception.get("value"),
        "exc_text": text if "Traceback (most recent call last):" in text else None,
        "pathname": file.get("path") or file.get("name"),
        "filename": file.get("name"),
        "lineno": rec.get("line"),
        "levelname": level.get("name"),
        "funcName": rec.get("function"),
        "name": rec.get("name"),
    }
    ts = timestamp or _parse_ts(time.get("timestamp") or time.get("repr"))
    return LogRecord(
        timestamp=ts,
        message=str(rec.get("message", "")),
        fields={k: v for k, v in fields.items() if v is not None},
        size=_size(raw),
    )


def _size(raw: str) -> int:
    return len(raw.encode("utf-8", errors="replace"))


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # epoch seconds (zap, structlog), or milliseconds (pino, bunyan, Date.now()),
        # microseconds, nanoseconds: told apart by size
        seconds = float(value)
        while abs(seconds) > 1e11:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None
