"""Find every log call in a Python codebase using the standard-library ``ast`` module."""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from .models import LogSite

LEVEL_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception"}
_LEVEL_ALIASES = {"warn": "warning", "fatal": "critical", "msg": "info", "trace": "debug",
                  "success": "info"}  # fmt: skip
# Extra level methods per library (structlog also has async a-prefixed variants).
LIBRARY_METHODS = {
    "logging": LEVEL_METHODS,
    "structlog": LEVEL_METHODS | {"msg"} | {f"a{m}" for m in LEVEL_METHODS},
    "loguru": LEVEL_METHODS | {"trace", "success"},
}
# Calls that return a logger with extra context: log.bind(x=1).info(...), logger.opt(...).
WRAPPERS = {"bind", "new", "opt", "patch"}
DEFAULT_EXCLUDES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox",
    "target", "vendor", ".gradle",
}  # fmt: skip


def _receiver_text(node: ast.expr) -> str | None:
    """Return dotted text for Name/Attribute chains (``self.logger``), else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _receiver_text(node.value)
        return f"{inner}.{node.attr}" if inner else None
    if isinstance(node, ast.Call):  # logging.getLogger(__name__).info(...)
        inner = _receiver_text(node.func)
        return f"{inner}()" if inner else None
    return None


def _unwrap(node: ast.expr) -> tuple[ast.expr, list[str], bool]:
    """Strip ``.bind(...)`` / ``.opt(...)`` wrappers: (base, bound field names, opt(exception))."""
    bound: list[str] = []
    exc = False
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WRAPPERS
    ):
        if node.func.attr in ("bind", "new"):
            bound += [k.arg for k in node.keywords if k.arg]
        if node.func.attr == "opt":
            exc = exc or any(
                k.arg == "exception"
                and not (isinstance(k.value, ast.Constant) and not k.value.value)
                for k in node.keywords
            )
        node = node.func.value
    return node, bound, exc


def _root(node: ast.expr) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def library_names(tree: ast.AST) -> dict[str, tuple[str, frozenset[str]]]:
    """Names bound to structlog / loguru loggers in a module: ``{name: (library, bound)}``.

    ``from loguru import logger as lg``, ``log = structlog.get_logger()``,
    ``log = logger.bind(request_id=r)``. Scopes are ignored: a name means the same thing in
    the whole file, which is how logger variables are used in practice.
    """
    names: dict[str, tuple[str, frozenset[str]]] = {"structlog": ("structlog", frozenset())}
    assigns: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "loguru":
            for alias in node.names:
                if alias.name == "logger":
                    names[alias.asname or "logger"] = ("loguru", frozenset())
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("structlog", "loguru"):
                    names[alias.asname or alias.name] = (alias.name, frozenset())
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", None)
                if name:
                    assigns.append((name, node.value))
    for name, value in assigns:  # std = logging.getLogger(__name__), whatever it's called
        func = _receiver_text(value.func) if isinstance(value, ast.Call) else None
        if func and func.rsplit(".", 1)[-1] in ("getLogger", "LoggerAdapter"):
            names.setdefault(name, ("logging", frozenset()))
    for _ in range(3):  # follow short chains: log = base.bind(...); req_log = log.bind(...)
        for name, value in assigns:
            base, bound, _exc = _unwrap(value)
            root = _root(base)
            if root in names:
                lib, already = names[root]
                if root == "structlog" or bound or base is not value or isinstance(base, ast.Name):
                    names[name] = (lib, already | frozenset(bound))
    if "loguru" in names:  # import loguru; loguru.logger.info(...)
        names.setdefault("loguru", ("loguru", frozenset()))
    return names


def _looks_like_logger(receiver: str) -> bool:
    last = receiver.rsplit(".", 1)[-1].lower()
    return "log" in last or receiver.endswith("getLogger()")


def extract_template(node: ast.expr) -> str | None:
    """Turn the message argument of a log call into a template string.

    ``"user %s"`` -> ``"user %s"``; ``f"user {uid}"`` -> ``"user {}"``;
    ``"user {}".format(uid)`` -> ``"user {}"``. Anything else -> None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value.replace("%", "%%"))
            else:
                out.append("{}")
        return "".join(out)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        return re.sub(r"\{[^{}]*\}", "{}", node.func.value.value.replace("%", "%%"))
    return None


_BROAD = {"Exception", "BaseException"}


