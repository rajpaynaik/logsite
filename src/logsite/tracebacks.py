"""Find and parse the Python exception in a log record, and tell error records apart.

Tracebacks reach log stores in many shapes:

- text in a field: python-json-logger's ``exc_info``, ECS ``error.stack_trace``,
  OpenTelemetry ``exception.stacktrace``, loguru's serialized ``text``...
- structlog's ``dict_tracebacks``: a list of ``{"exc_type", "exc_value", "frames"}``
- type and message fields without a stack (``error.type`` + ``error.message``)
- a plain-text ``Traceback (most recent call last):`` block in the message itself
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .context import _keys
from .models import LogRecord

# Where log stores and shippers usually put the level.
LEVEL_FIELDS = ("level", "levelname", "severity", "log.level", "level.name")
ERROR_WORDS = {"error", "err", "critical", "fatal", "exception", "crit", "alert", "emergency"}
WARNING_WORDS = {"warning", "warn"}

TRACEBACK_FIELDS = (
    "exc_info", "exc_text", "exception", "stack_trace", "stacktrace", "error.stack_trace",
    "exception.stacktrace", "error.stack", "traceback", "stack",
)  # fmt: skip
TYPE_FIELDS = ("exc_type", "exception.type", "error.type", "error.kind")
MESSAGE_FIELDS = ("exc_message", "exception.message", "error.message")

TRACEBACK_START = "Traceback (most recent call last):"
TRACEBACK_HEAD = "Traceback (most recent call last)"  # IPython and Jupyter leave out the colon
CHAIN_MARKERS = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)
_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>.+))?\s*$')
_EXC_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<msg>.*))?$")
_JDK_PACKAGES = ("java/", "javax/", "jdk/", "sun/", "com/sun/", "kotlin/", "scala/")
# Java: "\tat com.acme.Checkout.charge(Checkout.java:42)", "at java.base/java.lang.Thread.run(...)"
_JAVA_FRAME = re.compile(
    r"^\s*at\s+(?:[\w.$@-]+/)?(?P<cls>[\w$.]+)\.(?P<method>[\w$<>-]+)\((?P<src>[^)]*)\)"
)
_JAVA_EXC = re.compile(
    r"^(?:Exception in thread \"[^\"]*\" )?(?:Caused by: )?"
    r"(?P<type>(?:[a-z_$][\w$]*\.)+[A-Z][\w$]*)(?::\s?(?P<msg>.*))?$"
)
# Go: "panic: ...", then per frame "pkg.Func(args)" and "\t/path/file.go:42 +0x1d".
# "panic: ..." and the runtime's "fatal error: concurrent map writes" (not recoverable)
_GO_PANIC = re.compile(r"^(?P<kind>panic|fatal error): (?P<msg>.*?)(?: \[recovered[^\]]*\])?$")
_GO_FILE = re.compile(r"^\t(?P<file>\S+\.go):(?P<line>\d+)(?: \+0x[0-9a-f]+)?$")
# IPython 8+: "File ~/app/x.py:52, in wrapper(registry, *args)", "Cell In[4], line 1"
_IPY_FILE = re.compile(r"^File (?P<file>.+?):(?P<line>\d+), in (?P<func>[^(]+)")
_IPY_CELL = re.compile(
    r"^(?:Cell|Input) In ?\[\d*\](?:, line (?P<line>\d+))?(?:, in (?P<func>[^(]+))?"
)
# older IPython: "/srv/app/x.py in wrapper(registry)", "<ipython-input-3-ab12> in <module>()"
_IPY_OLD = re.compile(r"^(?P<file>(?:[/~<]|[A-Za-z]:\\)\S*) in (?P<func>[^(\s]+)")
# the line it ran: "---> 52 return x", "-> 1701         other = Unit(other)"
_IPY_ARROW = re.compile(r"^-*> ?(?P<line>\d+)\s?(?P<code>.*)$")
# Django's debug page ends "Exception Type: ValueError at /sitemap.xml", "Exception Value: ..."
_DJANGO_TYPE = re.compile(r"^Exception Type: (?P<type>[\w.]+)(?: at \S*)?$")
_DJANGO_VALUE = "Exception Value:"
_ANSI = re.compile(r"\x1b\[[\d;]*m")
_WRAP_WIDTH = 70  # a frame line at least this long may have been hard-wrapped by a terminal
_TEXT_LEVEL = re.compile(r"\b(ERROR|CRITICAL|FATAL|EXCEPTION)\b")
_LIBRARY_PARTS = ("site-packages", "dist-packages", "node_modules")


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    function: str
    code: str | None = None

    @property
    def is_library(self) -> bool:
        """Installed packages and the standard library, never the project's own code."""
        path = self.file.replace("\\", "/")
        return (
            any(f"/{p}/" in f"/{path}" for p in _LIBRARY_PARTS)
            or path.startswith(("<", "node:"))  # <frozen runpy>, <string>, node:internal/...
            or re.search(r"/lib/python\d", path) is not None
            or path.startswith(_JDK_PACKAGES)  # Java frames carry the package as the path
            or "/pkg/mod/" in path  # Go modules
            or re.search(r"/go(?:/[\d.]+)?(?:/libexec)?/src/", path) is not None  # GOROOT
        )

    @property
    def library(self) -> str | None:
        """Where a frame from outside the project comes from: ``httpx`` (Python),
        ``org.postgresql`` (Java), ``github.com/lib/pq`` or ``net/http`` (Go)."""
        path = self.file.replace("\\", "/")
        for p in _LIBRARY_PARTS:
            if f"/{p}/" in f"/{path}":
                rest = f"/{path}".split(f"/{p}/", 1)[1]
                parts = rest.split("/")
                if parts[0].startswith("@") and len(parts) > 1:  # @scope/package
                    return f"{parts[0]}/{parts[1]}"
                return parts[0].removesuffix(".py")
        if path.startswith("node:"):
            return "node"
        m = re.search(r"/lib/python[\d.]+/([^/]+)", path)
        if m:
            return m.group(1).removesuffix(".py")
        if path.endswith(".java"):
            return ".".join(path.split("/")[:-1][:2]) or None
        m = re.search(r"/pkg/mod/(.+?)@", path)
        if m:
            return m.group(1)
        m = re.search(r"/go(?:/[\d.]+)?(?:/libexec)?/src/(.+)/[^/]+\.go$", path)
        return m.group(1) if m else None


