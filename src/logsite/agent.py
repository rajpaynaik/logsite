"""Agent mode: test and build output, grouped by cause, with what changed since the last run.

``logsite agent -- pytest`` runs the command and prints, instead of hundreds of lines, one
group per cause (30 failing tests -> 2 causes), where each was raised in your code, and how
this run differs from the previous one (fixed, still failing, new). The full output is saved
to a file; ``logsite agent --show N`` prints group N's first failure in full.

Recognised: pytest, ``go test`` / ``go build``, Maven and Gradle (JUnit), Jest, Vitest, and compiler
errors in the ``file:line:col: message`` / ``file(line,col): error ...`` shapes (tsc, javac,
go, gcc, mypy...). Anything else that fails shows its last lines: nothing is hidden.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AppFiles, normalize
from .tracebacks import Frame, parse_go_trace, parse_java_trace

TAIL_LINES = 40  # of unrecognised failing output
SHOW_LINES = 120  # of one failure, for --show


@dataclass
class Failure:
    test: str | None  # "tests/test_orders.py::test_order_3", "TestTotal", None for a build error
    kind: str  # "test" | "build"
    exc_type: str | None
    message: str
    raised_at: str | None = None  # "app/orders/repository.py:10": the deepest frame of yours
    raised_in: str | None = None
    test_file: str | None = None
    origin: str | None = None  # the library the failure came out of
    block: list[str] = field(default_factory=list)  # the original lines, for --show
    # every line of the project the traceback passes through ("path:line"), root cause first
    locations: list[str] = field(default_factory=list)

    def key(self) -> str:
        """What makes two failures the same cause."""
        if self.kind == "build":
            return f"build|{build_pattern(self.message)}"
        if self.raised_at and not _is_test_path(self.raised_at.rsplit(":", 1)[0]):
            return f"raised|{self.exc_type}|{self.raised_at}"
        # failed inside the test itself (an assertion): the pattern of the message, per file
        return f"assert|{self.exc_type}|{self.test_file}|{normalize(self.message)}"


def build_pattern(message: str) -> str:
    """A compiler message with its numbers masked but its names kept: which name is missing
    or mistyped is the cause (``Cannot find name 'totl'`` and ``'shippng'`` are two typos)."""
    code, sep, rest = message.partition(" ")
    if not re.fullmatch(r"(TS|CS)\d+", code):
        code, sep, rest = "", "", message
    return code + sep + re.sub(r"\b\d+(?:\.\d+)*\b", "<n>", rest)


@dataclass
class Cause:
    key: str
    failures: list[Failure]

    @property
    def id(self) -> str:
        return hashlib.sha1(self.key.encode()).hexdigest()[:8]

    @property
    def first(self) -> Failure:
        return self.failures[0]

    @property
    def tests(self) -> list[str]:
        return [f.test for f in self.failures if f.test]

    @property
    def title(self) -> str:
        f = self.first
        message = f.message.split("\n", 1)[0]
        if len({x.message for x in self.failures}) > 1:
            message = build_pattern(message) if f.kind == "build" else normalize(message)
        if f.exc_type and not message.startswith(f.exc_type):
            return f"{f.exc_type}: {message}" if message else f.exc_type
        return message or (f.exc_type or "failed")


@dataclass
class Run:
    tool: str
    command: list[str]
    exit_code: int
    seconds: float
    causes: list[Cause]
    counts: dict[str, int]  # passed / failed / errors / skipped, as the tool reported them
    raw_path: str | None = None
    tail: list[str] = field(default_factory=list)  # unrecognised failing output


# --- parsing helpers ---------------------------------------------------------------------------

_LIBRARY = re.compile(
    r"(site-packages|dist-packages|/lib/python\d|/go/src/|/pkg/mod/|^<|node_modules/|^node:)"
)


def _is_test_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return bool(
        re.match(
            r"(test_.*\.py|.*_test\.py|.*_test\.go|.*Tests?\.java|.*\.(test|spec)\.[jt]sx?)$", name
        )
        or "/tests/" in f"/{path}"
        or "/test/" in f"/{path}"
    )


def _locations(frames: Iterable[Frame], app: AppFiles) -> list[str]:
    return [f"{p}:{f.line}" for p, f in ((app.resolve(f), f) for f in frames) if p]


def _place(frames: list[Frame], app: AppFiles) -> tuple[str | None, str | None, str | None]:
    """``(raised_at, function, origin)``: the deepest frame of the project (not a test, if
    there is one), and the library the failure came out of beyond it."""
    # yours = files in the project; without a file list, anything that isn't plainly a library
    known = bool(app._by_name)
    own = [
        (app.resolve(f) if known else (None if _LIBRARY.search(f.file) else f.file), f)
        for f in frames
    ]
    own = [(p, f) for p, f in own if p]
    if not own:
        return None, None, frames[-1].library if frames else None
    code = [(p, f) for p, f in own if not _is_test_path(p)] or own
    path, frame = code[-1]
    origin = None
    if (
        frames
        and frames[-1] is not frame
        and (frames[-1].is_library or not app.resolve(frames[-1]))
    ):
        origin = frames[-1].library
    return f"{path}:{frame.line}", frame.function, origin


# --- pytest -----------------------------------------------------------------------------------

_PYTEST_HEADER = re.compile(r"^_{3,} (?P<name>.+?) _{3,}$")
_PYTEST_SECTION = re.compile(r"^={3,} (?P<name>FAILURES|ERRORS|short test summary info) ={3,}$")
_PYTEST_LOCATION = re.compile(r"^(?P<file>[^\s:][^:]*\.py):(?P<line>\d+): ?(?P<tail>.*)$")
_PYTEST_SUMMARY = re.compile(
    r"^={3,} (?P<body>.*\d+ (passed|failed|error|errors|skipped).*) ={3,}$"
)
_PYTEST_FAILED = re.compile(r"^(FAILED|ERROR) (?P<id>\S+)")


def parse_pytest(lines: list[str], app: AppFiles) -> tuple[list[Failure], dict[str, int]]:
    failures: list[Failure] = []
    counts: dict[str, int] = {}
    section = None
    block: list[str] = []
    name = None
    ids: dict[str, str] = {}  # test function name -> node id, from the short summary

    def flush() -> None:
        if name is None or not block:
            return
        frames, errors, exc_name, failing, func = [], [], None, None, "?"
        for line in block:
            m = _PYTEST_LOCATION.match(line)
            if m:
                tail = m["tail"].strip()
                frames.append(
                    Frame(m["file"], int(m["line"]), tail[3:] if tail.startswith("in ") else func)
                )
                if tail and not tail.startswith("in "):
                    exc_name = tail
                func = "?"
            elif line.startswith("E "):
                errors.append(line[1:].strip())
            elif line.startswith(">"):
                failing = line[1:].strip()  # the line that failed, as pytest marks it
            else:
                d = re.match(r"^\s*(?:async )?def (\w+)\(", line)
                if d:
                    func = d.group(1)
        message = "\n".join(errors) if errors else ""
        exc_type = exc_name
        first = errors[0] if errors else ""
        m = re.match(
            r"^(?P<type>[\w.]+(?:Error|Exception|Exit|Interrupt|Warning)\b):?\s*(?P<msg>.*)", first
        )
        if m:
            exc_type, message = (
                m["type"],
                m["msg"] + ("\n" + "\n".join(errors[1:]) if errors[1:] else ""),
            )
        if (
            failing
            and failing.startswith("assert")
            and (not errors or errors[0].startswith("assert"))
        ):
            message = failing + ("\n" + "\n".join(errors) if errors else "")  # names the call
        raised, func, origin = _place(frames, app)
        test_file = next((f.file for f in frames if _is_test_path(f.file)), None)
        failures.append(
            Failure(
                test=ids.get(name, name),
                kind="test",
                exc_type=exc_type or "AssertionError",
                message=message.strip(),
                raised_at=raised,
                raised_in=func,
                test_file=test_file,
                origin=origin,
                block=list(block),
                locations=_locations(frames, app),
            )
        )

    for line in lines:
        s = _PYTEST_SECTION.match(line)
        if s:
            flush()
            name, block = None, []
            section = s["name"]
            continue
        m = _PYTEST_SUMMARY.match(line)
        if m:
            flush()
            name, block = None, []
            for n, what in re.findall(
                r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", m["body"]
            ):
                counts["errors" if what.startswith("error") else what] = int(n)
            continue
        if section == "short test summary info":
            f = _PYTEST_FAILED.match(line)
            if f:
                ids[f["id"].rsplit("::", 1)[-1]] = f["id"]
            continue
        if section in ("FAILURES", "ERRORS"):
            h = _PYTEST_HEADER.match(line)
            if h:
                flush()
                name, block = h["name"].replace("ERROR at setup of ", ""), []
                continue
            if name is not None:
                block.append(line)
    flush()
    for f in failures:  # short summary ids arrive after the blocks
        if f.test and "::" not in f.test and f.test in ids:
            f.test = ids[f.test]
    return failures, counts


# --- go test / go build -----------------------------------------------------------------------

_GO_FAIL = re.compile(r"^\s*--- FAIL: (?P<test>\S+)")
_GO_PASS = re.compile(r"^\s*--- PASS: ")
_GO_ERROR_LINE = re.compile(r"^\s+(?P<file>\S+\.go):(?P<line>\d+): (?P<msg>.*)$")
_GO_PKG = re.compile(r"^(ok|FAIL|---)\s+(?P<pkg>\S+)\s")


def parse_go(lines: list[str], app: AppFiles) -> tuple[list[Failure], dict[str, int]]:
    failures: list[Failure] = []
    counts = Counter()
    test = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _GO_FAIL.match(line)
        if m:
            test = m["test"]
            counts["failed"] += 1
            i += 1
            continue
        if _GO_PASS.match(line):
            counts["passed"] += 1
        if line.startswith("panic: "):
            end = i + 1
            while end < len(lines) and not re.match(r"^(FAIL|ok)\s", lines[end]):
                end += 1
            block = lines[i:end]
            (exc,) = parse_go_trace("\n".join(block)) or [None]
            if exc is not None:
                raised, func, origin = _place(exc.frames, app)
                in_test = next((f.file for f in exc.frames if _is_test_path(f.file)), None)
                test_file = app.resolve(Frame(in_test, 0, "?")) or in_test if in_test else None
                failures.append(
                    Failure(test, "test", exc.type, exc.message, raised, func, test_file,
                            origin, block)
                )  # fmt: skip
            i = end
            continue
        e = _GO_ERROR_LINE.match(line)
        if e and test:
            block = [line]
            while i + 1 < len(lines) and lines[i + 1].startswith("        "):  # wrapped message
                i += 1
                block.append(lines[i])
            path = app.resolve(Frame(e["file"], int(e["line"]), "?")) or e["file"]
            failures.append(
                Failure(test, "test", None, e["msg"].strip(), f"{path}:{e['line']}", test,
                        path, None, block)
            )  # fmt: skip
        elif test is None:
            b = _COMPILE.match(line.strip())
            if b and b["file"].endswith(".go"):
                failures.append(_build_failure(b, line, app))
        i += 1
    return failures, dict(counts)


# --- JUnit: Maven surefire, Gradle ------------------------------------------------------------

_SUREFIRE = re.compile(
    r"^\[ERROR\] (?P<test>\S+?)(?:\((?P<cls>[\w.$]+)\))?\s+(?:--\s+)?"
    r"Time elapsed: .*<<< (FAILURE|ERROR)!"
)
_GRADLE = re.compile(r"^(?P<cls>\S+) > (?P<test>.+?) FAILED$")
_JUNIT_TOTALS = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)")


def parse_junit(lines: list[str], app: AppFiles) -> tuple[list[Failure], dict[str, int]]:
    failures: list[Failure] = []
    counts: dict[str, int] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _SUREFIRE.match(line) or _GRADLE.match(line.strip())
        if m:
            test = m["test"] if not m.groupdict().get("cls") else f"{m['cls']}.{m['test']}"
            end = i + 1
            while end < len(lines) and lines[end].strip() and not lines[end].startswith("["):
                if _GRADLE.match(lines[end].strip()):
                    break
                end += 1
            block = lines[i + 1 : end]
            failures.append(_junit_failure(test, block, app))
            i = end
            continue
        t = _JUNIT_TOTALS.search(line)
        if t and (
            "Results" in "".join(lines[max(0, i - 3) : i])
            or line.startswith("[ERROR] Tests run")
            or line.startswith("[WARNING] Tests run")
        ):
            run, fails, errs, skipped = (int(x) for x in t.groups())
            counts = {
                "passed": run - fails - errs - skipped,
                "failed": fails,
                "errors": errs,
                "skipped": skipped,
            }
        i += 1
    return failures, counts


def _junit_failure(test: str, block: list[str], app: AppFiles) -> Failure:
    text = "\n".join(x.strip() if x.strip().startswith("at ") else x for x in block)
    chain = parse_java_trace(
        "\n".join("\tat " + x.strip()[3:] if x.strip().startswith("at ") else x for x in block)
    )
    if chain:
        root = chain[0]
        frames = [f for e in chain for f in e.frames]
        raised, func, origin = _place(root.frames or frames, app)
        head = chain[-1]
        resolved = (app.resolve(f) for f in frames)
        test_file = next((p for p in resolved if p and _is_test_path(p)), None)
        return Failure(test, "test", head.type.rsplit(".", 1)[-1], head.message, raised, func,
                       test_file, origin, block, _locations(frames, app))  # fmt: skip
    g = re.match(r"^\s*(?P<type>[\w.$]+) at (?P<file>\S+\.(?:java|kt)):(?P<line>\d+)", text)
    if g:  # Gradle's short form: "org.opentest4j.AssertionFailedError at TaxTest.java:12"
        path = app.resolve(Frame(g["file"], int(g["line"]), "?")) or g["file"]
        return Failure(test, "test", g["type"].rsplit(".", 1)[-1], "", f"{path}:{g['line']}", None,
                       path, None, block)  # fmt: skip
    return Failure(test, "test", None, block[0].strip() if block else "failed", block=block)


# --- Jest / Vitest ------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_JEST_FILE = re.compile(r"^(FAIL|PASS) +(?P<file>\S+)")
_JEST_TEST = re.compile(r"^  ● (?P<name>.+)$")
_JEST_TOTALS = re.compile(r"^Tests:\s+(?P<body>.+)$")
_VITEST_TEST = re.compile(r"^ FAIL  (?P<file>\S+?)(?: > (?P<name>.+?)| \[ .+ \])?$")
_VITEST_TOTALS = re.compile(r"^\s+Tests\s+(?P<body>\d+ (?:failed|passed|skipped|todo).*)$")
_VITEST_RULE = re.compile(r"^⎯{3,}")
# V8 stack frames: "at fn (file:line:col)", "at file:line:col"; Vitest: "❯ fn file:line:col"
_JS_AT = re.compile(r"^\s+at (?:(?P<fn>.+?) \()?(?P<file>[^\s()]+?):(?P<line>\d+):\d+\)?$")
_JS_ARROW = re.compile(r"^\s*❯ (?:(?P<fn>\S+) )?(?P<file>[^\s()]+?):(?P<line>\d+):\d+$")
_JS_CODE = re.compile(r"^\s*>?\s*\d*\s*\|")  # a code frame line: "  > 8 | ...", "   11| ..."
_JS_ERROR = re.compile(r"^(?P<type>[\w$.]*(?:Error|Exception)|Error):\s?(?P<msg>.*)$")


def _js_frames(block: list[str]) -> list[Frame]:
    """The first error's frames, outermost first (V8 lists the innermost first)."""
    frames = []
    for line in block:
        if line.lstrip().startswith("Caused by:"):
            break
        m = _JS_AT.match(line) or _JS_ARROW.match(line)
        if not m or m["file"] in ("<anonymous>", "native"):
            continue
        file = m["file"].removeprefix("file://")
        fn = (m["fn"] or "?").removeprefix("Object.").removeprefix("async ")
        frames.append(Frame(file, int(m["line"]), fn.split(" [as ", 1)[0]))
    return frames[::-1]


