"""``logsite prompt``: one stored error as a case file for an AI assistant (or a colleague).

Everything comes from the error store, the project's code and its git history: what failed
and where, how often and since when, the release and the commits around its start, whether
it was fixed before, what the failing requests have in common, and real examples (values
masked when they were stored). No AI is called here: the output is text to paste.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import _dt

SAMPLE = 500  # occurrences read for what the failing requests share
CONTEXT = 4  # lines of code on each side of where it was raised
MAX_TRACEBACK_LINES = 60
# fields every log line has, or that say where it was logged: not about the request
NOISE = {
    "time", "timestamp", "@timestamp", "ts", "asctime", "created", "msecs", "relativeCreated",
    "msg", "message", "event", "level", "levelname", "levelno", "severity", "logger", "name",
    "pathname", "filename", "file", "lineno", "line", "funcName", "func", "module", "caller",
    "thread", "threadName", "process", "processName", "taskName", "pid", "hostname", "host",
    "stack_info", "exc_info", "exc_text", "traceback", "stack", "stacktrace", "stack_trace",
    "trace_id", "traceId", "span_id", "spanId", "request_id", "requestId", "service",
    "service_name", "release", "version", "env", "environment", "_truncated",
}  # fmt: skip


class NotFound(LookupError):
    """No stored group matches, or more than one does (``matches`` then lists them)."""

    def __init__(self, message: str, matches: list[dict] | None = None) -> None:
        super().__init__(message)
        self.matches = matches or []


def find_group(store, ref: str) -> dict:
    """The stored group for an id, an id prefix, or a full fingerprint."""
    ref = ref.strip()
    if not ref:
        raise NotFound("no error id given")
    group = store.group(ref[:8])  # an id is the fingerprint's first 8 characters
    if group is not None:
        return group
    matches = store.find(ref)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFound(f"no stored error with id {ref!r}")
    raise NotFound(f"{len(matches)} stored errors start with {ref!r}", matches)


def gather(cfg, store, ref: str, now: datetime | None = None) -> dict:
    """Everything known about one error, as plain data (``--json`` prints it as is)."""
    now = now or datetime.now(timezone.utc)
    group = find_group(store, ref)
    gid = group["id"]
    events = store.events(gid, limit=SAMPLE)
    hourly = store.hourly(gid, now - timedelta(days=7))
    by = store.breakdown(gid)
    first = _dt(group.get("first_seen"))
    where = group.get("raised_at") or group.get("logged_at")
    return {
        "group": group,
        "code": _code(cfg.root, where),
        "logged_code": _code(cfg.root, group.get("logged_at"), context=0)
        if group.get("logged_at") and group.get("logged_at") != where
        else None,
        "frequency": _frequency(hourly, now),
        "hourly": hourly,
        "releases": _tally(by, "release"),
        "services": _tally(by, "service"),
        "started": _started(cfg.root, first, where, by),
        "history": _history(cfg, group.get("fingerprint")),
        "shared": shared_fields([e.get("fields") for e in events]),
        "sampled": len(events),
        "examples": events[:3],
        "test": _test(cfg.root, gid),
        "url": _url(cfg, where),
    }


# --- the pieces --------------------------------------------------------------------------------


def _split(where: str | None) -> tuple[str, int] | None:
    if not where or ":" not in where:
        return None
    path, _, line = where.rpartition(":")
    return (path, int(line)) if line.isdigit() and path else None


def _code(root: Path, where: str | None, context: int = CONTEXT) -> dict | None:
    """The lines around ``path:line`` in the project, numbered, or None."""
    loc = _split(where)
    if loc is None:
        return None
    path, line = loc
    try:
        text = (Path(root) / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not 1 <= line <= len(text):
        return None
    start, end = max(1, line - context), min(len(text), line + context)
    return {"path": path, "line": line,
            "lines": [(n, text[n - 1]) for n in range(start, end + 1)]}  # fmt: skip


def _url(cfg, where: str | None) -> str | None:
    if _split(where) is None:
        return None
    from .cli import _code_links

    return _code_links(cfg)[1](where)


def _frequency(hourly: list[dict], now: datetime) -> dict:
    """Last 24 hours against the 24 before, and the busiest hour of the week."""
    day = (now - timedelta(hours=24)).astimezone(timezone.utc).strftime("%Y-%m-%d %H")
    two = (now - timedelta(hours=48)).astimezone(timezone.utc).strftime("%Y-%m-%d %H")
    last = sum(h["n"] for h in hourly if h["hour"] > day)
    before = sum(h["n"] for h in hourly if two < h["hour"] <= day)
    peak = max(hourly, key=lambda h: h["n"], default=None)
    return {"last_24h": last, "previous_24h": before, "week": sum(h["n"] for h in hourly),
            "peak_hour": peak}  # fmt: skip


def _tally(rows: list[dict], key: str) -> list[dict]:
    """``breakdown`` rows summed over one column, the most first."""
    out: dict[str, dict] = {}
    for r in rows:
        name = r.get(key)
        if not name:
            continue
        t = out.setdefault(name, {"name": name, "n": 0, "first_at": r["first_at"],
                                  "last_at": r["last_at"]})  # fmt: skip
        t["n"] += r["n"]
        t["first_at"] = min(t["first_at"], r["first_at"])
        t["last_at"] = max(t["last_at"], r["last_at"])
    return sorted(out.values(), key=lambda t: -t["n"])


def _started(root: Path, first: datetime | None, where: str | None, by: list[dict]) -> dict:
    """The release it was first seen in (from the logs, or the git tag at the time), and the
    commits that changed the code around where it's raised in the 3 days before."""
    from .git import line_history
    from .history import release_lookup

    out: dict = {"at": first.isoformat() if first else None, "release": None,
                 "tag": None, "commits": []}  # fmt: skip
    dated = [r for r in by if r.get("release") and r.get("first_at")]
    if dated:
        out["release"] = min(dated, key=lambda r: r["first_at"])["release"]
    if first is None:
        return out
    out["tag"] = release_lookup(root)(first)
    loc = _split(where)
    if loc is not None:
        path, line = loc
        commits = line_history(root, path, max(1, line - 5), line + 5,
                               since=first - timedelta(days=3), max_count=5)  # fmt: skip
        out["commits"] = [
            {"sha": c.sha[:8], "author": c.author, "date": c.date.isoformat(), "summary": c.summary}
            for c in commits
            if c.date <= first + timedelta(minutes=5)
        ]
    return out