@dataclass
class Exc:
    """One exception in a chain."""

    type: str
    message: str = ""
    frames: list[Frame] = field(default_factory=list)
    # The frames show where it was logged, not raised: zap's "stacktrace" for a Go error.
    logged_only: bool = False

    @property
    def title(self) -> str:
        if self.type == "error" and self.message:  # a Go error: no type, just its text
            return self.message
        return f"{self.type}: {self.message}" if self.message else self.type


@dataclass
class ErrorInfo:
    """The exception attached to a record: ``chain[0]`` is the root cause, ``chain[-1]`` the
    exception that was finally raised and logged."""

    chain: list[Exc]
    text: str | None = None  # the traceback as text, when the record had one

    @property
    def final(self) -> Exc:
        return self.chain[-1]

    @property
    def root(self) -> Exc:
        return self.chain[0]


def parse_traceback(text: str) -> list[Exc]:
    """Every exception in a traceback text, root cause first. Empty if there's none.

    Python's own format, IPython's and Jupyter's, and Django's debug page; also tracebacks
    indented as a whole (quoted in an issue or an email) or hard-wrapped by a terminal."""
    chain: list[Exc] = []
    current: Exc | None = None
    done = True  # the current exception's type line has been read
    ipython = False
    indent = ""  # the indent of the whole traceback, when it was quoted
    header = ""  # the last frame's "File ..." line, to put a wrapped one back together
    for raw in _ANSI.sub("", text).splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.endswith((TRACEBACK_START, TRACEBACK_HEAD)):
            current, done = Exc(type=""), False
            ipython = not stripped.endswith(":")
            indent = line[: len(line) - len(line.lstrip())]
            chain.append(current)
            continue
        if current is None or done:
            if current and stripped.startswith(_DJANGO_VALUE) and not current.message:
                current.message = stripped.removeprefix(_DJANGO_VALUE).strip()
            continue
        if indent and line.startswith(indent):
            line = line[len(indent) :]
        if ipython:
            done = _ipython_line(current, line, stripped)
            continue
        m = _FRAME.match(line)
        if m:
            current.frames.append(Frame(m["file"], int(m["line"]), (m["func"] or "?").strip()))
            header = line
            continue
        if not stripped:
            continue
        if line[:1].isspace():  # the source line under a frame, or 3.11+ ^^^^ markers
            last = current.frames[-1] if current.frames else None
            if last and last.code is None and not set(stripped) <= set("^~ "):
                current.frames[-1] = Frame(last.file, last.line, last.function, stripped)
            continue
        last = current.frames[-1] if current.frames else None
        if last and last.code is None and len(header) >= _WRAP_WIDTH and ": " not in stripped:
            # "..., line 446, in e" + "xecute_from_command_line"
            m = _FRAME.match(header + stripped)
            if m:
                header += stripped
                current.frames[-1] = Frame(m["file"], int(m["line"]), (m["func"] or "?").strip())
                continue
        m = _DJANGO_TYPE.match(stripped)
        if m:
            current.type, done = m["type"], True
            continue
        m = _EXC_LINE.match(line)
        if m:
            current.type, current.message, done = m["type"], (m["msg"] or "").strip(), True
    return [e for e in chain if e.type]