def _js_failure(test: str, file: str | None, block: list[str], app: AppFiles) -> Failure:
    text: list[str] = []
    for line in block:  # the message: everything before the code frame and the stack
        if _JS_CODE.match(line) or _JS_AT.match(line) or _JS_ARROW.match(line):
            break
        if line.lstrip().startswith("Caused by:"):
            break
        text.append(line.strip())
    while text and not text[-1]:
        text.pop()
    while text and not text[0]:
        text.pop(0)
    exc_type, message = "Error", "\n".join(text)
    m = _JS_ERROR.match(text[0]) if text else None
    if m:
        exc_type = m["type"]
        message = "\n".join([m["msg"], *text[1:]])
    elif message.startswith(("expect(", "expected ")):
        exc_type = "AssertionError"
    message = "\n".join(x for x in message.split("\n") if x not in ("- Expected", "+ Received"))
    message = re.sub(r"\n{2,}", "\n", message).strip()
    raised, func, origin = _place(_js_frames(block), app)
    if func == "?" or (raised and _is_test_path(raised.rsplit(":", 1)[0])):
        func = None  # in a test file V8 (with Jest's source maps) names the callee: toBe
    return Failure(
        test=test, kind="test", exc_type=exc_type, message=message, raised_at=raised,
        raised_in=func, test_file=file, origin=origin, block=list(block),
    )  # fmt: skip