def _history(cfg, fingerprint: str | None) -> dict | None:
    """What logsite's history remembers: fixed before, came back, how many times."""
    from .history import open_history

    if not fingerprint:
        return None
    h = open_history(cfg)
    if h is None:
        return None
    try:
        row = h.show(fingerprint)
    except Exception:  # an old or locked history file: leave it out
        row = None
    finally:
        h.close()
    if row is None:
        return None
    keep = ("status", "first_release", "resolved_at", "resolved_release", "fixed_by",
            "regressions", "reopened_at", "previous_fix")  # fmt: skip
    return {k: row.get(k) for k in keep}


def shared_fields(rows: list, min_share: float = 0.8) -> dict:
    """What the failing requests have in common: ``same`` lists the fields with one value in
    at least ``min_share`` of the occurrences that have them, ``varies`` the fields whose
    values differ a lot (probably not the cause)."""
    seen: dict[str, Counter] = {}
    total = 0
    for fields in rows:
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except ValueError:
                continue
        if not isinstance(fields, dict):
            continue
        total += 1
        for key, value in _flat(fields):
            if key in NOISE or key.split(".")[-1] in NOISE:
                continue
            seen.setdefault(key, Counter())[_short(value)] += 1
    same, varies = [], []
    for key, counts in seen.items():
        n = sum(counts.values())
        if n < 3 or n < total / 2:  # too few to say
            continue
        value, top = counts.most_common(1)[0]
        if top / n >= min_share:
            same.append({"field": key, "value": value, "count": top, "of": n})
        elif len(counts) >= 3:
            varies.append({"field": key, "values": len(counts), "of": n,
                           "top": [v for v, _ in counts.most_common(3)]})  # fmt: skip
    same.sort(key=lambda f: (-f["count"], f["field"]))
    varies.sort(key=lambda f: (-f["values"], f["field"]))
    return {"same": same[:10], "varies": varies[:6], "occurrences": total}


