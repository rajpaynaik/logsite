"""Latency from logs: how long each logged request or operation took, read from the lines
the service already writes (``duration_ms=…``, ``"took": "1.2s"``, ``completed in 350ms``),
per log call and endpoint, as p50/p95/p99 an hour.

A timing comes from a field first, then the message. A field named with its unit
(``duration_ms``, ``elapsed_seconds``) or a value with one (``"1.2s"``, Go's ``"2m3.5s"``) is
exact. A bare number in a field without a unit (``duration: 3``) is a guess: a whole number
from a million up is nanoseconds (slog's JSON handler), one with a fraction under 1,000 is
seconds (zap), any other is milliseconds.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import LogRecord

# field -> seconds per unit of a bare number; None: guess (see the module docstring)
UNITS: dict[str, float | None] = {
    "duration_ms": 1e-3, "elapsed_ms": 1e-3, "latency_ms": 1e-3, "took_ms": 1e-3,
    "response_time_ms": 1e-3, "request_duration_ms": 1e-3, "time_ms": 1e-3,
    "durationms": 1e-3, "responsetime": 1e-3,  # pino-http's responseTime
    "duration_us": 1e-6, "duration_ns": 1e-9,
    "duration_s": 1.0, "duration_seconds": 1.0, "elapsed_seconds": 1.0,
    "request_time": 1.0, "upstream_response_time": 1.0,  # nginx
    "http.server.duration": 1e-3,  # OpenTelemetry, older semantic conventions
    "http.server.request.duration": 1.0,
    "duration": None, "elapsed": None, "latency": None, "took": None, "response_time": None,
}  # fmt: skip
_UNIT = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "sec": 1.0, "secs": 1.0,
         "seconds": 1.0, "m": 60.0, "min": 60.0, "h": 3600.0, "milliseconds": 1e-3}  # fmt: skip
_AMOUNT = r"(\d+(?:\.\d+)?)\s*(ns|us|µs|ms|milliseconds|seconds|secs|sec|s|min|m|h)\b"
_GO_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+$")
_GO_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_IN_TEXT = re.compile(
    r"(?:\btook|\b(?:completed|finished|done|processed|served|responded|handled) in|"
    r"\belapsed|\bduration|\blatency|\bresponse time)[=:]?\s*" + _AMOUNT + r"|\(" + _AMOUNT
    + r"\)\s*$",
    re.I,
)  # fmt: skip

# Lines that may carry a timing, for a log store to filter on before sending them (RE2 and
# Python alike); duration() decides.
LINE_FILTER = (r"(?i)(duration|elapsed|latency|took|response_?time|request_time|"
               r"(completed|finished|done|processed|served|responded|handled) in|"
               r"\([0-9.]+ ?(ms|s|us)\))")  # fmt: skip

ENDPOINT_FIELDS = ("http.route", "route", "endpoint", "url.path", "http.target", "path",
                   "request_path", "uri", "url")  # fmt: skip
METHOD_FIELDS = ("http.request.method", "http.method", "method", "request_method")
_ACCESS = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/[^\s?\"]*)")
_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,})$",
    re.I,
)


def duration(record: LogRecord) -> float | None:
    """How long the logged operation took, in seconds, or None."""
    flat = _flat(record.fields)
    for name, unit in UNITS.items():
        value = flat.get(name)
        if value is None:
            continue
        seconds = _value(value, unit)
        if seconds is not None:
            return seconds
    m = _IN_TEXT.search(record.message.split("\n", 1)[0])
    if m:
        amount, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        return float(amount) * _UNIT[unit.lower()]
    return None


def endpoint(record: LogRecord) -> str | None:
    """``"POST /orders/{id}"``: the route (ids in it as ``{id}``), with the method if known."""
    flat = _flat(record.fields)
    path = next((str(flat[k]) for k in ENDPOINT_FIELDS if flat.get(k)), None)
    method = next((str(flat[k]).upper() for k in METHOD_FIELDS if flat.get(k)), None)
    if path is None:
        m = _ACCESS.search(record.message.split("\n", 1)[0])
        if not m:
            return None
        method, path = m.group(1), m.group(2)
    path = path.split("?", 1)[0]
    if "://" in path:  # a whole URL: its path
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    if path.startswith("/"):
        path = "/".join("{id}" if _ID_SEGMENT.match(p) else p for p in path.split("/"))
    return f"{method} {path}" if method else path


def _value(value: object, unit: float | None) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if _GO_DURATION.match(text):  # "1.5s", "2m3.5s", "350ms"
            return sum(float(n) * _UNIT[u] for n, u in _GO_PART.findall(text))
        m = re.fullmatch(_AMOUNT, text, re.I)
        if m:
            return float(m.group(1)) * _UNIT[m.group(2).lower()]
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if unit is not None:
        return number * unit
    if number.is_integer() and number >= 1e6:
        return number * 1e-9  # nanoseconds
    if not number.is_integer() and number < 1000:
        return number  # seconds
    return number * 1e-3  # milliseconds


def _flat(fields: Mapping, prefix: str = "") -> dict[str, object]:
    """Fields by dotted path, lower-cased: ``{"http": {"route": …}}`` -> ``"http.route"``."""
    out: dict[str, object] = {}
    for key, value in fields.items():
        name = f"{prefix}{key}".lower()
        if isinstance(value, Mapping):
            out.update(_flat(value, name + "."))
        else:
            out.setdefault(name, value)
            out.setdefault(name.rsplit(".", 1)[-1], value)
    return out


# --- percentiles ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Timing:
    """An hour's (or any window's) timings of one log call and endpoint, in seconds."""

    n: int
    p50: float
    p95: float
    p99: float
    max: float

    @classmethod
    def of(cls, values: Iterable[float]) -> Timing | None:
        xs = sorted(values)
        if not xs:
            return None
        return cls(len(xs), _rank(xs, 0.50), _rank(xs, 0.95), _rank(xs, 0.99), xs[-1])


def _rank(xs: list[float], q: float) -> float:
    """The nearest-rank percentile of sorted ``xs``."""
    return xs[max(math.ceil(q * len(xs)) - 1, 0)]


def timings(matcher, records: Iterable[LogRecord]) -> dict[tuple[str, str], list[float]]:
    """``{(site id, endpoint): [seconds, …]}`` for the records that carry a timing. A record
    no log call matched (an access log, a proxy) counts under site ``""``; one without an
    endpoint under endpoint ``""``."""
    out: dict[tuple[str, str], list[float]] = {}
    for record in records:
        seconds = duration(record)
        if seconds is None:
            continue
        site = matcher.match(record)
        key = (site.id if site else "", endpoint(record) or "")
        out.setdefault(key, []).append(seconds)
    return out


def format_seconds(seconds: float | None) -> str:
    """``0.18`` -> ``"180ms"``, ``1.4`` -> ``"1.4s"``, ``95`` -> ``"1.6m"``."""
    if seconds is None:
        return "—"
    if seconds < 1e-3:
        return f"{seconds * 1e6:.0f}µs"
    if seconds < 1:
        return f"{seconds * 1e3:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


# --- what got slower ------------------------------------------------------------------------


@dataclass
class LatencyConfig:
    window_hours: float = 24.0  # logsite slow: this long, against as long before
    ratio: float = 2.0  # slower: p95 this many times the window before
    min_calls: int = 100  # ... over at least this many timed calls in each window

    @classmethod
    def from_dict(cls, data: Mapping) -> LatencyConfig:
        return cls(
            window_hours=float(data.get("window_hours", cls.window_hours)),
            ratio=float(data.get("ratio", cls.ratio)),
            min_calls=int(data.get("min_calls", cls.min_calls)),
        )


@dataclass
class Slowdown:
    site: str
    endpoint: str
    before: dict  # store.latency() rows
    after: dict

    @property
    def ratio(self) -> float:
        return self.after["p95"] / self.before["p95"] if self.before["p95"] else math.inf


def windows(now: datetime, hours: float) -> tuple[datetime, datetime, datetime]:
    """``(end, middle, start)``: the last ``hours`` and as long before, in whole hours ending
    with the last complete one, as the timings are stored."""
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    span = timedelta(hours=max(round(hours), 1))
    return end, end - span, end - 2 * span


def slower(store, start, middle, end, cfg: LatencyConfig) -> list[Slowdown]:
    """Log calls and endpoints whose p95 from ``middle`` to ``end`` is ``cfg.ratio`` times or
    more what it was from ``start`` to ``middle``, slowest jump first."""
    before = {(r["site"], r["endpoint"]): r for r in store.latency(start, middle)}
    out = []
    for r in store.latency(middle, end):
        b = before.get((r["site"], r["endpoint"]))
        if b is None or min(b["n"], r["n"]) < cfg.min_calls:
            continue
        if r["p95"] >= cfg.ratio * b["p95"]:
            out.append(Slowdown(r["site"], r["endpoint"], b, r))
    return sorted(out, key=lambda s: s.ratio, reverse=True)


def overall(rows: list[dict]) -> dict | None:
    """All of ``store.latency()``'s rows as one: calls, and p50/p95/p99 weighted by them."""
    n = sum(r["n"] for r in rows)
    if not n:
        return None
    out = {k: sum(r[k] * r["n"] for r in rows) / n for k in ("p50", "p95", "p99")}
    return {"n": n, **out, "max": max(r["max"] for r in rows)}


