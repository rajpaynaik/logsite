"""Core data types shared by every logsite module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Matches %-style placeholders (%s, %d, %(name)s, %.2f, %-5r ...; Go's %v, %q, %w, %+v ...)
# and str.format / f-string / SLF4J placeholders that the indexers normalise to "{}".
_PLACEHOLDER = re.compile(r"%(?:\([^)]*\))?[#0\- +]*\d*(?:\.\d+)?[sdifrxXeEgGcoauvqwTtpbU%]|\{\}")
# "Checkout.java:42", "/srv/app/checkout.go:42": a file and line in one value (zap's
# "caller", logrus's "file").
_FILE_AND_LINE = re.compile(r"^(?P<file>.+\.[A-Za-z0-9]+):(?P<line>\d+)$")
# Letters of fixed text a template with placeholders needs before it's matched against
# messages: "{}: {}" or "[{}] {}" would match almost any line and claim errors it never
# logged. A constant message ("ok") only matches itself, so it needs none.
MIN_LETTERS = 4


def parse_iso(value: str) -> datetime:
    """``datetime.fromisoformat`` that also accepts a trailing ``Z`` on Python 3.10.

    Git >= 2.5x prints UTC dates as ``...Z``; 3.10's parser only knows ``+00:00``.
    """
    value = value.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class LogSite:
    """One log call in source code, e.g. ``logger.info("user %s logged in", uid)``."""

    path: str  # path relative to the project root, POSIX separators
    line: int  # 1-based
    col: int  # 0-based
    end_line: int
    end_col: int
    level: str  # debug | info | warning | error | critical | exception | log
    template: str | None  # message template, placeholders normalised; None if dynamic
    logger: str  # source text of the receiver, e.g. "logger", "self.log"
    in_except: bool = False
    in_loop: bool = False
    # Inside a bare / ``except Exception`` handler that never raises: the error stops here.
    in_swallowing_except: bool = False
    # In a retry loop (try/except in a loop that sleeps or counts attempts) or @retry function.
    in_retry: bool = False
    # In an except handler whose try block calls an HTTP / DB / service client.
    near_dependency: bool = False
    library: str = "logging"  # logging | structlog | loguru
    # Fields bound to the logger before the call: log.bind(user_id=...).info(...), or a
    # name assigned from logger.bind(...).
    bound_fields: tuple[str, ...] = ()
    exc_opt: bool = False  # loguru: logger.opt(exception=...) attaches the traceback
    # Source text of the values the template's placeholders are filled with, in order:
    # ``log.info("shipping %s to %s", shipment.id, carrier)`` -> ("shipment.id", "carrier").
    args: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def literal_len(self) -> int:
        """Characters of fixed text in the template; used to rank competing matches."""
        if not self.template:
            return 0
        return len(_PLACEHOLDER.sub("", self.template))

    @property
    def matchable(self) -> bool:
        """Enough fixed words to tell this site's messages from anyone else's."""
        if not self.template:
            return False
        fixed = _PLACEHOLDER.sub("", self.template)
        if fixed == self.template:
            return bool(fixed.strip())
        return sum(c.isalpha() for c in fixed) >= MIN_LETTERS

    def template_regex(self) -> re.Pattern[str] | None:
        """Regex that matches a rendered message produced by this site.

        Not anchored at the start, because formatters usually prefix a timestamp/level.
        """
        if not self.matchable:
            return None
        parts: list[str] = []
        pos = 0
        for m in _PLACEHOLDER.finditer(self.template):
            parts.append(re.escape(self.template[pos : m.start()]))
            parts.append("%" if m.group() == "%%" else r".*?")
            pos = m.end()
        parts.append(re.escape(self.template[pos:]))
        return re.compile("".join(parts) + r"\s*$")

    def values(self, message: str) -> dict[str, str]:
        """The values a rendered message was filled with, by the source text of each:
        ``"shipping S-1042 to ups"`` -> ``{"shipment.id": "S-1042", "carrier": "ups"}``.
        Empty when the message doesn't match the template or the arguments aren't known."""
        if not self.matchable or not self.args:
            return {}
        parts: list[str] = []
        pos = n = 0
        for m in _PLACEHOLDER.finditer(self.template):
            parts.append(re.escape(self.template[pos : m.start()]))
            if m.group() == "%%":
                parts.append("%")
            else:
                parts.append(f"(?P<v{n}>.*?)")
                n += 1
            pos = m.end()
        parts.append(re.escape(self.template[pos:]))
        if n != len(self.args):
            return {}
        found = re.search("".join(parts) + r"\s*$", message.split("\n", 1)[0])
        if not found:
            return {}
        return {arg: found[f"v{i}"] for i, arg in enumerate(self.args)}


@dataclass
class LogRecord:
    """A single log event returned by a backend."""

    timestamp: datetime | None
    message: str
    fields: dict[str, Any] = field(default_factory=dict)
    size: int | None = None  # bytes as stored by the log backend, for cost estimates

    @property
    def source_file(self) -> str | None:
        return self._location()[0]

    @property
    def source_line(self) -> int | None:
        return self._location()[1]

    def _location(self) -> tuple[str | None, int | None]:
        """Where the record was logged from, in whichever fields the logger put it."""
        f = self.fields
        nested = f.get("source") if isinstance(f.get("source"), dict) else {}
        file = _first_value(
            f,
            nested,
            # Python logging, OpenTelemetry (code.*), ECS (log.origin.*), logstash-logback-
            # encoder (caller_*), Log4j 2 and Go slog (source.file)
            ("pathname", "filename", "file", "code.filepath", "log.origin.file.name",
             "source_file", "caller_file_name", "source.file"),
        )  # fmt: skip
        line = _first_value(
            f,
            nested,
            ("lineno", "line", "code.lineno", "log.origin.file.line", "source_line",
             "caller_line_number", "source.line"),
        )  # fmt: skip
        for combined in (file, f.get("caller")):  # "checkout.go:42" (zap, zerolog, logrus)
            if isinstance(combined, str):
                m = _FILE_AND_LINE.match(combined)
                if m and (line is None or combined is not file):
                    file, line = m["file"], m["line"]
                    break
        if file is not None:
            file = str(file)
            # Java: "Checkout.java" + class "com.acme.Checkout" -> "com/acme/Checkout.java"
            cls = f.get("caller_class_name") or nested.get("class")
            if isinstance(cls, str) and "/" not in file and "." in cls:
                package = cls.rsplit(".", 1)[0].split("$")[0]
                file = f"{package.replace('.', '/')}/{file}"
        try:
            number = int(line) if line is not None else None
        except (TypeError, ValueError):
            number = None
        return (file or None), number


def _first_value(fields: dict, nested: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = fields.get(key)
        if value is None and key.startswith("source."):
            value = nested.get(key.split(".", 1)[1])
        if value not in (None, ""):
            return value
    return None


@dataclass
class Usage:
    """How much one log call wrote in a time range."""

    count: int = 0
    bytes: int = 0
    estimated: bool = False  # bytes extrapolated from a sample rather than measured


@dataclass(frozen=True)
class Diagnostic:
    """A log-quality finding."""

    code: str  # e.g. "LL001"
    message: str
    path: str
    line: int
    col: int
    end_line: int
    end_col: int
    severity: str = "warning"  # error | warning | info | hint

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.col + 1}: {self.code} {self.message}"
