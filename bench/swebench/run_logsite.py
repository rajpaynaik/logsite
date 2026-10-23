"""Run logsite alone (no AI) on the traceback of each shortlisted issue, at the issue's
commit, and score what it found against the real fix. Free: no model calls.

    python bench/swebench/run_logsite.py [instance_id ...]

Needs data/rows_*.json and the clones in repos/ (see README). Writes results/logsite.json,
and each issue's traceback and generated test under results/<instance_id>/.
"""

import ast
import json
import py_compile
import re
import subprocess
import sys
import time
from pathlib import Path

from select_issues import HERE, in_project, load_rows, package

from logsite.tracebacks import TRACEBACK_HEAD, parse_traceback

LOGSITE = Path(sys.executable).with_name("logsite")
RESULTS = HERE / "results"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")
_FILE = re.compile(r"^--- a/(\S+)")
NEAR = 3  # lines either side of a changed line that still count as "at the fix"


def traceback_text(issue: str) -> str:
    """The traceback in an issue, from its first header to the final exception line, without
    the markdown around it: what an error log would have carried."""
    lines = [ln for ln in issue.replace("\r\n", "\n").splitlines() if not ln.startswith("```")]
    start = next(i for i, ln in enumerate(lines) if TRACEBACK_HEAD in ln)
    final = parse_traceback(issue)[-1]
    name = final.type.rsplit(".", 1)[-1]
    end = max(
        (i for i in range(start, len(lines)) if name in lines[i] and not lines[i][:1].isspace()),
        default=len(lines) - 1,
    )
    return "\n".join(lines[start : end + 1]) + "\n"


def changed_lines(patch: str) -> dict[str, set[int]]:
    """The lines of each file a patch changes, numbered as before the fix."""
    out: dict[str, set[int]] = {}
    file, old = None, 0
    for ln in patch.splitlines():
        if m := _FILE.match(ln):
            file = m[1]
            out.setdefault(file, set())
        elif m := _HUNK.match(ln):
            old = int(m[1])
        elif file is None or ln.startswith(("+++", "---", "\\")):
            continue
        elif ln.startswith("-"):
            out[file].add(old)
            old += 1
        elif ln.startswith("+"):
            out[file].add(old)  # an insertion before this line
        else:
            old += 1
    return out


def at_fix(site: str | None, changed: dict[str, set[int]]) -> tuple[bool, bool]:
    """Whether a "path:line" is in a file the fix changed, and within NEAR lines of a change."""
    if not site or ":" not in site:
        return False, False
    path, line = site.rsplit(":", 1)
    for file, lines in changed.items():
        if file == path or file.endswith("/" + path):
            return True, any(abs(int(line) - n) <= NEAR for n in lines)
    return False, False


def in_fix_function(repo_dir: Path, site: str | None, func: str | None,
                    changed: dict[str, set[int]]) -> bool:  # fmt: skip
    """Whether the function a frame is in, as it is at the issue's commit, has a line the fix
    changed. Robust to line drift: reporters ran other versions than the issue's commit."""
    if not site or not func or ":" not in site:
        return False
    path = site.rsplit(":", 1)[0]
    lines = next((v for f, v in changed.items() if f == path or f.endswith("/" + path)), None)
    file = next((p for p in (repo_dir / path, repo_dir / "lib" / path) if p.exists()), None)
    if not lines or file is None:
        return False
    try:
        tree = ast.parse(file.read_text())
    except (SyntaxError, ValueError):
        return False
    name = func.rsplit(".", 1)[-1]
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
        and any(node.lineno <= n <= node.end_lineno for n in lines)
        for node in ast.walk(tree)
    )


def checkout(repo_dir: Path, sha: str) -> None:
    subprocess.run(["git", "-C", repo_dir, "clean", "-fdq"], check=True)
    subprocess.run(["git", "-C", repo_dir, "checkout", "-fq", sha], check=True)


