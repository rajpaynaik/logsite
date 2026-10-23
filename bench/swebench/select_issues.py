"""Pick the SWE-bench Verified issues that look like production errors: a traceback raised
inside the project's own code. Writes candidates.json, one entry per issue, and prints a
summary by repo.

    python bench/swebench/select_issues.py   # reads data/rows_*.json (see README)
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

from logsite.tracebacks import TRACEBACK_HEAD, parse_traceback

HERE = Path(__file__).parent
PER_REPO = 8  # so the shortlist isn't mostly Django
_PATCHED = re.compile(r"^\+\+\+ b/(\S+)", re.M)


def load_rows() -> list[dict]:
    files = sorted((HERE / "data").glob("rows_*.json"))
    if not files:
        sys.exit("no data/rows_*.json: download them first (see README)")
    return [r["row"] for f in files for r in json.loads(f.read_text())["rows"]]


def package(repo: str) -> str:
    """The import name a repo's frames run through: django/django -> django,
    scikit-learn/scikit-learn -> sklearn, psf/requests -> requests."""
    name = repo.split("/")[1]
    return {"scikit-learn": "sklearn", "pylint": "pylint", "matplotlib": "matplotlib"}.get(
        name, name.replace("-", "_")
    )


def in_project(file: str, pkg: str) -> str | None:
    """The repo path of a frame in the project's own package, or None."""
    path = file.replace("\\", "/")
    marker = f"/{pkg}/"
    if marker in path:
        return pkg + "/" + path.rsplit(marker, 1)[1]
    if path.startswith(pkg + "/"):
        return path
    return None


def candidate(row: dict) -> dict | None:
    text = row["problem_statement"]
    if TRACEBACK_HEAD not in text:
        return None
    chain = parse_traceback(text)
    if not chain:
        return None
    pkg = package(row["repo"])
    final = chain[-1]
    own = [(p, f) for f in final.frames if (p := in_project(f.file, pkg))]
    if not own:
        return None  # raised in the user's script or another library
    patched = [p for p in _PATCHED.findall(row["patch"]) if not p.startswith("tests/")]
    traced = {p for p, _ in own}
    deepest_path, deepest = own[-1]

    def fixed(path: str) -> bool:  # matplotlib's source is under lib/matplotlib/
        return any(q == path or q.endswith("/" + path) for q in patched)

    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "created_at": row["created_at"],
        "difficulty": row["difficulty"],
        "error": final.title[:120],
        "chain": len(chain),
        "frames": len(final.frames),
        "own_frames": len(own),
        "deepest_own": f"{deepest_path}:{deepest.line} {deepest.function}",
        "patched": patched,
        "fix_in_trace": any(fixed(p) for p in traced),
        "fix_in_deepest": fixed(deepest_path),
    }


def pick(found: list[dict]) -> list[dict]:
    """The issues whose fix is in a traceback file, at most PER_REPO per repo, different
    exception types first."""
    out = []
    for repo in sorted({c["repo"] for c in found}):
        mine = [c for c in found if c["repo"] == repo and c["fix_in_trace"]]
        seen: set[str] = set()
        first = []
        for c in mine:
            kind = c["error"].split(":")[0]
            (first if kind not in seen else []).append(c)
            seen.add(kind)
        rest = [c for c in mine if c not in first]
        out += (first + rest)[:PER_REPO]
    return out


def main() -> None:
    rows = load_rows()
    with_tb = [r for r in rows if TRACEBACK_HEAD in r["problem_statement"]]
    found = [c for r in rows if (c := candidate(r))]
    (HERE / "candidates.json").write_text(json.dumps(found, indent=1) + "\n")

    in_trace = sum(c["fix_in_trace"] for c in found)
    in_deepest = sum(c["fix_in_deepest"] for c in found)
    print(f"{len(rows)} issues · {len(with_tb)} with a traceback · {len(found)} in the project")
    print(f"fix touches a file in the traceback: {in_trace}")
    print(f"fix touches the deepest project frame's file: {in_deepest}")
    shortlist = pick(found)
    (HERE / "shortlist.json").write_text(json.dumps(shortlist, indent=1) + "\n")
    print(f"shortlist: {len(shortlist)} (fix in the traceback, at most {PER_REPO} per repo)")
    print()
    by_repo = Counter(c["repo"] for c in found)
    hit = Counter(c["repo"] for c in found if c["fix_in_trace"])
    for repo, n in by_repo.most_common():
        print(f"  {repo:32} {n:3}  fix in trace {hit[repo]}")


if __name__ == "__main__":
    main()