def _is_broad(node: ast.expr | None) -> bool:
    """``except:``, ``except Exception``, ``except (ValueError, Exception)``..."""
    if node is None:
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_broad(elt) for elt in node.elts)
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    return name in _BROAD


def _raises(handler: ast.ExceptHandler) -> bool:
    """Whether the handler raises (re-raise or translate), ignoring nested functions."""
    stack: list[ast.AST] = list(handler.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


# Calls that talk to another system: by module, by method, or by the receiver's name.
DEPENDENCY_MODULES = {
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "boto3", "botocore", "redis",
    "pymongo", "grpc", "psycopg", "psycopg2", "pymysql", "sqlite3", "sqlalchemy", "socket",
}  # fmt: skip
DEPENDENCY_METHODS = {"execute", "executemany", "urlopen", "fetchone", "fetchall", "fetchmany"}
DEPENDENCY_RECEIVERS = re.compile(
    r"session|client|http|api|conn|cursor|db|engine|stub|gateway|pool|queue|bucket|producer",
    re.IGNORECASE,
)
_RETRY_NAME = re.compile(r"attempt|retr|tries|tri\b", re.IGNORECASE)


def is_dependency_call(name: str) -> bool:
    """``requests.get``, ``cursor.execute``, ``self.payment_client.charge`` ... -> True."""
    parts = name.replace("()", "").split(".")
    if parts[0] in DEPENDENCY_MODULES or parts[-1] in DEPENDENCY_METHODS:
        return True
    return len(parts) >= 2 and bool(DEPENDENCY_RECEIVERS.search(parts[-2]))


def _calls_dependency(nodes: list[ast.stmt]) -> bool:
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                name = _receiver_text(sub.func)
                if name and is_dependency_call(name):
                    return True
    return False


def _is_retry_loop(node: ast.For | ast.AsyncFor | ast.While) -> bool:
    """A loop that catches exceptions and either sleeps or counts attempts."""
    body = [sub for stmt in node.body for sub in ast.walk(stmt)]
    if not any(isinstance(sub, (ast.Try, *_TRY_STAR)) for sub in body):
        return False
    if isinstance(node, (ast.For, ast.AsyncFor)):
        target = _receiver_text(node.target) if isinstance(node.target, ast.Name) else None
        if target and _RETRY_NAME.search(target):
            return True
    names = (_receiver_text(sub.func) or "" for sub in body if isinstance(sub, ast.Call))
    return any(n.rsplit(".", 1)[-1] == "sleep" for n in names)


def _has_retry_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        name = _receiver_text(dec.func if isinstance(dec, ast.Call) else dec) or ""
        if re.search(r"retry|backoff|tenacity", name, re.IGNORECASE):
            return True
    return False


_TRY_STAR = (ast.TryStar,) if hasattr(ast, "TryStar") else ()


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, names: dict[str, tuple[str, frozenset[str]]] | None = None):
        self.path = path
        self.names = names or {}
        self.sites: list[LogSite] = []
        self.calls: list[tuple[LogSite, ast.Call]] = []
        self._except_depth = 0
        self._loop_depth = 0
        self._func_depth = 0
        self._swallowing: list[bool] = []  # one entry per enclosing except handler
        self._dependency: list[bool] = []  # per handler: does its try call a dependency?
        self._retry_depth = 0  # retry loops / @retry functions around the current node

    # Track context so the linter can reason about it later.
    def visit_Try(self, node: ast.Try) -> None:
        calls_dependency = _calls_dependency(node.body)
        for handler in node.handlers:
            handler._logsite_dependency = calls_dependency  # type: ignore[attr-defined]
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_depth += 1
        self._swallowing.append(_is_broad(node.type) and not _raises(node))
        self._dependency.append(getattr(node, "_logsite_dependency", False))
        self.generic_visit(node)
        self._dependency.pop()
        self._swallowing.pop()
        self._except_depth -= 1

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        retry = _is_retry_loop(node)
        self._loop_depth += 1
        self._retry_depth += retry
        self.generic_visit(node)
        self._retry_depth -= retry
        self._loop_depth -= 1

    visit_For = visit_AsyncFor = visit_While = _visit_loop

    def _visit_func(self, node: ast.AST) -> None:
        # A loop outside a function doesn't make a log inside a nested def "in a loop".
        saved = (
            self._loop_depth,
            self._except_depth,
            self._swallowing,
            self._dependency,
            self._retry_depth,
        )
        self._loop_depth = self._except_depth = 0
        self._swallowing, self._dependency = [], []
        is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        self._retry_depth = int(is_def and _has_retry_decorator(node))  # type: ignore[arg-type]
        self.generic_visit(node)
        (
            self._loop_depth,
            self._except_depth,
            self._swallowing,
            self._dependency,
            self._retry_depth,
        ) = saved

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        site = self._as_log_site(node)
        if site:
            self.sites.append(site)
            self.calls.append((site, node))
        self.generic_visit(node)

    def _as_log_site(self, node: ast.Call) -> LogSite | None:
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        base, bound, exc_opt = _unwrap(func.value)
        receiver = _receiver_text(base)
        if receiver is None:
            return None
        known = self.names.get(_root(base) or "")
        library, name_bound = known or ("logging", frozenset())
        if known is None and not _looks_like_logger(receiver):
            return None

        method = func.attr
        if library == "structlog" and method.startswith("a") and method[1:] in LEVEL_METHODS:
            method = method[1:]  # await log.ainfo(...)
        if method in LIBRARY_METHODS[library]:
            level = _LEVEL_ALIASES.get(method, method)
            msg_arg = node.args[0] if node.args else None
        elif func.attr == "log" and len(node.args) >= 2:
            level = "log"
            msg_arg = node.args[1]
        else:
            return None

        if msg_arg is None:
            msg_kw = next((k.value for k in node.keywords if k.arg == "msg"), None)
            msg_arg = msg_kw
        template = extract_template(msg_arg) if msg_arg is not None else None
        if library == "loguru" and template is not None and isinstance(msg_arg, ast.Constant):
            # loguru formats with str.format: "{}" / "{user}" / "{:.2f}" are placeholders.
            template = re.sub(r"\{[^{}]*\}", "{}", template.replace("%", "%%"))
        if library == "structlog" and msg_arg is None:
            msg_kw = next((k.value for k in node.keywords if k.arg == "event"), None)
            template = extract_template(msg_kw) if msg_kw is not None else None

        return LogSite(
            path=self.path,
            line=node.lineno,
            col=node.col_offset,
            end_line=node.end_lineno or node.lineno,
            end_col=node.end_col_offset or node.col_offset,
            level=level,
            template=template,
            logger=receiver,
            in_except=self._except_depth > 0,
            in_loop=self._loop_depth > 0,
            in_swallowing_except=bool(self._swallowing) and self._swallowing[-1],
            in_retry=self._retry_depth > 0,
            near_dependency=bool(self._dependency) and self._dependency[-1],
            library=library,
            bound_fields=tuple(sorted(set(bound) | name_bound)),
            exc_opt=exc_opt,
        )