def run(row: dict) -> dict:
    iid = row["instance_id"]
    out_dir = RESULTS / iid
    out_dir.mkdir(parents=True, exist_ok=True)
    tb = out_dir / "traceback.txt"
    tb.write_text(traceback_text(row["problem_statement"]))
    test = out_dir / f"test_repro_{iid.replace('-', '_')}.py"
    test.unlink(missing_ok=True)

    repo_dir = HERE / "repos" / row["repo"].split("/")[1]
    checkout(repo_dir, row["base_commit"])
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            LOGSITE,
            "repro",
            "--from",
            tb,
            "--format",
            "json",
            "--write-test",
            test,
            "--color",
            "never",
        ],  # fmt: skip
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    seconds = round(time.monotonic() - t0, 1)
    result = {"instance_id": iid, "repo": row["repo"], "seconds": seconds}
    try:
        found = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return result | {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-300:]}

    changed = changed_lines(row["patch"])
    # where the raising line is at the issue's commit: logsite relocates a line that moved
    # since the reporter's version by the code the traceback shows for it
    raised = found["signature"].get("local_at") or found["signature"].get("raised_at")
    entry = (found.get("entry") or {}).get("at")
    frames = found.get("frames") or []
    expected = parse_traceback(row["problem_statement"])[-1].type.rsplit(".", 1)[-1]
    compiles = False
    if test.exists():
        try:
            py_compile.compile(str(test), doraise=True)
            compiles = True
        except py_compile.PyCompileError:
            pass
    file_hit, line_hit = at_fix(raised, changed)
    pkg = package(row["repo"])
    on_stack = [
        (p, f) for f in parse_traceback(tb.read_text())[-1].frames if (p := in_project(f.file, pkg))
    ]
    return result | {
        "ok": True,
        "error_type": found["signature"]["exc_type"],
        "right_error": found["signature"]["exc_type"].rsplit(".", 1)[-1] == expected,
        "raised_at": raised,
        "raised_in_fix_file": file_hit,
        "raised_at_fix": line_hit,
        "raised_in": found["signature"].get("raised_in"),
        "raised_in_fix_function": in_fix_function(
            repo_dir, raised, found["signature"].get("raised_in"), changed
        ),
        "fix_file_in_frames": any(at_fix(f, changed)[0] for f in frames),
        "fix_line_in_frames": any(at_fix(f, changed)[1] for f in frames),
        "fix_function_on_stack": any(
            in_fix_function(repo_dir, f"{p}:{f.line}", f.function, changed) for p, f in on_stack
        ),
        "entry": entry,
        "entry_function": (found.get("entry") or {}).get("function"),
        "fake": (found.get("fake") or {}).get("target"),
        "caller": (found.get("caller") or {}).get("code"),
        "missing": found.get("missing") or [],
        "test_written": test.exists(),
        "test_compiles": compiles,
        "todos": test.read_text().count("TODO") if test.exists() else None,
    }


def main() -> None:
    rows = {r["instance_id"]: r for r in load_rows()}
    shortlist = [c["instance_id"] for c in json.loads((HERE / "shortlist.json").read_text())]
    ids = sys.argv[1:] or shortlist
    results = []
    for iid in ids:
        r = run(rows[iid])
        results.append(r)
        if r["ok"]:
            mark = "✓" if r["raised_in_fix_function"] else "~" if r["raised_in_fix_file"] else "✗"
            print(f"{mark} {iid:34} {r['raised_at'] or '-':48} entry {r['entry_function']}")
        else:
            print(f"! {iid:34} {r['error']}")
    RESULTS.mkdir(exist_ok=True)
    if not sys.argv[1:]:
        (RESULTS / "logsite.json").write_text(json.dumps(results, indent=1) + "\n")
    summarize(results)


def summarize(results: list[dict]) -> None:
    ok = [r for r in results if r["ok"]]

    def n(key: str) -> str:
        return f"{sum(bool(r.get(key)) for r in ok):2}/{len(results)}"

    print()
    print(f"read the traceback        {len(ok):2}/{len(results)}")
    print(f"right exception           {n('right_error')}")
    print(f"raised at: the fix's file {n('raised_in_fix_file')}")
    print(f"raised in: the fix's func {n('raised_in_fix_function')}")
    print(f"raised at: the fix's line {n('raised_at_fix')}   (within {NEAR} lines)")
    print(f"fix's file on the trace   {n('fix_file_in_frames')}")
    print(f"fix's line on the trace   {n('fix_line_in_frames')}")
    print(f"fix's func on the trace   {n('fix_function_on_stack')}")
    print(f"entry function found      {n('entry')}")
    print(f"caller's line found       {n('caller')}")
    print(f"test written / compiles   {n('test_written')} / {n('test_compiles')}")


if __name__ == "__main__":
    main()
