"""A Claude Code PreToolUse hook: run the assistant's test and build commands through agent
mode, so it gets failures grouped by cause instead of the raw output.

``logsite hook claude`` reads the hook's JSON on stdin and, for a plain test or build
command (``pytest -q``, ``go test ./...``, ``mvn test``, ``npx jest``, ``npm test``, ``npx tsc``…,
optionally after ``cd DIR &&``), answers with the command rewritten to ``logsite agent --
<command>``. Anything else (pipes, redirects, chains, other commands) is left alone, so the
assistant always gets what it asked for. Agent mode keeps the exit code, keeps the full
output on disk, and prints the raw output when it can't do better.

LOGSITE_HOOK=off turns it off. An ``rtk`` prefix (from the RTK hook) is dropped: agent mode
needs the tool's own output.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# a command made only of these words is rewritten; the first word(s) pick the tool
TOOLS = [
    (("pytest",), None),
    (("py.test",), None),
    (("python", "-m", "pytest"), None),
    (("python3", "-m", "pytest"), None),
    (("uv", "run", "pytest"), None),
    (("go", "test"), None),
    (("go", "build"), None),
    (("go", "vet"), None),
    (("mvn",), {"test", "verify", "package", "install", "compile", "test-compile"}),
    (("./mvnw",), {"test", "verify", "package", "install", "compile", "test-compile"}),
    (("gradle",), {"test", "build", "check", "compileJava", "compileTestJava"}),
    (("./gradlew",), {"test", "build", "check", "compileJava", "compileTestJava"}),
    (("tsc",), None),
    (("npx", "tsc"), None),
    (("javac",), None),
    (("jest",), None),
    (("npx", "jest"), None),
    (("yarn", "jest"), None),
    (("vitest",), None),
    (("npx", "vitest"), None),
    (("yarn", "vitest"), None),
    (("pnpm", "vitest"), None),
    (("npm", "test"), None),
    (("npm", "t"), None),
    (("npm", "run", "test"), None),
    (("yarn", "test"), None),
    (("pnpm", "test"), None),
]
WATCH = {"--watch", "--watchAll", "watch", "dev", "-w"}  # never ends: leave it alone
SHELL = re.compile(r"[|;<>`]|&&|\|\||\$\(|&\s*$")  # pipes, chains, redirects, substitution
CD = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)\s*&&\s*")


def rewrite(command: str) -> str | None:
    """The command through agent mode, or None to leave it alone."""
    prefix = ""
    m = CD.match(command)
    if m:
        prefix, command = command[: m.end()], command[m.end() :]
    command = command.strip()
    if command.startswith("rtk "):
        command = command[4:].lstrip()
    if not command or SHELL.search(command) or command.startswith("logsite "):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if any("=" in w.split("/")[0] and not w.startswith("-") for w in words[:1]):
        return None  # VAR=value pytest: leave environment tricks alone
    if WATCH & set(words):
        return None
    for head, goals in TOOLS:
        if tuple(words[: len(head)]) != head:
            continue
        if goals is not None and not goals & set(words[len(head) :]):
            return None  # e.g. `mvn dependency:tree`: not a build or test
        return f"{prefix}logsite agent -- {command}"
    return None


def claude(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if os.environ.get("LOGSITE_HOOK", "").lower() in ("0", "off", "false", "no"):
        return 0
    try:
        event = json.load(stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never get in the way
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command")
    new = rewrite(command) if isinstance(command, str) else None
    if new is None:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecisionReason": "logsite agent mode: failures grouped by cause",
                "updatedInput": {**event["tool_input"], "command": new},
            }
        },
        stdout,
    )
    return 0
