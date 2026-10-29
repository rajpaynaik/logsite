"""Context fields: which ones a project adds to every log automatically (so LL006 doesn't
ask for them on each call), and how many production records actually carry them.

Recognised: logging filters / record factories that set ``record.<field>``, structlog
``bind_contextvars(...)`` and processors that set ``event_dict["<field>"]``,
loguru ``contextualize(...)`` and ``configure(extra={...})``,
``LoggerAdapter(logger, {...})`` extras, and OpenTelemetry's logging instrumentation.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .index import DEFAULT_EXCLUDES, iter_python_files, parse_python
from .models import LogRecord, LogSite

# Names that mean the same field; satisfying one satisfies the others.
SYNONYMS = [
    {"trace_id", "traceId", "trace.id", "otelTraceID"},
    {"span_id", "spanId", "span.id", "otelSpanID"},
    {"request_id", "requestId", "req_id", "x_request_id", "correlation_id", "correlationId"},
    {"user_id", "userId", "user.id", "uid"},
    {"tenant_id", "tenantId", "tenant", "org_id", "orgId", "account_id"},
]
OTEL_FIELDS = {"otelTraceID", "otelSpanID", "otelServiceName", "otelTraceSampled"}
_RECORD_PARAMS = {"record", "rec", "log_record", "logrecord"}
_EVENT_DICT_PARAMS = {"event_dict", "event", "ed"}


def aliases(field: str) -> set[str]:
    for group in SYNONYMS:
        if field in group:
            return group
    return {field}


def satisfied(field: str, available: Iterable[str]) -> bool:
    return bool(aliases(field) & set(available))


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = node.args
    return {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return f.id if isinstance(f, ast.Name) else ""


def detect_in_source(source: str, path: str = "<string>") -> dict[str, list[str]]:
    """``{field: ["path:line how", ...]}`` for context injected by this file."""
    try:
        tree = parse_python(source, path)
    except SyntaxError:
        return {}
    found: dict[str, list[str]] = {}

    def add(field: str, node: ast.AST, how: str) -> None:
        found.setdefault(field, []).append(f"{path}:{getattr(node, 'lineno', 0)} {how}")

    for node in ast.walk(tree):
        # def filter(self, record): record.request_id = ...  /  processors: event_dict["x"] = ...
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = _params(node)
            record_params = params & _RECORD_PARAMS
            event_params = params & _EVENT_DICT_PARAMS
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
                    targets = [sub.target]
                else:
                    targets = []
                for t in targets:
                    if (
                        isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id in record_params
                    ):
                        add(t.attr, sub, "sets it on every log record")
                    if (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Name)
                        and t.value.id in event_params
                        and isinstance(t.slice, ast.Constant)
                        and isinstance(t.slice.value, str)
                    ):
                        add(t.slice.value, sub, "structlog processor")
                if (
                    isinstance(sub, ast.Call)
                    and _call_name(sub) == "setattr"
                    and len(sub.args) >= 2
                    and isinstance(sub.args[0], ast.Name)
                    and sub.args[0].id in record_params
                    and isinstance(sub.args[1], ast.Constant)
                    and isinstance(sub.args[1].value, str)
                ):
                    add(sub.args[1].value, sub, "sets it on every log record")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        # (.bind(...) is per logger, not global: the index tracks it on each call instead.)
        if name in ("bind_contextvars", "contextualize"):
            lib = "loguru" if name == "contextualize" else "structlog"
            for kw in node.keywords:
                if kw.arg:
                    add(kw.arg, node, f"{lib} {name}()")
        elif name == "configure":  # loguru: logger.configure(extra={"service": ...})
            for kw in node.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                    for k in kw.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            add(k.value, node, "loguru configure(extra=)")
        elif name == "LoggerAdapter" and len(node.args) >= 2 and isinstance(node.args[1], ast.Dict):
            for k in node.args[1].keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    add(k.value, node, "LoggerAdapter extra")
        elif name == "LoggingInstrumentor":
            for field in OTEL_FIELDS:
                add(field, node, "OpenTelemetry logging instrumentation")
    return found


def detect_injected(
    root: str | Path, excludes: Iterable[str] = DEFAULT_EXCLUDES
) -> dict[str, list[str]]:
    """Scan every ``.py`` file under ``root`` for automatically injected context."""
    root = Path(root).resolve()
    found: dict[str, list[str]] = {}
    for file in iter_python_files(root, excludes):
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = file.relative_to(root).as_posix()
        for field, where in detect_in_source(text, rel).items():
            found.setdefault(field, []).extend(where)
    return found


_EMPTY = {"", "-", "null", "none", "nil", "undefined", "unknown"}


def _keys(fields: dict, prefix: str = "") -> Iterable[tuple[str, object]]:
    for key, value in fields.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _keys(value, f"{name}.")
        else:
            yield name, value


def get_field(record: LogRecord, field: str) -> object | None:
    """The value of ``field`` (or a synonym) at any nesting, or None."""
    names = aliases(field)
    for key, value in _keys(record.fields):
        if (key in names or key.rsplit(".", 1)[-1] in names) and value is not None:
            return value
    return None


def mentions(record: LogRecord, field: str, value: str) -> bool:
    """``field`` (or a synonym) equals ``value``; if the record has no such field, ``value``
    appears in the message as a whole word (so id ``ab1`` doesn't match ``ab12``)."""
    found = get_field(record, field)
    if found is not None:
        return str(found) == value
    return re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", record.message) is not None


def has_field(record: LogRecord, field: str) -> bool:
    """Whether the record carries a non-empty ``field`` (or a synonym), at any nesting."""
    names = aliases(field)
    for key, value in _keys(record.fields):
        if (key in names or key.rsplit(".", 1)[-1] in names) and value is not None:
            if not (isinstance(value, str) and value.strip().lower() in _EMPTY):
                return True
    return False


@dataclass
class FieldCoverage:
    field: str
    situation: str  # why it's required: all | error | in_retry | near_dependency
    present: int = 0
    total: int = 0

    @property
    def ratio(self) -> float:
        return self.present / self.total if self.total else 1.0


def measure_coverage(
    sites: Iterable[LogSite],
    samples: Mapping[str, Sequence[LogRecord]],
    required: Callable[[LogSite], list[tuple[str, str]]],
) -> dict[str, list[FieldCoverage]]:
    """``{site.id: [FieldCoverage, ...]}`` for every sampled site that requires fields."""
    out: dict[str, list[FieldCoverage]] = {}
    for site in sites:
        records = samples.get(site.id)
        fields = required(site)
        if not records or not fields:
            continue
        cov = [FieldCoverage(f, why, total=len(records)) for f, why in fields]
        for record in records:
            for c in cov:
                c.present += has_field(record, c.field)
        out[site.id] = cov
    return out


def overall_coverage(per_site: Mapping[str, list[FieldCoverage]]) -> list[FieldCoverage]:
    """Coverage per field across every record that requires it, worst first."""
    totals: dict[str, FieldCoverage] = {}
    for cov in per_site.values():
        for c in cov:
            t = totals.setdefault(c.field, FieldCoverage(c.field, c.situation))
            t.present += c.present
            t.total += c.total
    return sorted(totals.values(), key=lambda c: c.ratio)
