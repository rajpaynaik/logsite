"""Reproduce a production error as a failing test.

``logsite repro`` puts together what production says about one error group (the exception
and the line it went through, the function to call, the values the failing request logged,
the dependency that failed) and can write a pytest skeleton from it. ``logsite agent
--expect <id>`` then checks that a test fails the way production did: the same exception,
through the same line of the project. A test that fails for another reason (a mock raising
the error itself, the wrong function called) doesn't count.

Nothing here calls a model: the case is the evidence, the check is deterministic, and
whoever writes the test (a person, or an assistant through the MCP server) uses both.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .backends import LogBackend, TimeRange
from .context import get_field
from .errors import ErrorGroup, history_json
from .leaks import LeakConfig, redact
from .matching import Matcher
from .models import LogRecord, LogSite
from .timeline import SERVICE_FIELDS, text_head
from .tracebacks import LEVEL_FIELDS, TRACEBACK_FIELDS, extract_error

MAX_INPUTS = 30
MAX_REQUEST_LINES = 200
NEAR_LINES = 10  # a production line this close in the same file: the code moved a little

# Fields every record has that say nothing about the request's inputs.
_PLUMBING = {
    "timestamp", "time", "@timestamp", "ts", "asctime", "created", "msecs", "relativecreated",
    "message", "msg", "event", "pathname", "filename", "lineno", "funcname", "module", "logger",
    "name", "logger_name", "thread", "threadname", "process", "processname", "taskname",
    "stack_info", "caller", "source", "levelno", "host", "hostname",
    "trace_id", "traceid", "trace.id", "span_id", "spanid", "span.id", "dd.trace_id", "dd.span_id",
    *LEVEL_FIELDS, *TRACEBACK_FIELDS, *SERVICE_FIELDS,
}  # fmt: skip
_KEY_VALUE = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)=(\"[^\"]*\"|'[^']*'|[^\s,;)\]]+)")


@dataclass
class Input:
    name: str  # the source text of the value ("shipment.id") or the field it was logged as
    value: str  # as logged, masked when it's a secret or personal data
    source: str  # the log call ("app/api.py:22") or "field" / "message"
    masked: bool = False


@dataclass
class Case:
    id: str
    title: str
    count: int
    exc_type: str | None  # the exception the request finally failed with
    raised_at: str | None  # "app/carrier.py:58": the project line the cause went through
    raised_in: str | None
    root_type: str | None  # the root cause, e.g. "httpx.ReadTimeout"
    root_message: str
    origin: str | None  # the library the root cause came out of
    causes: list[str]
    call_path: list[str]
    frames: list[str]  # "path:line" of the project's frames, outermost first
    first_seen: str | None = None
    last_seen: str | None = None
    entry: dict | None = None  # {"at", "function", "class", "params", "init_params", "module"}
    fake: dict | None = None  # {"at", "call", "target", "raise"}
    logged_at: str | None = None
    # where raised_at's statement is in the code here, when it moved since production ran
    # (found by the source text the traceback shows for it)
    local_at: str | None = None
    request: dict = field(default_factory=dict)  # {"field", "value", "lines"}
    inputs: list[Input] = field(default_factory=list)
    history: dict | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def python(self) -> bool:
        return bool(self.raised_at and self.raised_at.split(":", 1)[0].endswith(".py"))


# --- building the case ---------------------------------------------------------------------------


def pick_group(groups: list[ErrorGroup], wanted: str | None) -> ErrorGroup:
    """The group ``wanted`` names (its id, an id prefix, or its number in the list); the
    first one when nothing is named. Raises ValueError with a user-facing message."""
    if not groups:
        raise ValueError("no errors in these logs")
    if not wanted:
        return groups[0]
    if wanted.isdigit() and 1 <= int(wanted) <= len(groups) and len(wanted) < 4:
        return groups[int(wanted) - 1]
    found = [g for g in groups if g.fingerprint.startswith(wanted.lower())]
    if len(found) == 1:
        return found[0]
    if found:
        raise ValueError(f"{wanted} matches {len(found)} groups; give more of the id")
    ids = ", ".join(g.id for g in groups[:10])
    raise ValueError(f"no error group {wanted} in these logs (groups: {ids})")


def find_case(
    cfg,
    wanted: str | None,
    log_file: str | None = None,
    backend: LogBackend | None = None,
    hours: float | None = None,
    limit: int = 5000,
) -> tuple[Case, int]:
    """Read the errors (from ``log_file``, or ``backend`` over the last ``hours``), pick the
    group ``wanted`` names and build its case. Returns ``(case, other groups)``; raises
    ValueError with a user-facing message."""
    from datetime import datetime, timedelta, timezone

    from .backends.file import FileBackend
    from .errors import AppFiles, summarize_errors
    from .index import DEFAULT_EXCLUDES, index_project

    if log_file:
        if not Path(log_file).is_file():
            raise ValueError(f"no such file: {log_file}")
        backend = FileBackend(log_file)
        now = datetime.now(timezone.utc)
        window = TimeRange(datetime(1970, 1, 1, tzinfo=timezone.utc), now + timedelta(days=1))
    elif backend is None:
        raise ValueError("no [backend] configured: give a log file instead")
    else:
        window = TimeRange.last(hours or cfg.window_hours)
    excludes = DEFAULT_EXCLUDES | set(cfg.excludes)
    sites = index_project(cfg.root, excludes)
    app = AppFiles.scan(cfg.root, excludes)
    fields = dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"])
    try:
        summary = summarize_errors(
            backend, sites, app, window, None, cfg.lint, cfg.leaks, limit, fields
        )
    except Exception as exc:
        raise ValueError(f"couldn't read the {backend.name} logs: {exc}") from exc
    if not log_file:  # a downloaded file is not the production window: leave history be
        from . import history

        history.apply(cfg, summary.groups, window.start, window.end, record=False)
    group = pick_group(summary.groups, wanted)
    case = build_case(group, cfg.root, sites, backend, window, cfg.timeline.field_name, cfg.leaks)
    return case, len(summary.groups) - 1


def save_pasted(root: Path, text: str) -> Path:
    d = cases_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "pasted.log"
    path.write_text(text, encoding="utf-8")
    return path


def build_case(
    group: ErrorGroup,
    root: Path,
    sites: Iterable[LogSite],
    backend: LogBackend | None,
    window: TimeRange | None,
    trace_field: str = "trace_id",
    leaks: LeakConfig | None = None,
) -> Case:
    sites = list(sites)
    info = extract_error(group.record) if group.record is not None else None
    root_exc = info.chain[0] if info else None
    case = Case(
        id=group.id,
        title=group.title,
        count=group.count,
        exc_type=group.exc_type,
        raised_at=group.raised_at,
        raised_in=group.raised_in,
        root_type=root_exc.type if root_exc else None,
        root_message=redact(root_exc.message, leaks) if root_exc else "",
        origin=group.origin,
        causes=list(group.causes),
        call_path=list(group.path),
        frames=list(group.frames_at),
        first_seen=group.first_seen.isoformat() if group.first_seen else None,
        last_seen=group.last_seen.isoformat() if group.last_seen else None,
        logged_at=group.logged_at.id if group.logged_at else None,
        history=history_json(group.history),
    )
    records, case.request = _request(group, backend, window, trace_field, sites, leaks)
    case.inputs = inputs_from(_until(records, group.record), sites, leaks)
    if case.python:
        code = {(Path(f.file).name, f.line): f.code for e in (info.chain if info else [])
                for f in e.frames if f.code}  # fmt: skip
        here = [_locate(root, at, code) for at in case.frames]
        if case.raised_at:
            case.local_at = _locate(root, case.raised_at, code)
            if case.local_at == case.raised_at:
                case.local_at = None
        case.entry = _entry(root, here, case.inputs)
        case.fake = _fake(root, case)
    case.missing = _missing(case, group)
    return case


def _request(
    group: ErrorGroup,
    backend: LogBackend | None,
    window: TimeRange | None,
    trace_field: str,
    sites: list[LogSite],
    leaks: LeakConfig | None,
) -> tuple[list[LogRecord], dict]:
    """The failing request's log lines: every line with the group's example trace id, or
    the error line alone when there's no id to follow."""
    record = group.record
    ids = group.sample.get("ids") or {}
    name, value = next(
        ((k, v) for k in (trace_field, "trace_id", "request_id") if (v := ids.get(k))),
        (None, None),
    )
    records = [record] if record is not None else []
    if name and value and backend is not None and window is not None:
        try:
            found = backend.find_records(name, value, window, limit=MAX_REQUEST_LINES)
        except Exception:  # the error line alone still makes a case
            found = []
        if found:
            records = sorted(found, key=lambda r: (r.timestamp is None, r.timestamp))
            if record is not None and not any(r is record or r.message == record.message
                                              for r in records):  # fmt: skip
                records.append(record)
    matcher = Matcher(sites)
    lines = []
    for r in records:
        site = matcher.match(r)
        level = next((str(v) for k in LEVEL_FIELDS if (v := get_field(r, k))), None)
        lines.append({
            "time": r.timestamp.isoformat() if r.timestamp else None,
            "level": (level or text_head(r)[0] or (site.level if site else "") or "").upper(),
            "message": redact(r.message.split("\n", 1)[0], leaks)[:300],
            "site": site.id if site else None,
        })  # fmt: skip
    return records, {"field": name, "value": value, "lines": lines}