def _ipython_line(exc: Exc, line: str, stripped: str) -> bool:
    """Read one line of an IPython traceback into ``exc``; True once it's the exception."""
    if not stripped or line[:1].isspace():  # the code around each frame's line
        return False
    for rx in (_IPY_FILE, _IPY_CELL, _IPY_OLD):
        m = rx.match(stripped)
        if m:
            d = m.groupdict()
            file = d.get("file") or "<cell>"  # a notebook cell: never the project's code
            func = (d.get("func") or "<module>").strip()
            exc.frames.append(Frame(file, int(d.get("line") or 0), func))
            return False
    m = _IPY_ARROW.match(stripped)
    if m:
        if exc.frames:
            last = exc.frames[-1]
            line_no = last.line or int(m["line"])  # the older format has it only here
            exc.frames[-1] = Frame(last.file, line_no, last.function, m["code"].strip() or None)
        return False
    m = _EXC_LINE.match(stripped)
    if m:
        exc.type, exc.message = m["type"], (m["msg"] or "").strip()
        return True
    return False


def parse_java_trace(text: str) -> list[Exc]:
    """A Java stack trace (with ``Caused by:`` causes), root cause first; frames from the
    entry point to where each was thrown, like Python's."""
    chain: list[Exc] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        m = _JAVA_FRAME.match(line)
        if m and chain:
            chain[-1].frames.append(_java_frame(m["cls"], m["method"], m["src"]))
            continue
        if stripped.startswith(("...", "Suppressed:")):
            continue
        m = _JAVA_EXC.match(stripped)
        if m and (not chain or stripped.startswith("Caused by:")):
            chain.append(Exc(m["type"], (m["msg"] or "").strip()))
        elif (
            chain
            and stripped
            and line[:1].isspace()  # wrapped messages are indented; another header isn't
            and not chain[-1].frames
            and len(chain[-1].message) < 400
        ):
            # the message on the lines after "SomeException:" (wrapped, or Postgres' Detail)
            msg = chain[-1].message
            chain[-1].message = f"{msg} {stripped}".strip() if msg else stripped
    for exc in chain:
        exc.frames.reverse()  # Java prints the innermost frame first
    if not chain or not chain[0].frames:
        return []  # a line that only looks like an exception, without a trace
    return list(reversed(chain))


def _go_func(func: str) -> str:
    """``main.(*Cache).Set(0xc000120000, {0x9})`` -> ``Cache.Set``: the package path and the
    arguments go, the receiver stays."""
    name = func.split("/")[-1]
    if name.endswith(")") and "(" in name:
        name = name[: name.rfind("(")]  # the arguments (never nested parentheses)
    name = name.split(".", 1)[1] if "." in name else name
    return re.sub(r"\(\*?([^)]*)\)", r"\1", name)


