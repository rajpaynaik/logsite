"""Benchmark: reproduce and fix a production error, with and without logsite.

Each scenario is examples/bookstore plus the incident.log of one planted bug. Each arm gets a
fresh copy and the same task:

- ``logsite``: no AI. ``logsite repro --write-test``, unedited. Free.
- ``claude``: Claude Code with the log file and the code, and nothing else.
- ``claude+logsite``: the same, plus logsite (its MCP server and the ``logsite`` command).

The AI arms run ``claude -p`` with the user's own settings, hooks, plugins and memory left
out, and cost usage: they only run with ``--run``. Scoring is deterministic:

- **fail-to-pass**: the arm's new tests fail on the buggy code and pass on the reference fix
  (the check SWE-bench uses), and logsite's verdict on them (``agent --expect``);
- **fixed**: the hidden test for the bug passes on the arm's code, and so do the service's
  own tests;
- tokens, cost and time, as ``claude -p`` reports them.

    python bench/repro/run.py --check                 # the scenarios themselves are sound
    python bench/repro/run.py                         # the free arm only, and what --run would do
    python bench/repro/run.py --run --trials 3        # every arm, 3 times each (costs usage)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
APP = REPO / "examples" / "bookstore"
SCENARIOS = json.loads((HERE / "scenarios.json").read_text())
OWN_TESTS = ["tests/test_catalog.py", "tests/test_inventory.py", "tests/test_shipping.py"]
PROMPT = (
    "Users report failed requests on the bookstore service. The production log is "
    "incident.log in this directory. Find the error behind most of the failures, write a "
    "pytest test in tests/ that reproduces it, then fix the code so the test passes. Run "
    "tests with `python -m pytest`. Don't change incident.log or settings.json."
)
AI_ARMS = ("claude", "claude+logsite")
BASE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(python -m pytest:*)",
              "Bash(python:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(head:*)",
              "Bash(tail:*)", "Bash(wc:*)"]  # fmt: skip
LOGSITE_TOOLS = ["mcp__logsite", "Bash(logsite:*)"]


def python() -> str:
    return sys.executable  # has pytest and logsite


def logsite_bin() -> str:
    found = shutil.which("logsite", path=str(Path(sys.executable).parent))
    if not found:
        sys.exit("run this with the Python that has logsite installed (pip install -e .)")
    return found


def sh(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 300):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def ensure_logs(scenarios) -> None:
    """The hard scenarios' logs aren't in git (~30 MB): write the missing ones."""
    missing = [s for s in scenarios if not (HERE / "scenarios" / s / "incident.log").is_file()]
    if missing:
        print(f"writing the logs of {', '.join(missing)} ...", file=sys.stderr, flush=True)
        args = [arg for s in missing for arg in ("--scenario", s)]
        subprocess.run([python(), str(HERE / "make_incidents.py"), *args], check=True,
                       stdout=subprocess.DEVNULL)  # fmt: skip


def workspace(dest: Path, scenario: str, fixed: bool = False) -> Path:
    """A copy of the service with the scenario's log, as a git repo (to see what changed)."""
    shutil.copytree(APP, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.copy(HERE / "scenarios" / scenario / "incident.log", dest / "incident.log")
    if fixed:
        apply_fix(dest, scenario)
    sh(["git", "init", "-q"], dest)
    sh(["git", "add", "-A"], dest)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=bench@example.com", "commit", "-qm",
        "start"], dest)  # fmt: skip
    return dest


def apply_fix(dest: Path, scenario: str) -> None:
    fix = SCENARIOS[scenario]["fix"]
    path = dest / fix["file"]
    text = path.read_text()
    assert fix["old"] in text, f"{scenario}: the reference fix no longer applies"
    path.write_text(text.replace(fix["old"], fix["new"]))