def _until(records: list[LogRecord], error: LogRecord | None) -> list[LogRecord]:
    """The request's lines up to and including the error: what it logged afterwards
    (cleanup, a retry of something else) isn't what it failed with."""
    for n, r in enumerate(records):
        if error is not None and (r is error or (r.message == error.message
                                                 and r.timestamp == error.timestamp)):  # fmt: skip
            return records[: n + 1]
    return records


def _literal(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def inputs_from(
    records: Iterable[LogRecord], sites: Iterable[LogSite], leaks: LeakConfig | None = None
) -> list[Input]:
    """The values a request logged on its way to the error: what its log calls were filled
    with (by the code that filled them), its structured fields, and ``key=value`` pairs in
    messages that matched no log call. In the order first seen, each with the last value
    logged (a feed that fails on its third row logged that row last)."""
    matcher = Matcher(sites)
    out: dict[str, Input] = {}

    def add(name: str, value: object, source: str) -> None:
        if (name not in out and len(out) >= MAX_INPUTS) or isinstance(value, (dict, list)):
            return
        raw = _literal(value)
        if not raw or len(raw) > 200:
            return
        shown = redact(raw, leaks)
        if name in out and out[name].value == shown:
            return  # the same value again: keep where it was first logged
        out[name] = Input(name, shown, source, masked=shown != raw)  # a new value wins

    for r in records:
        site = matcher.match(r)
        values = site.values(r.message) if site else {}
        for expr, value in values.items():
            add(expr, value, site.id)
        for key, value in r.fields.items():
            if key.lower() not in _PLUMBING and value is not None:
                add(key, value, "field")
        if not values:
            for key, value in _KEY_VALUE.findall(r.message.split("\n", 1)[0]):
                add(key, value.strip("\"'"), site.id if site else "message")
    return list(out.values())


# --- reading the code ------------------------------------------------------------------------


def _parse(root: Path, path: str) -> ast.Module | None:
    try:
        return ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _enclosing(tree: ast.Module, line: int) -> tuple[ast.FunctionDef | None, ast.ClassDef | None]:
    """The innermost function around ``line`` and the class it's a method of."""
    best: tuple = (None, None)

    def walk(node: ast.AST, cls: ast.ClassDef | None) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    best = (child, cls)
                    walk(child, None)
            elif isinstance(child, ast.ClassDef):
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    walk(child, child)
            else:
                walk(child, cls)

    walk(tree, None)
    return best


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef | None, method: bool) -> list[str]:
    if fn is None:
        return []
    a = fn.args
    names = [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return names[1:] if method and names[:1] in (["self"], ["cls"]) else names


def module_of(path: str) -> str:
    """``src/app/api.py`` -> ``app.api``: what a test imports it as."""
    parts = Path(path).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _entry(root: Path, frames: list[str], inputs: list[Input]) -> dict | None:
    """What the test calls: the outermost function of the project on the error's path whose
    arguments the logs give (``handle(route, body)`` with no ``body`` logged is no use when
    ``normalize_isbn(row["isbn"])`` below it had ``row["isbn"]`` logged), else the outermost."""
    candidates: list[dict] = []
    caller: tuple[ast.Module, int] | None = None
    for at in frames:
        path, _, line = at.rpartition(":")
        tree = _parse(root, path)
        if tree is None or not line.isdigit():
            caller = None
            continue
        fn, cls = _enclosing(tree, int(line))
        if fn is not None and not fn.name.startswith("test"):
            init = next(
                (n for n in (cls.body if cls else []) if isinstance(n, ast.FunctionDef)
                 and n.name == "__init__"),
                None,
            )  # fmt: skip
            params = _params(fn, cls is not None)
            candidates.append({
                "at": f"{path}:{fn.lineno}",
                "module": module_of(path),
                "function": fn.name,
                "class": cls.name if cls else None,
                "params": params,
                "init_params": _params(init, True) if cls else [],
                "is_async": isinstance(fn, ast.AsyncFunctionDef),
                # what the caller passed for each parameter: "raw" <- "row['isbn']"
                "args": _call_site(*caller, fn.name, params) if caller else {},
                "_required": _required(fn, cls is not None)
                + (_required(init, True) if cls and init else []),
            })  # fmt: skip
        caller = (tree, int(line))
    for c in candidates:
        if all(_argument(p, inputs, c["args"].get(p))[1].startswith(("logged", "the "))
               for p in c["_required"]):  # fmt: skip
            break
    else:
        c = candidates[0] if candidates else None
    for x in candidates:
        x.pop("_required")
    return c


def _required(fn: ast.FunctionDef | ast.AsyncFunctionDef | None, method: bool) -> list[str]:
    """The parameters without a default."""
    if fn is None:
        return []
    a = fn.args
    positional = [*a.posonlyargs, *a.args]
    required = positional[: len(positional) - len(a.defaults)]
    names = [p.arg for p in required]
    names += [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None]
    return names[1:] if method and names[:1] in (["self"], ["cls"]) else names


def _call_site(tree: ast.Module, line: int, name: str, params: list[str]) -> dict[str, str]:
    """The expressions the call to ``name`` on ``line`` passes for each parameter."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.lineno <= line <= (node.end_lineno or 0):
            continue
        func = node.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called != name:
            continue
        out = {}
        for param, arg in zip(params, node.args, strict=False):
            if isinstance(arg, ast.Starred):
                break
            out[param] = ast.unparse(arg)
        for kw in node.keywords:
            if kw.arg in params:
                out[kw.arg] = ast.unparse(kw.value)
        return out
    return {}


def _locate(root: Path, at: str, code: dict[tuple[str, int], str]) -> str:
    """``at`` ("path:line", as production ran it) in the code here: the same line when it
    still holds the statement the traceback showed, else the nearest line that does."""
    path, _, line = at.rpartition(":")
    shown = code.get((Path(path).name, int(line))) if line.isdigit() else None
    if not shown:
        return at
    try:
        lines = (root / path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return at
    n = int(line)
    want = shown.strip()
    for distance in range(0, 200):
        for k in {n - distance, n + distance}:
            if 1 <= k <= len(lines) and lines[k - 1].strip() == want:
                return f"{path}:{k}"
    return at


def _fake(root: Path, case: Case) -> dict | None:
    """When the cause came out of a library, the call at ``raised_at`` that reached it and
    what to patch so it fails the same way."""
    if not case.origin or not case.raised_at or not case.root_type:
        return None
    path, _, line = (case.local_at or case.raised_at).rpartition(":")
    tree = _parse(root, path)
    if tree is None or not line.isdigit():
        return None
    n = int(line)
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Call)
             and c.lineno <= n <= (c.end_lineno or c.lineno)]  # fmt: skip
    if not calls:
        return None
    # the call whose own name is on the line (not the arguments' calls), outermost first
    call = min(calls, key=lambda c: (c.func.end_lineno != n, c.lineno, c.col_offset)) \
        if any(c.func.end_lineno == n for c in calls) else calls[0]  # fmt: skip
    text = ast.unparse(call.func)
    message = case.root_message.replace("\\", "\\\\").replace('"', '\\"')
    return {
        "at": case.local_at or case.raised_at,
        "call": text,
        "target": _patch_target(tree, call.func, module_of(path)),
        "raise": f'{case.root_type}("{message}")',
    }


def _patch_target(tree: ast.Module, func: ast.expr, module: str) -> str | None:
    """What ``mock.patch`` needs for the call, or None when it can't be told from the file:

    - ``requests.post(...)`` / ``urllib.request.urlopen(...)``: the module attribute;
    - ``urlopen(...)`` after ``from urllib.request import urlopen``: the name where it's
      used (``app.supplier.urlopen``), since patching the original wouldn't reach it;
    - ``self.client.post(...)``: the class ``self.client`` was made from
      (``self.client = httpx.Client()`` -> ``httpx.Client.post``).
    """
    imported = _imported(tree)
    if isinstance(func, ast.Name):
        return f"{module}.{func.id}" if func.id in imported else None
    if not isinstance(func, ast.Attribute):
        return None
    owner = _dotted(func.value)
    if owner is not None:
        head, _, rest = owner.partition(".")
        if head in imported:
            return f"{imported[head]}{'.' + rest if rest else ''}.{func.attr}"
    owner_text = ast.unparse(func.value)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(ast.unparse(t) == owner_text for t in targets):
            continue
        made = _dotted(node.value.func) if isinstance(node.value, ast.Call) else None
        if made is None or not made.rsplit(".", 1)[-1][:1].isupper():
            continue  # not a class: a factory (build_opener()) says nothing about the type
        head, _, rest = made.partition(".")
        if head in imported:
            return f"{imported[head]}{'.' + rest if rest else ''}.{func.attr}"
    return None


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a plain chain of names, else None (``self.x`` counts: ``self`` is a name
    no import binds)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _imported(tree: ast.Module) -> dict[str, str]:
    """Names bound by imports to the dotted thing they stand for."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _missing(case: Case, group: ErrorGroup) -> list[str]:
    out = []
    if case.exc_type is None:
        site = case.logged_at or "the log call"
        out.append(f"no stack trace was logged: log it with exc_info / logger.exception at {site}"
                   " so the test knows which line to fail through")  # fmt: skip
    if not case.request.get("value"):
        out.append("no trace or request id on the error line: inputs are from that line only")
    if not case.inputs:
        where = case.entry["at"] if case.entry else (case.logged_at or "the entry point")
        out.append(f"the failing request logged no values: log its ids and inputs near {where}")
    elif any(i.masked for i in case.inputs):
        out.append("some values were masked as secrets or personal data: use made-up ones")
    if case.python and case.origin and case.fake and not case.fake.get("target"):
        out.append(f"couldn't tell what {case.fake['call']} is: patch it by hand")
    return out


# --- the test skeleton -----------------------------------------------------------------------


def _python_value(value: str) -> str:
    if re.fullmatch(r"-?\d+", value) and not (value.startswith("0") and len(value) > 1):
        return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        return value
    if value in ("True", "False", "None", "true", "false", "null"):
        return {"true": "True", "false": "False", "null": "None"}.get(value, value)
    return json.dumps(value)  # a valid Python literal, double-quoted


def _argument(param: str, inputs: list[Input], passed: str | None = None) -> tuple[str, str]:
    """``(value, comment)`` for one parameter, from the inputs the request logged: by the
    parameter's name, or by what the caller passed for it (``passed``, e.g. ``row['isbn']``)."""
    exact = [i for i in inputs if i.name in (param, param.replace("_", "."))
             or i.name.rsplit(".", 1)[-1] == param or (passed and i.name == passed)]  # fmt: skip
    if exact:
        i = exact[0]
        where = f"logged at {i.source}" if ":" in i.source else f"the {i.name} field"
        if i.masked:
            return "None", f"TODO: masked in the logs ({where}); use a made-up value"
        return _python_value(i.value), where
    parts = [i for i in inputs if i.name.split(".", 1)[0] == param]
    if parts:
        seen = ", ".join(f"{i.name}={i.value}" for i in parts[:3])
        return "None", f"TODO: build one; production logged {seen}"
    return "None", "TODO: not in the logs"


def skeleton(case: Case) -> str:
    """A pytest test that fails the way production did until the bug is fixed."""
    e = case.entry
    if e is None:
        raise ValueError("couldn't find a function of the project to call on the error's path")
    imports = {"import pytest"} if e["is_async"] else set()
    fake = case.fake if case.fake and case.fake.get("target") else None
    if fake:
        imports.add("from unittest import mock")
    for name in (case.root_type if fake else None,):
        if name and "." in name:
            imports.add(f"import {name.rsplit('.', 1)[0]}")
    target = e["class"] or e["function"]
    lines = sorted(i for i in imports if i.startswith("import")) + sorted(
        i for i in imports if i.startswith("from")
    )
    lines += ["", f"from {e['module']} import {target}", "", ""]
    if e["is_async"]:
        lines.append("@pytest.mark.asyncio  # or anyio, as the project's other tests do")
    name = f"test_repro_{case.id}"
    lines.append(f"{'async ' if e['is_async'] else ''}def {name}(caplog):")
    seen = f", {case.first_seen[:16].replace('T', ' ')} UTC" if case.first_seen else ""
    title = case.title if len(case.title) <= 90 else case.title[:89] + "…"
    lines.append(f'    """Production: {title} ({case.count}x{seen}).')
    if case.raised_at:
        lines.append(f"    Raised through {case.raised_at}. Fails until that's fixed.")
    lines.append('    """')
    body = "    "
    if fake:
        lines.append(f"    # {fake['at']} calls {fake['call']}(...): make it fail as in production")
        lines.append(f'    with mock.patch("{fake["target"]}", side_effect={fake["raise"]}):')
        body = "        "
    call = target
    if e["class"]:
        args = _call_args(e["init_params"], case.inputs, body + "    ")
        lines.append(f"{body}obj = {e['class']}(" + (f"\n{args}\n{body})" if args else ")"))
        call = f"obj.{e['function']}"
    args = _call_args(e["params"], case.inputs, body + "    ", e.get("args"))
    awaited = "await " if e["is_async"] else ""
    lines.append(f"{body}{awaited}{call}(" + (f"\n{args}\n{body})" if args else ")"))
    lines += [
        "    # Once it's fixed the code goes further: fake what it calls next (a service, the",
        "    # database) so the test doesn't need them.",
        "    # If the code logs the error instead of raising it, fail with what it logged.",
        "    for record in caplog.records:",
        "        if record.exc_info and record.exc_info[1] is not None:",
        "            raise record.exc_info[1]",
        "",
    ]
    return "\n".join(lines)


def _call_args(
    params: list[str], inputs: list[Input], indent: str, passed: dict[str, str] | None = None
) -> str:
    out = []
    for p in params:
        value, comment = _argument(p, inputs, (passed or {}).get(p))
        out.append(f"{indent}{p}={value},  # {comment}")
    return "\n".join(out)


def skeleton_path(root: Path, case: Case) -> Path:
    tests = root / "tests"
    return (tests if tests.is_dir() else root) / f"test_repro_{case.id}.py"


# --- saving, and checking a test run -------------------------------------------------------------


def case_json(case: Case) -> dict:
    return {
        "id": case.id,
        "title": case.title,
        "count": case.count,
        "first_seen": case.first_seen,
        "last_seen": case.last_seen,
        "signature": {
            "exc_type": case.exc_type,
            "raised_at": case.raised_at,
            "raised_in": case.raised_in,
            "local_at": case.local_at,
        },
        "root_cause": {
            "type": case.root_type,
            "message": case.root_message,
            "origin": case.origin,
        },  # fmt: skip
        "causes": case.causes,
        "call_path": case.call_path,
        "frames": case.frames,
        "entry": case.entry,
        "fake": case.fake,
        "logged_at": case.logged_at,
        "request": case.request,
        "inputs": [vars(i) for i in case.inputs],
        "history": case.history,
        "missing": case.missing,
    }


def cases_dir(root: Path) -> Path:
    from .agent import state_dir

    key = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]
    return state_dir().parent / "repro" / key