def _flat(d: dict, prefix: str = ""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and len(prefix) < 40:
            yield from _flat(v, key + ".")
        elif not isinstance(v, list | dict):
            yield key, v


def _short(value) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= 80 else text[:77] + "..."


LIBRARY = re.compile(
    r"[\\/](site-packages|dist-packages|node_modules|lib[\\/]python\d[^\\/]*|"
    r"usr[\\/]lib[\\/]jvm|go[\\/]src|go[\\/]pkg[\\/]mod)[\\/]"
)


def fold_library_frames(lines: list[str]) -> list[str]:
    """A Python traceback with each run of library frames (the standard library, installed
    packages) folded into one line: the frames in the project are what matter."""
    out: list[str] = []
    folded = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith('File "') and LIBRARY.search(line):
            folded += 1
            i += 1
            while (
                i < len(lines)
                and lines[i].startswith("    ")
                and not lines[i].lstrip().startswith('File "')
            ):  # its code and ^^^ markers
                i += 1
            continue
        if folded:
            out.append(f"  ... {folded} library frame(s)")
            folded = 0
        out.append(line)
        i += 1
    if folded:
        out.append(f"  ... {folded} library frame(s)")
    return out


def _test(root: Path, gid: str) -> str | None:
    """The reproduction test ``logsite repro --write-test`` wrote for this error, if any."""
    for folder in (Path(root) / "tests", Path(root)):
        path = folder / f"test_repro_{gid}.py"
        if path.is_file():
            return str(path.relative_to(root))
    return None


# --- the text ----------------------------------------------------------------------------------


def _when(value) -> str:
    at = _dt(value)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if at else "unknown"


def _lang(path: str) -> str:
    return {".py": "python", ".go": "go", ".java": "java", ".kt": "kotlin", ".ts": "typescript",
            ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx", ".rb": "ruby"}.get(
        Path(path).suffix, "")  # fmt: skip


def render(case: dict) -> str:
    g = case["group"]
    out = [f"# Production error: {g.get('title') or g.get('exc_type') or g['id']}", ""]
    out += ["I need help fixing an error from production. Everything below comes from our "
            "logs and code, gathered by logsite (values were masked when stored).", ""]  # fmt: skip

    out += ["## The error", ""]
    out.append(f"- **Error:** `{g.get('title')}`")
    if g.get("root_cause") and g["root_cause"] != g.get("title"):
        out.append(f"- **Root cause:** `{g['root_cause']}`")
    if g.get("raised_at"):
        fn = f" in `{g['raised_in']}()`" if g.get("raised_in") else ""
        link = f" ([source]({case['url']}))" if case.get("url") else ""
        out.append(f"- **Raised at:** `{g['raised_at']}`{fn}{link}")
    if g.get("logged_at") and g.get("logged_at") != g.get("raised_at"):
        out.append(f"- **Logged at:** `{g['logged_at']}`")
    if g.get("origin"):
        out.append(f"- **Comes from:** {g['origin']}")
    code = case.get("code")
    if code:
        width = len(str(code["lines"][-1][0]))
        body = "\n".join(f"{'>' if n == code['line'] else ' '} {n:>{width}} | {text}"
                         for n, text in code["lines"])  # fmt: skip
        out += ["", f"```{_lang(code['path'])}", f"# {code['path']}", body, "```"]
    out.append("")

    f, s = case["frequency"], case["started"]
    out += ["## How often", ""]
    out.append(f"- **{g.get('events', 0)}** times between {_when(g.get('first_seen'))} and "
               f"{_when(g.get('last_seen'))}")  # fmt: skip
    trend = ""
    if f["previous_24h"]:
        ratio = f["last_24h"] / f["previous_24h"]
        trend = " (rising)" if ratio >= 1.5 else " (falling)" if ratio <= 0.67 else " (steady)"
    elif f["last_24h"]:
        trend = " (none the day before)"
    if f["last_24h"] or f["previous_24h"]:
        out.append(f"- Last 24 hours: **{f['last_24h']}**, the 24 before: "
                   f"{f['previous_24h']}{trend}")  # fmt: skip
    else:
        out.append("- Not seen in the last 48 hours")
    if f.get("peak_hour") and f["peak_hour"]["n"] > 1:
        out.append(f"- Busiest hour this week: {f['peak_hour']['hour']}:00 UTC "
                   f"({f['peak_hour']['n']})")  # fmt: skip
    for label, rows in (("Services", case["services"]), ("Releases", case["releases"])):
        if rows:
            shown = ", ".join(f"`{r['name']}` ({r['n']})" for r in rows[:6])
            more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
            out.append(f"- {label}: {shown}{more}")
    out.append("")

    out += ["## When it started", ""]
    out.append(f"- First seen {_when(s['at'])}")
    if s.get("release"):
        out.append(f"- First release with it, from the logs: `{s['release']}`")
    if s.get("tag") and s.get("tag") != s.get("release"):
        out.append(f"- Latest git tag at the time: `{s['tag']}`")
    if s["commits"]:
        out.append(f"- Commits that changed the code around `{g.get('raised_at')}` in the 3 days "
                   "before it started:")  # fmt: skip
        out += [f"  - `{c['sha']}` {c['date'][:10]} {c['author']}: {c['summary']}"
                for c in s["commits"]]  # fmt: skip
    elif _split(g.get("raised_at")):
        out.append("- No commit changed the code around where it's raised in the 3 days before")
    out.append("")

    h = case.get("history")
    if h and (h.get("regressions") or h.get("fixed_by") or h.get("previous_fix")):
        out += ["## Has it happened before", ""]
        if h.get("regressions"):
            out.append(f"- It stopped and came back {h['regressions']} time(s); last back "
                       f"{_when(h.get('reopened_at'))}")  # fmt: skip
        fix = h.get("previous_fix") or {}
        commits = h.get("fixed_by") or fix.get("fixed_by") or []
        if commits:
            rel = h.get("resolved_release") or fix.get("release")
            out.append("- It went away" + (f" in `{rel}`" if rel else "") +
                       "; commits that probably fixed it then:")  # fmt: skip
            out += [f"  - `{c['sha']}` {c.get('date', '')[:10]} {c.get('author', '')}: "
                    f"{c.get('summary', '')}" for c in commits]  # fmt: skip
        out.append("")

    sh = case["shared"]
    if sh["same"] or sh["varies"]:
        out += [f"## What the failing requests have in common ({sh['occurrences']} looked at)", ""]
        out += [f"- `{x['field']}` = `{x['value']}` in {x['count']} of {x['of']}"
                for x in sh["same"]]  # fmt: skip
        if sh["varies"]:
            out.append("- Varies, so probably not the cause: " + ", ".join(
                f"`{x['field']}` ({x['values']} values)" for x in sh["varies"]))  # fmt: skip
        out += ["", "(Only failing requests are stored, so this can't say how normal traffic "
                "differs.)", ""]  # fmt: skip

    examples = case["examples"]
    if examples:
        out += ["## Examples", ""]
        for i, e in enumerate(examples):
            keys = ("service", "release", "trace_id")
            meta = [_when(e.get("at"))] + [f"{k} `{e[k]}`" for k in keys if e.get(k)]
            out.append(f"**{i + 1}.** {' · '.join(meta)}")
            out += ["", "```", e.get("message") or ""]
            fields = e.get("fields")
            if isinstance(fields, dict):
                fields = {k: v for k, v in fields.items() if k not in NOISE}
            if fields:
                text = fields if isinstance(fields, str) else json.dumps(fields, ensure_ascii=False)
                out.append(f"fields: {text}")
            out.append("```")
            if i == 0 and e.get("traceback"):
                lines = fold_library_frames(e["traceback"].splitlines())
                if len(lines) > MAX_TRACEBACK_LINES:
                    lines = ["..."] + lines[-MAX_TRACEBACK_LINES:]
                out += ["", "```", *lines, "```"]
            out.append("")

    out += ["## A test that reproduces it", ""]
    if case.get("test"):
        out.append(f"`{case['test']}` reproduces it (written by `logsite repro`); it should fail "
                   "before the fix and pass after.")  # fmt: skip
    else:
        out.append(f"None yet. `logsite repro {g['id']} --write-test` writes one from the "
                   "production values.")  # fmt: skip
    out.append("")

    out += ["## What I'd like", "",
            "1. Explain the root cause, pointing at the code.",
            "2. Fix it where it starts, not by catching or hiding the exception.",
            "3. Make the reproduction test pass (or write one), and keep it as a regression test.",
            "4. Say whether the fix needs anything beyond a deploy (data cleanup, a config "
            "change, a retry of failed requests).", ""]  # fmt: skip
    return "\n".join(out)