def _js_counts(body: str) -> dict[str, int]:
    out = {}
    for n, what in re.findall(r"(\d+) (failed|passed|skipped|todo)", body):
        out[what] = int(n)
    return out


def parse_jest(lines: list[str], app: AppFiles) -> tuple[list[Failure], dict[str, int]]:
    """Jest and Vitest: one failure per test (a test file that failed to load counts as
    one), with where it was raised in your code."""
    lines = [_ANSI.sub("", x) for x in lines]
    failures: list[Failure] = []
    counts: dict[str, int] = {}
    file, name, block = None, None, []
    pending: list[tuple[str, str]] = []  # Vitest: FAIL headers sharing the next error
    vitest = any(_VITEST_TEST.match(x) or x.startswith(" RUN  v") for x in lines[:400])

    def flush() -> None:
        nonlocal name, block
        if name is not None and any(x.strip() for x in block):
            for f_file, f_name in [*pending, (file, name)]:
                failures.append(_js_failure(f_name, f_file, block, app))
            pending.clear()
        name, block = None, []

    for line in lines:
        if vitest:
            t = _VITEST_TOTALS.match(line)
            if t:
                flush()
                counts.update(_js_counts(t["body"]))
                continue
            m = _VITEST_TEST.match(line)
            if m:
                if name is not None and not any(x.strip() for x in block):
                    pending.append((file, name))  # no error of its own: shares the next one
                    name = None
                flush()
                file = m["file"]
                name = f"{m['file']} > {m['name']}" if m["name"] else m["file"]
                continue
            if _VITEST_RULE.match(line):
                flush()
                continue
        else:
            if line.startswith("Summary of all failing tests"):
                flush()
                break  # the same failures again
            t = _JEST_TOTALS.match(line)
            if t:
                flush()
                counts.update(_js_counts(t["body"]))
                continue
            m = _JEST_FILE.match(line)
            if m:
                flush()
                file = m["file"]
                continue
            m = _JEST_TEST.match(line)
            if m:
                flush()
                if m["name"] != "Console":  # console.log output, not a failure
                    test = file if m["name"] == "Test suite failed to run" else m["name"]
                    name = test if test == file else f"{file} › {test}"
                continue
            if line.startswith("Test Suites:"):
                flush()
                continue
        if name is not None:
            block.append(line)
    flush()
    return failures, counts