def slow_json(found: list[Slowdown], start, middle, end) -> dict:
    return {
        "window": {
            "before": start.isoformat(),
            "from": middle.isoformat(),
            "until": end.isoformat(),
        },  # fmt: skip
        "slower": [
            {
                "site": s.site or None,
                "endpoint": s.endpoint or None,
                "ratio": round(s.ratio, 2),
                "before": s.before,
                "after": s.after,
            }
            for s in found
        ],  # fmt: skip
    }


def render_slow(found: list[Slowdown], pal, width: int, hours: float, releases=()) -> list[str]:
    from .report import _cut, short_place

    span = f"{hours:.0f}h" if hours >= 1 else f"{hours * 60:.0f} min"
    lines = [pal.bold(f"Slower in the last {span}, against the {span} before"), ""]
    if not found:
        return lines + [pal.gray("Nothing got slower.")]
    head = ("#", "P95", "P99", "CALLS", "WHERE", "LOG CALL")
    rows = [(str(i), f"{format_seconds(s.before['p95'])} → {format_seconds(s.after['p95'])}",
             f"{format_seconds(s.before['p99'])} → {format_seconds(s.after['p99'])}",
             f"{s.after['n']:,}", s.endpoint or "—", short_place(s.site or None))
            for i, s in enumerate(found, 1)]  # fmt: skip
    w = [max(len(head[c]), *(len(r[c]) for r in rows)) for c in range(len(head))]
    fixed = sum(w) - w[4] + 2 * (len(head) - 1) + 2
    w[4] = max(min(w[4], width - fixed), 12)
    right = {0, 3}

    def cells(row) -> list[str]:
        return [(_cut(t, n).rjust(n) if c in right else _cut(t, n).ljust(n))
                for c, (t, n) in enumerate(zip(row, w, strict=True))]  # fmt: skip

    lines += ["  " + pal.bold(pal.gray("  ".join(cells(head)).rstrip())),
              "  " + pal.gray("─" * (sum(w) + 2 * (len(head) - 1)))]  # fmt: skip
    for row in rows:
        n, p95, p99, calls, where, site = cells(row)
        lines.append("  " + "  ".join([pal.gray(n), pal.bold_yellow(p95), pal.yellow(p99),
                                       calls, pal.bold_white(where),
                                       pal.gray(site) if site.strip() == "—"
                                       else pal.bold_cyan(site)]).rstrip())  # fmt: skip
    if releases:
        names = ", ".join(f"{r.name} ({r.at:%b %d %H:%M})" for r in releases)
        lines += ["", pal.gray(f"Released in this window: {names}")]
    return lines