def save_case(root: Path, data: dict) -> Path | None:
    d = cases_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{data['id']}.json"
        path.write_text(json.dumps(data, indent=2))
        return path
    except OSError:
        return None


def load_case(root: Path, wanted: str) -> dict:
    """A saved case by id (or prefix), or a case JSON file. Raises ValueError."""
    p = Path(wanted)
    if p.suffix == ".json" and p.is_file():
        return json.loads(p.read_text())
    d = cases_dir(root)
    found = sorted(d.glob(f"{wanted.lower()}*.json")) if d.is_dir() else []
    if len(found) == 1:
        return json.loads(found[0].read_text())
    if found:
        raise ValueError(f"{wanted} matches {len(found)} saved cases; give more of the id")
    raise ValueError(f"no saved case {wanted} for this project: run logsite repro first")


def _short(exc_type: str | None) -> str:
    return (exc_type or "").rsplit(".", 1)[-1]


@dataclass
class Verdict:
    status: str  # reproduced | wrong_failure | passed | no_result
    text: str
    test: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "reproduced"


def check(data: dict, run) -> Verdict:
    """Does the run fail the way production did? ``run`` is an :class:`agent.Run`."""
    sig = data["signature"]
    want = _short(sig["exc_type"])
    root = _short(data["root_cause"]["type"])
    at = sig.get("local_at") or sig["raised_at"]  # the line here that production went through
    production = f"{sig['exc_type'] or data['title']}" + (f" through {at}" if at else "")
    if sig.get("local_at"):
        production += f" ({sig['raised_at']} in production)"
    failures = [f for c in run.causes for f in c.failures]
    if not failures:
        if run.exit_code == 0:
            return Verdict("passed", f"didn't fail: production raised {production}")
        return Verdict("no_result", "the command failed before any test ran: see its output")

    def score(f) -> tuple[int, int]:
        got = _short(f.exc_type)
        same_type = 2 if got == want else 1 if got and got == root else 0
        through = 2 if at and at in f.locations else 1 if at and _near(at, f.locations) else 0
        return same_type, through

    best = max(failures, key=score)
    same_type, through = score(best)
    test = best.test
    got = best.exc_type or "a failure"
    got_at = f" at {best.raised_at}" if best.raised_at else ""
    if not at and same_type == 2:
        return Verdict("reproduced", f"reproduced: {got}, as in production (no stack trace "
                       "was logged, so the line can't be checked)", test)  # fmt: skip
    if same_type and through:
        notes = []
        if same_type == 1:
            notes.append(
                f"it raised the root cause {got}; production's caller turned it into "
                f"{sig['exc_type']}: call the entry point to go the whole way"
            )
        if through == 1:
            notes.append(f"through a line near {at}, not {at} itself: the code here differs "
                         "from what production ran, or the test takes another branch")  # fmt: skip
        return Verdict("reproduced", f"reproduced: {got} through {at}, as in production", test,
                       notes)  # fmt: skip
    if same_type:
        why = f"the same exception, but it never went through {at}: it was raised{got_at}"
        if best.raised_at and best.test_file and best.raised_at.startswith(best.test_file):
            why += " (in the test itself: a mock raising it, or the wrong function called)"
        elif best.raised_at in data["frames"]:
            why += f" (on production's path, before {at}: something there is faked, so the code "
            why += "that failed in production never ran)"
        return Verdict("wrong_failure", f"wrong failure: {why}; production raised {production}",
                       test)  # fmt: skip
    return Verdict("wrong_failure", f"wrong failure: the test raised {got}{got_at}; "
                   f"production raised {production}", test)  # fmt: skip