# --- compiler errors ---------------------------------------------------------------------------

_COMPILE = re.compile(
    r"^(?:\[ERROR\]\s+)?(?P<file>[^\s:()]+\.(?:py|pyi|java|kt|go|ts|tsx|js|jsx|c|cc|cpp|h|rs|cs|scala))"
    r"(?::\[?(?P<line>\d+)(?:[:,](?P<col>\d+))?\]?|\((?P<line2>\d+),(?P<col2>\d+)\))"
    r"(?::|\s+-)?\s*(?:(?P<severity>error|warning|fatal error)\s*:?\s*)?"
    r"(?P<code>(?:TS|CS)\d+:?\s*)?(?P<msg>.+)$",
    re.IGNORECASE,
)


def _build_failure(m: re.Match, line: str, app: AppFiles) -> Failure:
    n = m["line"] or m["line2"]
    path = app.resolve(Frame(m["file"], int(n or 0), "?")) or m["file"]
    code = (m["code"] or "").strip().rstrip(":")
    message = f"{code} {m['msg'].strip()}".strip()
    return Failure(None, "build", None, message, f"{path}:{n}", None, path, None, [line])


def parse_compile(lines: list[str], app: AppFiles) -> tuple[list[Failure], dict[str, int]]:
    failures = []
    for i, line in enumerate(lines):
        m = _COMPILE.match(line.strip())
        if not m or (m["severity"] or "").lower() == "warning":
            continue
        f = _build_failure(m, line, app)
        # javac / maven put the detail on the next lines: "  symbol:   variable rate"
        for extra in lines[i + 1 : i + 4]:
            d = re.match(r"^\s+(symbol|location|required|found)\s*:\s*(.*)$", extra)
            if d:
                f.block.append(extra)
                if d.group(1) == "symbol":  # which name is missing is part of the cause
                    f.message += f": {d.group(2).strip()}"
        failures.append(f)
    return failures, {"errors": len(failures)} if failures else {}


