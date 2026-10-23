"""Open a draft pull request with a reproduction test for each new production error.

Run by the logsite GitHub Action (``action.yml`` at the repo root) in a checkout of the
project, with logsite installed and the logs reachable (a ``[backend]`` in logsite.toml, or
``--from`` a file). For each new error group raised in the project's code, at most
``--max-prs`` of them:

1. skip it if a pull request for it exists already, open or closed (branch
   ``logsite/repro-<id>``), so a run never opens the same one twice;
2. ``logsite repro <id> --write-test`` on a new branch;
3. optionally run the test through ``logsite agent --expect`` to check it fails the way
   production did, and put the verdict in the pull request;
4. push the branch and open a draft pull request with what production logged.

Needs ``git`` and ``gh`` (``GH_TOKEN`` with contents and pull-requests write).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys

BRANCH_PREFIX = "logsite/repro-"
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def new_groups(data: dict, include_all: bool = False) -> list[dict]:
    """The groups to reproduce: new ones (or all, with ``include_all``) that were raised in
    the project's code, since a test can only fail through a line of the project."""
    groups = data.get("groups", [])
    return [g for g in groups if (include_all or g.get("new")) and g.get("raised_at")]


def has_pr(branch: str) -> bool:
    out = run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number"])
    return bool(json.loads(out.stdout or "[]"))


def body(group: dict, case_text: str, verdict: str | None) -> str:
    seen = (group.get("first_seen") or "")[:16].replace("T", " ")
    lines = [
        f"Production raised **{group['title']}** {group['count']:,} time(s) since {seen} UTC, "
        f"through `{group['raised_at']}`.",
        "",
        "This pull request adds a test that reproduces it, written by "
        "[logsite](https://github.com/rajpaynaik/logsite) from what production logged.",
        "",
    ]
    if verdict:
        lines += [f"**Check:** {verdict}", ""]
    lines += [
        "<details><summary>What production logged</summary>",
        "",
        "```",
        case_text.strip(),
        "```",
        "",
        "</details>",
        "",
        "Next: fill in any `TODO` in the test, fix the code until it passes, and push the fix "
        "to this branch.",
    ]
    return "\n".join(lines)


_ANSI = re.compile(r"\x1b\[[\d;]*m")
_WROTE = re.compile(r"^wrote (\S+)", re.M)


def open_pr(group: dict, base: str, source: list[str], test_command: str | None,
            dry_run: bool) -> str:  # fmt: skip
    gid = group["id"]
    branch = BRANCH_PREFIX + gid
    if has_pr(branch):
        return f"skip {gid}: a pull request exists already ({branch})"
    run(["git", "switch", "-C", branch, base])
    try:
        out = run(["logsite", "repro", gid, "--write-test", "--color", "never", *source],
                  check=False)  # fmt: skip
        text = _ANSI.sub("", out.stdout)
        wrote = _WROTE.search(_ANSI.sub("", out.stderr)) or _WROTE.search(text)  # on stderr
        if out.returncode != 0 or not wrote:
            return f"skip {gid}: no test written ({(out.stderr or out.stdout).strip()[-200:]})"
        test = wrote[1]
        verdict = None
        if test_command:
            check = run(["logsite", "agent", "--expect", gid, "--",
                         *shlex.split(test_command), test], check=False)  # fmt: skip
            verdict = next(
                (ln.strip() for ln in _ANSI.sub("", check.stdout).splitlines()
                 if ln.strip().startswith(("✓", "✗"))),
                None,
            )  # fmt: skip
        if dry_run:
            return f"would open {branch}: {group['title']} ({test})"
        run(["git", "add", test])
        run(["git", "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", "commit", "-q",
             "-m", f"Reproduce {group['title'][:60]} ({gid})"])  # fmt: skip
        run(["git", "push", "-q", "origin", branch])
        title = f"Reproduce: {group['title']}"
        title = title if len(title) <= 100 else title[:99] + "…"
        pr = run(["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
                  "--title", title, "--body", body(group, text, verdict)])  # fmt: skip
        return f"opened {pr.stdout.strip()}"
    finally:
        run(["git", "switch", "-q", base], check=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--from", dest="log_file", help="read the errors from this log file")
    p.add_argument("--hours", type=float, help="the window to read from the backend")
    p.add_argument("--max-prs", type=int, default=3)
    p.add_argument("--all", action="store_true", help="every error group, not only new ones")
    p.add_argument("--base", help="the branch to open pull requests against (default: current)")
    p.add_argument("--test-command", help="run the test with this, e.g. 'python -m pytest'")
    p.add_argument("--dry-run", action="store_true", help="write the tests, open nothing")
    args = p.parse_args(argv)

    source = ["--from", args.log_file] if args.log_file else []
    window = ["--hours", str(args.hours)] if args.hours and not args.log_file else []
    out = run(["logsite", "errors", "--format", "json", "--no-history", *source, *window],
              check=False)  # fmt: skip
    if out.returncode not in (0, 1):
        print(f"logsite errors failed: {out.stderr.strip()}", file=sys.stderr)
        return 1
    groups = new_groups(json.loads(out.stdout), args.all)
    if not groups:
        print("no new error groups raised in this project's code")
        return 0
    base = args.base or run(["git", "branch", "--show-current"]).stdout.strip()
    for g in groups[: args.max_prs]:
        print(open_pr(g, base, source, args.test_command, args.dry_run))
    if len(groups) > args.max_prs:
        print(f"{len(groups) - args.max_prs} more group(s); raise max-prs to open them too")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"logsite: {len(groups)} new error group(s) raised in this project's code\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