def parse_go_trace(text: str, exc_type: str = "panic", message: str = "") -> list[Exc]:
    """A Go panic (``panic: ...`` + the panicking goroutine) or a stack alone (zap's
    ``stacktrace`` field). Frames from the entry point to where it panicked."""
    exc = Exc(exc_type, message)
    func = None
    goroutines = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _GO_PANIC.match(line)
        if m and not exc.frames and not goroutines:
            msg = m["msg"]
            if msg.startswith("runtime error: "):
                exc.type, exc.message = "runtime error", msg.removeprefix("runtime error: ")
            else:
                exc.type, exc.message = m["kind"], msg
            continue
        if line.startswith("goroutine "):
            goroutines += 1
            if goroutines > 1:
                break  # only the goroutine that panicked
            continue
        m = _GO_FILE.match(line)
        if m and func is not None:
            exc.frames.append(Frame(m["file"], int(m["line"]), _go_func(func)))
            func = None
            continue
        if line and not line[0].isspace() and not line.startswith(("created by", "exit status")):
            func = line
    # Go prints the innermost frame first; a panic's dump starts with the code handling it
    # (deferred functions, testing.tRunner's recover, the panic() call): not where it happened
    names = [f.function for f in exc.frames]
    if "panic" in names:
        exc.frames = exc.frames[names.index("panic") + 1 :]
    exc.frames.reverse()
    return [exc] if exc.frames else []


def _java_frame(cls: str, method: str, src: str) -> Frame:
    """A Java frame; generated proxies (``Foo$$SpringCGLIB$$0``, ``<generated>``) count as
    library code, since their file is the class they wrap."""
    file, _, number = src.partition(":")
    line = int(number) if number.isdigit() else 0
    if "$$" in cls or file.startswith("<generated"):
        return Frame("<generated>", line, method)
    package = cls.rsplit(".", 1)[0].split("$")[0] if "." in cls else ""
    if not file.endswith((".java", ".kt", ".scala", ".groovy")):  # Native Method
        file = cls.rsplit(".", 1)[-1].split("$")[0] + ".java"
    return Frame(f"{package.replace('.', '/')}/{file}" if package else file, line, method)


def line_frame(line: str) -> Frame | None:
    """The frame one line of a stack trace names (Python, Java or Go), for highlighting."""
    m = _FRAME.match(line)
    if m:
        return Frame(m["file"], int(m["line"]), (m["func"] or "?").strip())
    m = _JAVA_FRAME.match(line)
    if m:
        return _java_frame(m["cls"], m["method"], m["src"])
    m = _GO_FILE.match(line)
    if m:
        return Frame(m["file"], int(m["line"]), "?")
    return None


def is_exception_line(line: str) -> bool:
    """A line that names an exception: ``Caused by: ...``, ``KeyError: 'x'``, ``panic: ...``."""
    stripped = line.strip()
    if not stripped or line[:1].isspace():
        return False
    return bool(
        stripped.startswith(("Caused by:", "panic:", TRACEBACK_START))
        or _JAVA_EXC.match(stripped)
        or (_EXC_LINE.match(stripped) and ":" in stripped)
    )


def parse_js_trace(text: str) -> list[Exc]:
    """A Node.js error and its ``at`` frames (innermost first), with frames from the entry
    point to where it was thrown, like the others. Nested ``[cause]`` errors aren't followed."""
    exc = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if exc is None:
            m = _JS_ERROR_LINE.match(line.strip())
            if m:
                code = f" [{m['code']}]" if m["code"] else ""
                exc = Exc(m["type"], f"{m['msg']}{code}".strip())
            continue
        m = _JS_FRAME.match(line)
        if m:
            file = m["file"].removeprefix("file://")
            fn = (m["fn"] or "<anonymous>").split(" [as ", 1)[0].removeprefix("Object.")
            exc.frames.append(Frame(file, int(m["line"]), fn))
        elif line.strip() and not line[:1].isspace():
            break
    if exc is None or not exc.frames:
        return []
    exc.frames.reverse()
    return [exc]


def _parse_any(value: str, f: _Fields) -> tuple[list[Exc], str] | None:
    """The chain in a stack-trace text of any language, and the text from its start."""
    if TRACEBACK_HEAD in value:
        chain = parse_traceback(value)
        if chain:
            return chain, value[value.index(TRACEBACK_HEAD) :]
    if "\tat " in value or "\n    at " in value or value.lstrip().startswith("at "):
        chain = parse_java_trace(value)
        if chain:
            return chain, value
        chain = parse_js_trace(value)
        if chain:
            m = next(_JS_ERROR_LINE.match(x.strip()) for x in value.splitlines()
                     if _JS_ERROR_LINE.match(x.strip()))  # fmt: skip
            return chain, value[value.index(m.group(0)) :] if m.group(0) in value else value
    if ".go:" in value:
        message = next((str(v) for k in ("error", "err") if (v := f.get(k)) is not None), "")
        chain = parse_go_trace(value, "error", message)
        if chain:
            if chain[0].type == "error":
                chain[0].logged_only = True  # a stack taken where it was logged
            start = next((value.index(k) for k in ("panic: ", "fatal error: ") if k in value), 0)
            return chain, value[start:]
    return None