# --- the run ------------------------------------------------------------------------------------


def detect(command: list[str], lines: list[str]) -> str:
    """Which parser fits: by the command first, then by what the output looks like."""
    words = " ".join(os.path.basename(c) for c in command[:3])
    text = "\n".join(lines[:400] + lines[-200:])
    if "pytest" in words or "test session starts" in text:
        return "pytest"
    if re.search(r"\bvitest\b", words) or re.search(r"^ RUN  v\d", text, re.M):
        return "vitest"
    if re.search(r"\bjest\b", words) or re.search(r"^Tests:\s+\d+ (failed|passed)", text, re.M):
        return "jest"
    if re.search(r"\bgo (test|build|vet)\b", words) or re.search(
        r"^(ok|FAIL)\s+\S+\s+[\d.]+s", text, re.M
    ):
        return "go"
    if (
        re.search(r"\b(mvn|mvnw|gradle|gradlew)\b", words)
        or "T E S T S" in text
        or " FAILED" in text
        and " > " in text
    ):
        return "junit"
    return "compile"


PARSERS = {
    "pytest": parse_pytest,
    "go": parse_go,
    "junit": parse_junit,
    "jest": parse_jest,
    "vitest": parse_jest,
    "compile": parse_compile,
}


def group(failures: Iterable[Failure]) -> list[Cause]:
    causes: dict[str, Cause] = {}
    for f in failures:
        causes.setdefault(f.key(), Cause(f.key(), [])).failures.append(f)
    return sorted(causes.values(), key=lambda c: (-len(c.failures), c.title))