def shims(dest: Path, with_logsite: bool) -> dict:
    """PATH for an arm: ``python`` (with pytest), and ``logsite`` only where it's allowed."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "python").symlink_to(python())
    if with_logsite:
        (dest / "logsite").symlink_to(logsite_bin())
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRTUAL_ENV")}
    env["PATH"] = os.pathsep.join([str(dest), "/usr/bin", "/bin", "/usr/local/bin",
                                   "/opt/homebrew/bin"])  # fmt: skip
    return env


def pytest_ok(ws: Path, files: list[str], env: dict | None = None) -> bool:
    if not files:
        return False
    r = sh([python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", *files], ws, env)
    return r.returncode == 0


# --- the arms ---------------------------------------------------------------------------------


def run_logsite(ws: Path, env: dict, group: str | None = None) -> dict:
    """``group``: the scenario names one when it isn't the one logsite would pick (a quiet
    error the report describes), as the person would after reading ``logsite errors``."""
    start = time.monotonic()
    cmd = [logsite_bin(), "repro", *([group] if group else []), "--from", "incident.log",
           "--write-test"]  # fmt: skip
    r = sh(cmd, ws, env)
    return {"ok": r.returncode == 0, "seconds": round(time.monotonic() - start, 1),
            "output": r.stdout[-2000:] + r.stderr[-1000:]}  # fmt: skip


def run_claude(
    ws: Path,
    env: dict,
    with_logsite: bool,
    model: str | None,
    budget: float,
    out: Path,
    prompt: str = PROMPT,
) -> dict:
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           "--setting-sources", "project", "--strict-mcp-config", "--no-session-persistence",
           "--max-budget-usd", str(budget), "--allowedTools", *BASE_TOOLS]  # fmt: skip
    if with_logsite:
        cmd += LOGSITE_TOOLS
        config = out / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"logsite": {"command": logsite_bin(),
                                                                 "args": ["mcp"]}}}))  # fmt: skip
        cmd += ["--mcp-config", str(config)]
    if model:
        cmd += ["--model", model]
    start = time.monotonic()
    try:
        r = sh(cmd, ws, env, timeout=1200)
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": 1200, "error": "timed out after 20 min"}
    (out / "claude.json").write_text(r.stdout)
    try:
        result = json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "seconds": round(time.monotonic() - start), "error": r.stderr[-2000:]}
    usage = result.get("usage") or {}
    return {
        "ok": not result.get("is_error"),
        "seconds": round(result.get("duration_ms", 0) / 1000),
        "turns": result.get("num_turns"),
        "cost_usd": result.get("total_cost_usd"),
        "tokens": sum(
            usage.get(k) or 0
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        ),  # fmt: skip
        "summary": (result.get("result") or "")[-1500:],
    }


# --- scoring --------------------------------------------------------------------------------------


def new_tests(ws: Path) -> list[str]:
    """Test files the arm added or changed (the service's own tests count if it changed them)."""
    r = sh(["git", "status", "--porcelain", "--untracked-files=all"], ws)
    files = [line[3:].strip() for line in r.stdout.splitlines()]
    return sorted(f for f in files if f.startswith("tests/") and f.endswith(".py")
                  and Path(f).name.startswith("test"))  # fmt: skip


def score(ws: Path, scenario: str, tmp: Path) -> dict:
    tests = new_tests(ws)
    out: dict = {"tests": tests}
    # fail-to-pass, on clean copies of the buggy and the fixed code
    buggy = workspace(tmp / "buggy", scenario)
    fixed = workspace(tmp / "fixed", scenario, fixed=True)
    for copy in (buggy, fixed):
        for t in tests:
            (copy / t).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ws / t, copy / t)
    out["fails_on_bug"] = bool(tests) and not pytest_ok(buggy, tests)
    out["passes_on_fix"] = pytest_ok(fixed, tests)
    out["fail_to_pass"] = out["fails_on_bug"] and out["passes_on_fix"]
    # logsite's verdict on the same tests, against the buggy code
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp / "cache")}
    group = SCENARIOS[scenario].get("group")
    cmd = [logsite_bin(), "repro", *([group] if group else []), "--from", "incident.log",
           "--format", "json"]  # fmt: skip
    r = sh(cmd, buggy, env)
    case = json.loads(r.stdout)["id"] if r.returncode == 0 else None
    verdict = None
    if case and tests:
        r = sh([logsite_bin(), "agent", "--expect", case, "--format", "json", "--", python(),
                "-m", "pytest", "-p", "no:cacheprovider", *tests], buggy, env)  # fmt: skip
        try:
            verdict = json.loads(r.stdout).get("expect", {}).get("status")
        except ValueError:
            verdict = None
    out["logsite_verdict"] = verdict
    # the fix, on the arm's own code
    oracle = HERE / SCENARIOS[scenario]["oracle"]
    shutil.copy(oracle, ws / "tests" / "_hidden_oracle_test.py")
    out["fixed"] = pytest_ok(ws, ["tests/_hidden_oracle_test.py"])
    (ws / "tests" / "_hidden_oracle_test.py").unlink()
    out["own_tests_pass"] = pytest_ok(ws, OWN_TESTS)
    out["its_tests_pass"] = pytest_ok(ws, tests)
    return out


# --- driving it -----------------------------------------------------------------------------------