def _structlog_chain(value: list) -> list[Exc]:
    """structlog's ``dict_tracebacks``: the raised exception first, then its causes."""
    chain = []
    for item in value:
        if not isinstance(item, dict) or "exc_type" not in item:
            return []
        frames = [
            Frame(str(f.get("filename", "?")), int(f.get("lineno") or 0), str(f.get("name", "?")))
            for f in item.get("frames") or []
            if isinstance(f, dict)
        ]
        chain.append(Exc(str(item["exc_type"]), str(item.get("exc_value") or ""), frames))
    return chain[::-1]


class _Fields:
    """A record's fields by dotted path (``error.type`` whether stored nested or flattened)
    and by last segment (``level`` also finds ``json.level``), built once per record."""

    def __init__(self, record: LogRecord) -> None:
        self.exact: dict[str, object] = {}
        self.last: dict[str, object] = {}
        for key, value in _keys(record.fields):
            if value is None:
                continue
            self.exact.setdefault(key, value)
            self.last.setdefault(key.rsplit(".", 1)[-1], value)

    def get(self, key: str) -> object | None:
        value = self.exact.get(key)
        if value is None and "." not in key:
            value = self.last.get(key)
        return value


def extract_error(record: LogRecord, fields: _Fields | None = None) -> ErrorInfo | None:
    """The exception attached to ``record``, from whichever shape it was logged in."""
    f = fields or _Fields(record)
    for key in TRACEBACK_FIELDS:
        value = f.get(key)
        if isinstance(value, list) and value:
            chain = _structlog_chain(value)
            if chain:
                return ErrorInfo(chain)
        if isinstance(value, str) and "\n" in value:
            found = _parse_any(value, f)
            if found:
                return ErrorInfo(found[0], text=found[1])
    if "\n" in record.message:
        found = _parse_any(record.message, f)
        if found:
            return ErrorInfo(found[0], text=found[1])
    exc_type = next((v for k in TYPE_FIELDS if isinstance(v := f.get(k), str) and v), None)
    if exc_type:
        message = next((str(v) for k in MESSAGE_FIELDS if (v := f.get(k)) is not None), "")
        return ErrorInfo([Exc(exc_type, message)])
    return None


def level_of(record: LogRecord, fields: _Fields | None = None) -> str | None:
    f = fields or _Fields(record)
    for key in LEVEL_FIELDS:
        value = f.get(key)
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    return None


def is_error(record: LogRecord) -> bool:
    """An error-level record, or any record that carries an exception."""
    fields = _Fields(record)
    level = level_of(record, fields)
    if level is not None:
        if level.lower() in ERROR_WORDS:
            return True
    else:
        levelno = record.fields.get("levelno")
        if isinstance(levelno, int) and levelno >= 40:
            return True
        # Plain text, perhaps with stream labels as fields (Loki): the level is in the text,
        # "2026-10-17 12:00:01 ERROR app: payment failed"
        if _TEXT_LEVEL.search(record.message.split("\n", 1)[0][:200]):
            return True
    return extract_error(record, fields) is not None


# A record that is only one line of a stack trace ("Caused by: …", "\tat com.x.Y(Y.java:3)",
# '  File "a.py", line 3'): the log shipper sent each line as its own entry.
_TRACE_PIECE = re.compile(r"\s*(Caused by: |at [\w$.<>/]+\(|File \"[^\"]+\", line \d+)")


def is_trace_piece(message: str) -> bool:
    return bool(_TRACE_PIECE.match(message))