def analyse(command: list[str], output: str, exit_code: int, seconds: float, app: AppFiles) -> Run:
    lines = output.splitlines()
    tool = detect(command, lines)
    failures, counts = PARSERS[tool](lines, app)
    if tool != "compile" and not failures:  # a test runner that failed to build, say
        failures, extra = parse_compile(lines, app)
        counts = {**counts, **extra}
    causes = group(failures)
    run = Run(tool, command, exit_code, seconds, causes, counts)
    if exit_code != 0 and not causes:
        run.tail = lines[-TAIL_LINES:]
    return run


def run_command(command: list[str], cwd: Path) -> tuple[str, int, float]:
    start = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace")  # fmt: skip
    except FileNotFoundError:
        return f"logsite agent: command not found: {command[0]}\n", 127, 0.0
    return proc.stdout, proc.returncode, time.monotonic() - start


# --- what changed since the last run -------------------------------------------------------------


def state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "logsite" / "agent"


def state_key(root: Path, command: list[str]) -> str:
    return hashlib.sha1(f"{root.resolve()}\0{' '.join(command)}".encode()).hexdigest()[:16]


def load_previous(key: str) -> dict | None:
    try:
        return json.loads((state_dir() / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None


def save(key: str, run: Run, output: str) -> None:
    d = state_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        raw = d / f"{key}.log"
        raw.write_text(output, encoding="utf-8")
        run.raw_path = str(raw)
        state = {
            "command": run.command,
            "exit_code": run.exit_code,
            "counts": run.counts,
            "causes": {
                c.id: {"title": c.title, "count": len(c.failures), "tests": c.tests[:500]}
                for c in run.causes
            },
            "show": {c.id: c.first.block[:SHOW_LINES] for c in run.causes},
            "order": [c.id for c in run.causes],
        }
        (d / f"{key}.json").write_text(json.dumps(state))
    except OSError:
        pass  # a read-only home: no delta next time, nothing else lost


@dataclass
class Delta:
    fixed: list[tuple[str, dict]]  # causes gone since the last run
    new: list[Cause]
    still: list[Cause]
    fixed_tests: int
    previous_failed: int


def delta(run: Run, previous: dict | None) -> Delta | None:
    if not previous:
        return None
    before = previous.get("causes", {})
    now = {c.id for c in run.causes}
    fixed = [(cid, info) for cid, info in before.items() if cid not in now]
    new = [c for c in run.causes if c.id not in before]
    still = [c for c in run.causes if c.id in before]
    was = {t for info in before.values() for t in info.get("tests", [])}
    failing = {t for c in run.causes for t in c.tests}
    previous_failed = sum(info.get("count", 0) for info in before.values())
    return Delta(fixed, new, still, len(was - failing), previous_failed)


# --- output ------------------------------------------------------------------------------------


def _counts_text(counts: dict[str, int], previous: dict | None) -> str:
    order = ("failed", "errors", "passed", "skipped", "xfailed", "xpassed")
    parts = []
    was = (previous or {}).get("counts", {})
    for k in order:
        if counts.get(k):
            text = f"{counts[k]:,} {k}"
            if k in ("failed", "errors") and was.get(k) not in (None, counts[k]):
                text += f" (was {was[k]:,})"
            parts.append(text)
    return ", ".join(parts)


def _where(f: Failure, root: Path, pal) -> list[str]:
    from .term import source_line

    if not f.raised_at:
        return []
    label = "at      " if f.kind == "build" else "raised  "
    text = f"   {label} {pal.yours(f.raised_at)}"
    if f.raised_in and f.raised_in != "?":
        text += pal.gray(f" in {f.raised_in}()")
    if f.origin:
        text += pal.magenta(f"  ({f.origin})")
    out = [text]
    path, _, line = f.raised_at.rpartition(":")
    code = source_line(root, path, int(line)) if line.isdigit() else None
    if code:
        out.append(f"            {pal.gray(f'{line} │')} {pal.code(code[:110])}")
    return out


def _test_name(test: str, test_file: str | None) -> str:
    """pytest node ids -> the function; Jest/Vitest "file › describe › test" -> without the
    file; JUnit "com.x.PricingTest.total" -> "PricingTest.total"."""
    if "::" in test:
        return test.rsplit("::", 1)[-1]
    if test_file and test.startswith(test_file):
        return test[len(test_file) :].lstrip(" ›>") or "the file didn't load"
    return ".".join(test.split(".")[-2:])


def _tests_text(c: Cause) -> str:
    tested = [f for f in c.failures if f.test]
    by_file = Counter(
        (f.test.split("::", 1)[0] if "::" in f.test else (f.test_file or c.first.test_file or ""))
        for f in tested
    )
    names = [_test_name(f.test, f.test_file) for f in tested]
    shown = ", ".join(names[:3]) + (", …" if len(names) > 3 else "")
    files = ", ".join(f"{f} ×{n}" if f else f"×{n}" for f, n in by_file.most_common(3))
    return f"{files} ({shown})" if files else shown


def _build_text(c: Cause) -> str:
    by_file = Counter((f.raised_at or "?").rsplit(":", 1)[0] for f in c.failures)
    return ", ".join(f"{p} ×{n}" for p, n in by_file.most_common(4)) + (
        f", +{len(by_file) - 4} more files" if len(by_file) > 4 else ""
    )


def render(run: Run, d: Delta | None, previous: dict | None, root: Path, pal) -> list[str]:
    what = "error" if all(c.first.kind == "build" for c in run.causes) and run.causes else "test"
    head = f"{run.tool}: "
    counts = _counts_text(run.counts, previous)
    failing = sum(len(c.failures) for c in run.causes)
    if counts:
        head += counts
    elif failing:
        head += f"{failing:,} {what}(s) failing"
    else:
        head += "ok" if run.exit_code == 0 else "failed"
    if run.causes:
        head += f" · {len(run.causes)} cause(s)"
    head += f" · exit {run.exit_code} · {run.seconds:.1f}s"
    lines = [pal.bold_white(head)]
    if d is not None:
        for _, info in d.fixed:
            lines.append(
                pal.bold_green("✓ fixed")
                + f"   {info['title'][:110]}  "
                + pal.gray(f"({info['count']} {what}(s))")
            )
        for c in d.new:
            lines.append(f"{pal.badge_red(' NEW ') if pal.enabled else '+ new'}   {c.title[:110]}")
        if not (d.fixed or d.new) and d.still:
            lines.append(
                pal.gray("no change since the last run: the same causes are still failing")
            )
    for i, c in enumerate(run.causes, 1):
        n = len(c.failures)
        unit = "error" if c.first.kind == "build" else "test"
        tag = ""
        if d is not None and c in d.still:
            tag = pal.gray("  (still failing)")
        lines += [
            "",
            pal.bold(f"{i}.")
            + " "
            + pal.bold_white(f"{n} {unit}(s)")
            + "  "
            + pal.red(c.title[:140])
            + tag,
        ]
        lines += _where(c.first, root, pal)
        detail = [x for x in c.first.message.split("\n")[1:] if x.strip()][:2]
        for x in detail:
            lines.append(f"   detail   {pal.gray(x.strip()[:120])}")
        if c.first.kind == "build":
            lines.append(f"   files    {_build_text(c)}")
        elif c.tests:
            lines.append(f"   tests    {_tests_text(c)}")
    if run.tail:
        lines += ["", pal.gray(f"(not recognised: the last {len(run.tail)} lines)"), *run.tail]
    if run.raw_path and (run.causes or run.tail):
        lines += [
            "",
            pal.gray(f"full output: {run.raw_path} · one failure in full: logsite agent --show N"),
        ]
    return lines


def run_json(run: Run, d: Delta | None) -> dict:
    return {
        "tool": run.tool,
        "exit_code": run.exit_code,
        "counts": run.counts,
        "causes": [
            {
                "id": c.id,
                "title": c.title,
                "count": len(c.failures),
                "kind": c.first.kind,
                "raised_at": c.first.raised_at,
                "raised_in": c.first.raised_in,
                "origin": c.first.origin,
                "tests": c.tests,
                "message": c.first.message,
            }
            for c in run.causes
        ],
        "delta": None
        if d is None
        else {
            "fixed": [{"id": cid, **info} for cid, info in d.fixed],
            "new": [c.id for c in d.new],
            "still_failing": [c.id for c in d.still],
            "fixed_tests": d.fixed_tests,
        },
        "raw_output": run.raw_path,
        "tail": run.tail,
    }


def last_key_file(root: Path) -> Path:
    return state_dir() / f"last-{hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]}"