def _near(at: str, locations: list[str]) -> bool:
    path, _, line = at.rpartition(":")
    for loc in locations:
        p, _, n = loc.rpartition(":")
        if p == path and n.isdigit() and line.isdigit() and abs(int(n) - int(line)) <= NEAR_LINES:
            return True
    return False


def verdict_json(v: Verdict) -> dict:
    return {"status": v.status, "text": v.text, "test": v.test, "notes": v.notes}


# --- text output -------------------------------------------------------------------------------


def render(data: dict, pal, test_file: str | None = None, todo: bool = False) -> list[str]:
    """The case as the terminal shows it."""
    sig, rc = data["signature"], data["root_cause"]
    head = f"Reproduce {data['id']} · {data['title']} · {data['count']:,} error(s)"
    out = [pal.bold(head)]

    def row(label: str, text: str) -> None:
        out.append(f"  {label:<12} {text}")

    def more(text: str) -> None:
        out.append(f"  {'':<12} {text}")

    if sig["exc_type"]:
        where = f" through {pal.yours(sig['raised_at'])}" if sig["raised_at"] else ""
        if sig.get("local_at"):
            line = sig["local_at"].rpartition(":")[2]
            where = (
                f" through {pal.yours(sig['local_at'])} (line {sig['raised_at'].rpartition(':')[2]}"
            )
            where += f" in production: the code moved to line {line} here)"
        fn = f" ({sig['raised_in']})" if sig["raised_in"] else ""
        row("Fail with", f"{sig['exc_type']}{where}{fn}")
    if rc["type"] and _short(rc["type"]) != _short(sig["exc_type"]):
        origin = f"  ({rc['origin']})" if rc["origin"] else ""
        row("Root cause", f"{rc['type']}: {rc['message']}"[:140] + origin)
    if data["call_path"]:
        row("Call path", " › ".join(data["call_path"]))
    e = data.get("entry")
    if e:
        call = f"{e['class']}(…).{e['function']}" if e["class"] else e["function"]
        row("Call", f"{call}({', '.join(e['params'])})  from {e['module']}  {e['at']}")
    f = data.get("fake")
    if f:
        what = f"patch {f['target']}" if f.get("target") else "make it"
        exc = (data["root_cause"]["type"] or "").rsplit(".", 1)[-1]
        row("Fake", f"{f['at']} calls {f['call']}(…): {what} raise {exc}")
    inputs = data["inputs"]
    if inputs:
        width = min(max(len(i["name"]) for i in inputs), 28)
        for n, i in enumerate(inputs[:12]):
            src = f"logged at {i['source']}" if ":" in i["source"] else i["source"]
            line = f"{i['name']:<{width}} = {i['value'][:40]:<20}  {pal.dim(src)}"
            (row("Inputs", line) if n == 0 else more(line))
        if len(inputs) > 12:
            more(pal.dim(f"… {len(inputs) - 12} more (--format json)"))
    req = data["request"]
    if req.get("value") and req["lines"]:
        row("Request", f"{req['field']}={req['value']}: {len(req['lines'])} line(s)")
        for ln in req["lines"][:8]:
            t = (ln["time"] or "")[11:19]
            site = f"  {pal.dim(ln['site'])}" if ln["site"] else ""
            more(f"{t} {ln['level']:<5} {ln['message'][:80]}{site}")
    h = data.get("history")
    if h and h.get("fixed_by"):
        fixes = ", ".join(c.get("sha", "")[:8] + " " + c.get("summary", "") for c in h["fixed_by"])
        row("Fixed before", fixes[:140])
    for n, m in enumerate(data["missing"]):
        (row("Missing", m) if n == 0 else more(m))
    test = test_file or f"tests/test_repro_{data['id']}.py"
    if not test_file:
        row("Next", f"logsite repro {data['id']} --write-test")
    elif todo:
        row("Next", f"fill in the TODOs in {test_file}, then")
    else:
        row("Next", f"run {test_file} as it is:")
    more(f"logsite agent --expect {data['id']} -- pytest {test}")
    return out