def check() -> int:
    """The oracles fail on the bugs and pass on the reference fixes; the service's own tests
    pass on both."""
    bad = 0
    ensure_logs(SCENARIOS)
    with tempfile.TemporaryDirectory() as tmp:
        for scenario, spec in SCENARIOS.items():
            for fixed in (False, True):
                ws = workspace(Path(tmp) / f"{scenario}-{fixed}", scenario, fixed=fixed)
                shutil.copy(HERE / spec["oracle"], ws / "tests" / "_oracle_test.py")
                oracle = pytest_ok(ws, ["tests/_oracle_test.py"])
                own = pytest_ok(ws, OWN_TESTS)
                good = oracle == fixed and own
                bad += not good
                state = "fixed" if fixed else "buggy"
                print(
                    f"{'ok ' if good else 'BAD'} {scenario:<18} {state}: oracle "
                    f"{'passes' if oracle else 'fails'}, own tests {'pass' if own else 'FAIL'}"
                )
    return int(bad > 0)


def table(rows: list[dict]) -> str:
    head = ("| arm | scenario | fail-to-pass | logsite verdict | fixed | own tests | tokens | "
            "cost | time |\n|---|---|---|---|---|---|---|---|---|")  # fmt: skip
    lines = [head]
    for r in rows:
        s, a = r["score"], r["arm_result"]
        cost = f"${a['cost_usd']:.2f}" if a.get("cost_usd") is not None else "–"
        tokens = f"{a['tokens']:,}" if a.get("tokens") else "–"
        lines.append(
            f"| {r['arm']} | {r['scenario']} | {'✓' if s['fail_to_pass'] else '✗'} | "
            f"{s['logsite_verdict'] or '–'} | {'✓' if s['fixed'] else '✗'} | "
            f"{'✓' if s['own_tests_pass'] else '✗'} | {tokens} | {cost} | {a.get('seconds')}s |"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--check", action="store_true", help="check the scenarios, run nothing else")
    p.add_argument("--run", action="store_true", help="also run the AI arms (costs usage)")
    p.add_argument("--arm", action="append", choices=["logsite", *AI_ARMS])
    p.add_argument("--scenario", action="append", choices=list(SCENARIOS))
    p.add_argument("--hard", action="store_true", help="the hard scenarios only")
    p.add_argument("--easy", action="store_true", help="the easy scenarios only")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--model", help="for the AI arms, e.g. sonnet or opus (default: claude's)")
    p.add_argument("--budget", type=float, default=2.0, help="max $ per claude run (2.00)")
    p.add_argument("--out", type=Path, default=HERE / "results")
    args = p.parse_args()
    if args.check:
        return check()
    arms = args.arm or (["logsite", *AI_ARMS] if args.run else ["logsite"])
    if any(a in AI_ARMS for a in arms) and not args.run:
        sys.exit("the claude arms cost usage: add --run to run them")
    scenarios = args.scenario or [
        s for s, spec in SCENARIOS.items()
        if (args.hard and spec.get("hard")) or (args.easy and not spec.get("hard"))
        or not (args.hard or args.easy)
    ]  # fmt: skip
    ensure_logs(scenarios)
    runs = len([a for a in arms if a in AI_ARMS]) * len(scenarios) * args.trials
    if runs:
        print(f"{runs} claude -p run(s), up to 20 min and ${args.budget:.2f} each", file=sys.stderr)
    elif not args.arm:
        print(f"the AI arms ({', '.join(AI_ARMS)}) need --run: "
              f"{2 * len(scenarios) * args.trials} claude -p run(s)", file=sys.stderr)  # fmt: skip
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = args.out / stamp
    rows = []
    for trial in range(1, args.trials + 1):
        for scenario in scenarios:
            for arm in arms:
                if arm == "logsite" and trial > 1:
                    continue  # deterministic: once is enough
                out = root / f"{arm}-{scenario}-{trial}"
                ws = workspace(out / "ws", scenario)
                env = shims(out / "bin", with_logsite=arm != "claude")
                env["XDG_CACHE_HOME"] = str(out / "cache")
                print(f"{arm} · {scenario} · trial {trial} ...", file=sys.stderr, flush=True)
                spec = SCENARIOS[scenario]
                if arm == "logsite":
                    result = run_logsite(ws, env, spec.get("group"))
                else:
                    result = run_claude(ws, env, arm == "claude+logsite", args.model,
                                        args.budget, out, spec.get("prompt", PROMPT))  # fmt: skip
                with tempfile.TemporaryDirectory() as tmp:
                    s = score(ws, scenario, Path(tmp))
                rows.append({"arm": arm, "scenario": scenario, "trial": trial,
                             "arm_result": result, "score": s})  # fmt: skip
                (out / "score.json").write_text(json.dumps(rows[-1], indent=2))
    md = table(rows)
    root.mkdir(parents=True, exist_ok=True)
    (root / "results.json").write_text(json.dumps(rows, indent=2))
    (root / "results.md").write_text(md + "\n")
    print(md)
    print(f"\n{root / 'results.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
