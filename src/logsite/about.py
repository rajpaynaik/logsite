"""What production says about a piece of code: for an AI assistant (or a person) about to
change it.

For a file, a line or a range (a single line widens to the function around it), in one
answer: the errors raised there, logged there or passing through, with their history (back
after a fix, and what fixed it); the log calls there with their volume and monthly cost;
the commits that last touched it; and the logging problems the linter finds. Without a log
backend the production part is left out and the rest still works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .backends import TimeRange
from .config import Config
from .errors import AppFiles, ErrorGroup, group_json, summarize_errors
from .git import enclosing_block, line_history
from .index import DEFAULT_EXCLUDES, index_project
from .lint import lint_paths
from .models import Diagnostic, LogSite

WIDEN = 15  # lines on each side of a single line outside Python, where there's no function


@dataclass
class Span:
    path: str
    start: int
    end: int
    function: str | None = None  # the function a single line widened to, for Python

    def contains(self, location: str | None) -> bool:
        if not location:
            return False
        path, _, line = location.rpartition(":")
        return path == self.path and line.isdigit() and self.start <= int(line) <= self.end


@dataclass
class About:
    span: Span
    window_hours: float
    errors: list[tuple[str, ErrorGroup]] = field(default_factory=list)  # (relation, group)
    log_calls: list[dict] = field(default_factory=list)
    commits: list = field(default_factory=list)
    lint: list[Diagnostic] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # what couldn't be looked up, why


def parse_span(root: Path, target: str) -> Span:
    """``PATH``, ``PATH:LINE`` or ``PATH:START-END``, relative to the project root. A single
    Python line widens to the function around it; elsewhere to ±WIDEN lines."""
    path, sep, lines = target.rpartition(":")
    if not sep or not lines.replace("-", "").isdigit():
        path, lines = target, ""
    path = path.replace("\\", "/").removeprefix("./")
    file = root / path
    if not file.is_file():
        raise ValueError(f"no file {path} in {root}")
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"can't read {path}: {exc}") from None
    total = max(source.count("\n") + 1, 1)
    if not lines:
        return Span(path, 1, total)
    if "-" in lines:
        a, _, b = lines.partition("-")
        start, end = int(a or 1), int(b or total)
        if start > end:
            start, end = end, start
        return Span(path, start, min(end, total))
    line = int(lines)
    if path.endswith(".py"):
        start, end = enclosing_block(source, line)
        if (start, end) != (line, line):
            return Span(path, start, end, _function_name(source, start))
    return Span(path, max(1, line - WIDEN), min(total, line + WIDEN))


def _function_name(source: str, start: int) -> str | None:
    for text in source.splitlines()[start - 1 : start + 4]:
        text = text.strip()
        for kw in ("async def ", "def "):
            if text.startswith(kw):
                return text[len(kw) :].split("(", 1)[0]
    return None


def relation(g: ErrorGroup, span: Span) -> str | None:
    if span.contains(g.raised_at):
        return "raised here"
    if g.logged_at and span.contains(g.logged_at.id):
        return "logged here"
    if any(span.contains(f) for f in g.frames_at):
        return "passes through"
    return None


ORDER = {"raised here": 0, "logged here": 1, "passes through": 2}
ERROR_FIELDS = (
    "id", "title", "root_cause", "count", "previous", "trend", "new", "raised_at", "raised_in",
    "logged_at", "first_seen", "last_seen", "example_trace", "history",
)  # fmt: skip


def about(cfg: Config, span: Span, hours: float | None = None, backend=None) -> About:
    from . import history
    from .backends import from_config
    from .cost import monthly_costs

    hours = hours or cfg.window_hours
    out = About(span, hours)
    sites = index_project(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
    here: list[LogSite] = [s for s in sites if span.contains(s.id)]
    out.commits = line_history(cfg.root, span.path, span.start, span.end, max_count=5)
    out.lint = [
        d
        for d in lint_paths([cfg.root / span.path], cfg.lint, cfg.root)
        if span.start <= d.line <= span.end
    ]
    calls = {s.id: {"id": s.id, "level": s.level, "message": s.template} for s in here}
    out.log_calls = list(calls.values())
    if backend is None:
        try:
            backend = from_config(cfg.backend)
        except (ValueError, RuntimeError) as exc:
            out.unavailable.append(f"production data: bad [backend] config: {exc}")
            return out
    if backend is None:
        out.unavailable.append("production data: no [backend] configured")
        return out
    window = TimeRange.last(hours)
    try:
        usage = backend.usage_by_line(sites, window) if here else {}
        for c in monthly_costs(here, usage, hours, cfg.cost):
            calls[c.site.id].update(
                events_in_window=usage[c.site.id].count if c.site.id in usage else 0,
                events_per_month=round(c.events_per_month),
                bytes_per_month=round(c.bytes_per_month),
                cost_per_month=round(c.cost_per_month, 2) if cfg.cost.priced else None,
            )
    except Exception as exc:
        out.unavailable.append(f"log volume: couldn't query the {backend.name} backend: {exc}")
    try:
        app = AppFiles.scan(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
        fields = dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"])
        summary = summarize_errors(
            backend, sites, app, window, window.previous(), cfg.lint, cfg.leaks, 5000, fields
        )
        history.apply(cfg, summary.groups, window.start, window.end)
        found = [(relation(g, span), g) for g in summary.groups]
        out.errors = sorted(
            ((r, g) for r, g in found if r), key=lambda rg: (ORDER[rg[0]], -rg[1].count)
        )
    except Exception as exc:
        out.unavailable.append(f"errors: couldn't query the {backend.name} backend: {exc}")
    return out


# --- output ------------------------------------------------------------------------------------


def about_json(a: About, top: int = 10) -> dict:
    def error(r: str, g: ErrorGroup) -> dict:
        full = group_json(g)
        return {"relation": r, **{k: full[k] for k in ERROR_FIELDS}}

    return {
        "path": a.span.path,
        "lines": [a.span.start, a.span.end],
        "function": a.span.function,
        "window_hours": a.window_hours,
        "errors": [error(r, g) for r, g in a.errors[:top]],
        "errors_total": len(a.errors),
        "log_calls": a.log_calls,
        "recent_commits": [
            {
                "sha": c.short_sha,
                "date": c.date.date().isoformat(),
                "author": c.author,
                "summary": c.summary,
            }
            for c in a.commits
        ],  # fmt: skip
        "lint": [{"code": d.code, "line": d.line, "message": d.message} for d in a.lint],
        "unavailable": a.unavailable,
    }


def render(a: About, currency: str = "$") -> str:
    s = a.span
    head = f"{s.path}:{s.start}-{s.end}" + (f"  {s.function}()" if s.function else "")
    out = [head]
    window = f"{a.window_hours:g}h"
    if a.errors:
        out.append(f"  errors in the last {window}:")
        for r, g in a.errors[:10]:
            h = g.history
            trend = "back" if h is not None and h.regression else ("new" if g.is_new else "")
            line = f"    {g.count:>7,}  {g.title[:90]}  ({r}" + (f", {trend})" if trend else ")")
            out.append(line)
            if g.causes:
                out.append(f"             root cause: {g.causes[0][:100]}")
            if h is not None and h.regression:
                gone = f"gone after {h.resolved_release}" if h.resolved_release else "was gone"
                fix = "; ".join(f"{c['sha']} {c['summary']}" for c in h.fixed_by)
                out.append(
                    f"             ↩ came back: {gone}" + (f", fixed by {fix}" if fix else "")
                )
    elif not any(u.startswith("errors") or u.startswith("production") for u in a.unavailable):
        out.append(f"  no errors here in the last {window}")
    if a.log_calls:
        out.append("  log calls:")
        for c in a.log_calls:
            text = f"    {c['id']}  {c['level']}({c['message']!r})"
            if "events_per_month" in c:
                n = c["events_per_month"]
                text += (
                    f"  {n:,} lines/month"
                    if n or not c.get("events_in_window")
                    else "  <1 line/month"
                )
                if c.get("cost_per_month") is not None:
                    text += f", {currency}{c['cost_per_month']:,.2f}/month"
            out.append(text)
    if a.commits:
        out.append("  last changed:")
        out += [f"    {c.format()}" for c in a.commits]
    if a.lint:
        out.append("  logging problems:")
        out += [f"    {d.line}: {d.code} {d.message}" for d in a.lint]
    out += [f"  (no {u})" for u in a.unavailable]
    return "\n".join(out)