def index_calls(source: str, path: str = "<string>") -> list[tuple[LogSite, ast.Call]]:
    """Like :func:`index_source` but also returns the AST node of each call."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    visitor = _Visitor(path, library_names(tree))
    visitor.visit(tree)
    return visitor.calls


def index_source(source: str, path: str = "<string>") -> list[LogSite]:
    """Return all log sites in a source string; the language comes from ``path``'s extension
    (Python by default). Python syntax errors yield an empty list."""
    from .lang import MissingParser, language_of
    from .lang import index_calls as index_other

    language = language_of(path)
    if language == "python":
        return [site for site, _ in index_calls(source, path)]
    try:
        return [site for site, _ in index_other(language, source, path)]
    except MissingParser:
        return []


def iter_python_files(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> Iterator[Path]:
    return iter_source_files(root, excludes, (".py",))


def iter_source_files(
    root: Path,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    suffixes: Iterable[str] | None = None,
    tests: bool = False,
) -> Iterator[Path]:
    """Source files under ``root`` in the languages logsite reads (or only ``suffixes``);
    Go's ``_test.go`` files only with ``tests``."""
    from .lang import EXTENSIONS

    wanted = tuple(suffixes or EXTENSIONS)
    excludes = set(excludes)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excludes and not d.startswith(".")]
        for name in filenames:
            if name.endswith(wanted) and (tests or not name.endswith("_test.go")):
                yield Path(dirpath) / name


def index_project(root: str | Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> list[LogSite]:
    """Index every Python, Java and Go file under ``root``."""
    root = Path(root).resolve()
    sites: list[LogSite] = []
    for file in iter_source_files(root, excludes):
        rel = file.relative_to(root).as_posix()
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sites.extend(index_source(text, rel))
    return sites