def join_multiline(lines: Iterable[str]) -> Iterator[str]:
    """Join plain-text tracebacks that span several lines back into one event each.

    A Python traceback continues its event from ``Traceback (most recent call last):``
    through the indented frame lines to the ``SomeError: message`` line, including chained
    tracebacks. A Java one is the ``com.acme.SomeException: message`` line and the ``at ...``,
    ``Caused by:`` and ``... 3 more`` lines after it. A Go panic runs from ``panic:`` to the
    next line that starts like a log line. JSON lines are always whole events.
    """
    event: list[str] = []
    in_frames = False  # between "Traceback ..." and the exception line
    in_panic = False  # after a Go "panic: ..."
    in_java = False  # inside a Java stack trace
    ipython = False  # the frames are IPython's: unindented, up to the exception line
    django_value = False  # Django's "Exception Value:" line comes next
    previous = ""
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        wrapped = len(previous) >= _WRAP_WIDTH and ": " not in stripped  # see parse_traceback
        previous = line
        if not event or stripped.startswith("{"):
            continuation = False
        elif django_value and stripped.startswith(_DJANGO_VALUE):
            continuation, django_value = True, False
        elif in_frames and _DJANGO_TYPE.match(stripped):
            continuation, in_frames, django_value = True, False, True
        elif in_frames and not ipython and wrapped and _FRAME.match(event[-1] + stripped):
            continuation = True  # the rest of a frame line a terminal wrapped
        elif in_frames and not line[:1].isspace() and _EXC_LINE.match(line):
            continuation, in_frames = True, False
        elif in_frames and ipython:
            continuation = True  # "File ~/x.py:52, in f()", "---> 52 code": not indented
        elif in_panic and stripped and not _LOG_LINE_START.match(line):
            continuation = True
        elif _JAVA_CONTINUATION.match(line) or (
            not line[:1].isspace()
            and (_JAVA_THROWABLE.match(stripped) or _JS_ERROR_LINE.match(stripped))
        ):
            continuation = in_java = True
        elif in_java and stripped and line[:1].isspace():
            continuation = True  # "  Detail: ...", "  SQLState: 57014" under a cause
        elif stripped.startswith(TRACEBACK_START) or stripped.endswith(TRACEBACK_HEAD):
            continuation, in_frames = True, True
            ipython = not stripped.endswith(":")
        elif stripped in CHAIN_MARKERS:
            continuation = True
        elif in_frames and (not stripped or line[:1].isspace()):
            continuation = True
        elif not stripped:
            continue  # blank lines separate chained tracebacks; never an event of their own
        else:
            continuation = False
        if continuation:
            if stripped:
                event.append(line)
            continue
        if event:
            yield "\n".join(event)
        event = [line] if stripped else []
        in_java = False
        in_frames = stripped.endswith((TRACEBACK_START, TRACEBACK_HEAD))
        ipython = in_frames and not stripped.endswith(":")
        in_panic = bool(_GO_PANIC.search(stripped)) or (in_panic and not stripped)
    if event:
        yield "\n".join(event)


# "\tat com.acme.X.y(X.java:3)", "\t... 12 more", "Caused by: ...", "\tSuppressed: ..."
_JAVA_CONTINUATION = re.compile(
    r"^(\s+at \S|\s*\.\.\. \d+ (more|common frames)|\s*Caused by: |\s+Suppressed: )"
)
# "java.lang.IllegalStateException: boom" on its own line, under the log line that logged it
# Node.js: "Error: connect ECONNREFUSED ...", "TypeError: Cannot read properties of undefined",
# "Uncaught RangeError [ERR_X]: ...", then "    at fn (file:line:col)" frames
_JS_ERROR_LINE = re.compile(
    r"^(?:Uncaught )?(?P<type>(?:[A-Z][\w$]*)?(?:Error|Exception))(?: \[(?P<code>[\w_]+)\])?: "
    r"(?P<msg>.*)$"
)
_JS_FRAME = re.compile(
    r"^\s+at (?:async )?(?:(?P<fn>[^\s(]+(?: \[as [^\]]+\])?) \()?(?P<file>[^\s()]+?):"
    r"(?P<line>\d+):\d+\)?$"
)
_JAVA_THROWABLE = re.compile(
    r"^(?:[a-z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable)(?::|$)"
)
# How a new log line usually starts: a date, a bracketed date, or a level word.
_LOG_LINE_START = re.compile(r"^(\[?\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|[A-Z]{4,8} |time=|level=)")
