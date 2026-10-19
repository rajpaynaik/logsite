"""Terminal layout for ``logsite errors``: an overview table, then a card per error group.

Colours carry meaning and are the same everywhere:

- red badge: new · yellow badge: spiking · gray: steady, labels, framework noise
- bold red: the cause (root cause, the root of the chain, exception lines in traces)
- underlined cyan: your code (where it was raised, your frames, your functions)
- blue: your log call (where it was logged) · bright green: your source line
- magenta: a third-party library · yellow: a command to run
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .errors import AppFiles, ErrorGroup, ErrorSummary
from .term import Palette, source_line
from .tracebacks import is_exception_line, line_frame

_PACKAGE = re.compile(r"(?:\b[a-z_$][\w$]*\.)+(?=[A-Z])")  # "java.net." before a class name
LABEL_WIDTH = 10  # the same in every card, so they line up


def short_class(text: str) -> str:
    """``java.net.SocketTimeoutException: Read timed out`` -> ``SocketTimeoutException: ...``"""
    return _PACKAGE.sub("", text, count=1) if text else text


def short_place(where: str | None) -> str:
    """``src/main/java/com/x/CarrierApiClient.java:58`` -> ``CarrierApiClient.java:58``"""
    return where.rsplit("/", 1)[-1] if where else "—"


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def _when(ts: datetime | None) -> str:
    if ts is None:
        return "?"
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return f"{ts:%Y-%m-%d %H:%M}"


def _span(first: datetime | None, last: datetime | None) -> str:
    if first is None:
        return ""
    start, end = _when(first), _when(last)
    if end[:10] == start[:10]:
        end = end[11:]  # same day: just the time
    text = f"{start} → {end} UTC"
    if last is not None and last > first:
        minutes = (last - first).total_seconds() / 60
        text += f"  ({minutes / 60:.1f}h)" if minutes >= 90 else f"  ({minutes:.0f} min)"
    return text


def is_regression(g: ErrorGroup) -> bool:
    return bool(g.history is not None and g.history.regression)


def trend_of(g: ErrorGroup) -> str:
    if is_regression(g):
        return "back"  # fixed before, and back
    return "" if g.trend is None else ("new" if g.is_new else g.trend)


def _badge_text(g: ErrorGroup, pal: Palette) -> str:
    """`` NEW `` / `` BACK `` as a coloured badge; plain ``new`` without colours."""
    trend = trend_of(g)
    if not trend or not pal.enabled:
        return trend
    return f" {trend.upper() if g.is_new or is_regression(g) else trend} "


def _day(ts) -> str:
    return f"{ts.day} {ts:%b %Y}" if ts else "?"


TRAFFIC_NOTE = 0.2  # say what traffic did when it moved more than 20%


def trend_rows(g: ErrorGroup, pal: Palette) -> list[tuple[str, str]]:
    """When traffic changed, the error count's trend alone misleads: say what the rate did."""
    ratio, rate = g.traffic_ratio, g.rate_ratio
    if rate is None or ratio is None or abs(ratio - 1) < TRAFFIC_NOTE:
        return []
    text = f"{g.count / g.previous:.1f}x errors at {ratio:.1f}x traffic: the rate is {rate:.1f}x"
    if rate < 1.3:
        text += ", it grew with traffic"
    return [("Trend", pal.white(text))]


def history_rows(g: ErrorGroup, pal: Palette) -> list[tuple[str, str]]:
    """What the history store knows: back after a fix, first time ever, or how long it's
    been around."""
    h = g.history
    if h is None:
        return []
    if h.regression:
        text = pal.bold_red("↩ came back") + pal.gray(
            f" · seen {_day(h.first_seen)} → {_day(h.resolved_at)}"
        )
        if h.resolved_release:
            text += pal.gray(", gone after ") + pal.bold_yellow(h.resolved_release)
        if h.regressions:
            text += pal.gray(f" · came back {h.regressions} time(s) before")
        rows = [("History", text)]
        for i, c in enumerate(h.fixed_by):
            line = (
                f"{pal.yellow(c['sha'])} {pal.gray(c['date'][:10])} {c['author']}: {c['summary']}"
            )
            rows.append(("Fixed by" if i == 0 else "", line))
        return rows
    if h.new_ever:
        since = pal.gray(f"  (history since {_day(h.watching_since)})")
        return [("History", pal.red("first time ever") + since)]
    if h.known and h.first_seen:
        text = f"since {_day(h.first_seen)}"
        if h.first_release:
            text += f" ({h.first_release})"
        text += f" · {h.total:,} errors on {h.days_seen} day(s) before this"
        return [("History", pal.gray(text))]
    return []


class Style:
    """The meaning of each colour, in one place."""

    def __init__(self, pal: Palette, spiking: Callable[[ErrorGroup], bool]) -> None:
        self.pal = pal
        self.spiking = spiking

    def kind(self, g: ErrorGroup) -> str:
        if g.is_new or is_regression(g):
            return "new"
        return "spike" if self.spiking(g) else "steady"

    def badge(self, g: ErrorGroup, text: str | None = None) -> str:
        """`` NEW `` / `` 8.0x `` / `` 1.0x ``: coloured by what the trend means."""
        paint = {"new": self.pal.badge_red, "spike": self.pal.badge_yellow}.get(
            self.kind(g), self.pal.gray
        )
        return paint(_badge_text(g, self.pal) if text is None else text)

    def bar(self, g: ErrorGroup, text: str) -> str:
        paint = {"new": self.pal.red, "spike": self.pal.yellow}.get(self.kind(g), self.pal.gray)
        return paint(text)

    def path(self, steps: list[str], origin: str | None) -> str:
        """Your functions in cyan, the library it ended in magenta, arrows gray."""
        arrow = self.pal.gray(" › ")
        ends_in_library = bool(origin and steps and steps[-1] == origin)
        mine = steps[:-1] if ends_in_library else steps
        parts = [self.pal.cyan(s) for s in mine]
        if ends_in_library:
            parts.append(self.pal.magenta(origin or ""))
        return arrow.join(parts)

    def chain(self, names: list[str], wrappers: int) -> str:
        """Outermost first: framework wrappers gray, the headline white, the root cause red."""
        out = []
        for i, name in enumerate(names):
            if i < wrappers:
                out.append(self.pal.gray(name))
            elif i == len(names) - 1:
                out.append(self.pal.bold_red(name))
            else:
                out.append(self.pal.white(name))
        return self.pal.gray(" › ").join(out)


def overview(groups: list[ErrorGroup], pal: Palette, width: int, spiking) -> list[str]:
    """One row per group, columns aligned: # TREND COUNT ERROR RAISED AT LOGGED AT."""
    style = Style(pal, spiking)
    head = ("#", "TREND", "COUNT", "ERROR", "RAISED AT", "LOGGED AT")
    rows = [
        (
            str(i),
            _badge_text(g, pal),
            f"{g.count:,}",
            short_class(g.title),
            short_place(g.raised_at),
            short_place(g.logged_at.id if g.logged_at else None),
        )
        for i, g in enumerate(groups, 1)
    ]
    widths = [max(len(head[c]), *(len(r[c]) for r in rows)) for c in range(len(head))]
    fixed = sum(widths) - widths[3] + 2 * (len(head) - 1) + 2
    widths[3] = max(min(widths[3], width - fixed), 20)
    right = {0, 2}

    def cells(row: tuple[str, ...]) -> list[str]:
        out = []
        for c, (text, w) in enumerate(zip(row, widths, strict=True)):
            text = _cut(text, w)
            out.append(text.rjust(w) if c in right else text.ljust(w))
        return out

    rule = "─" * (sum(widths) + 2 * (len(head) - 1))
    lines = ["  " + pal.bold(pal.gray("  ".join(cells(head)).rstrip())), "  " + pal.gray(rule)]
    for g, row in zip(groups, rows, strict=True):
        n, trend, count, error, raised, logged = cells(row)
        word = trend.rstrip()
        lines.append(
            "  "
            + "  ".join(
                [
                    pal.gray(n),
                    style.badge(g, word) + trend[len(word) :],  # badge only the word
                    pal.bold_white(count),
                    pal.bold_white(error),
                    pal.gray(raised) if raised.strip() == "—" else pal.bold_cyan(raised),
                    pal.gray(logged) if logged.strip() == "—" else pal.blue(logged),
                ]
            ).rstrip()
        )
    return lines


def card(
    i: int,
    g: ErrorGroup,
    *,
    pal: Palette,
    width: int,
    root: Path,
    spiking,
    trace_field: str,
    app: AppFiles,
    traceback: bool,
) -> list[str]:
    """Everything about one group, in the same order every time."""
    style = Style(pal, spiking)
    badge_len = len(_badge_text(g, pal))
    count = f"{g.count:,}"
    title = _cut(f"{i}. {short_class(g.title)}", width - badge_len - len(count) - 12)
    bar = max(width - len(title) - badge_len - len(count) - 7, 3)
    lines = [
        "",
        f"{style.bar(g, '━━')} {pal.bold_white(title)} {style.bar(g, '━' * bar)} "
        f"{pal.bold_white(count)} {style.badge(g)}".rstrip(),
    ]
    rows: list[tuple[str, str]] = []
    if g.causes:
        name, sep, message = short_class(g.causes[0]).partition(": ")
        text = pal.bold_red(name) + (pal.red(f"{sep}{message}") if sep else "")
        if g.origin:
            text += "  " + pal.magenta(f"({g.origin})")
        rows.append(("Root cause", text))
    if g.exc_type and (g.causes or g.wrapped_in):
        names = [*g.wrapped_in, g.exc_type.rsplit(".", 1)[-1]]
        names += [c.split(":", 1)[0].rsplit(".", 1)[-1] for c in reversed(g.causes)]
        rows.append(("Chain", style.chain(names, len(g.wrapped_in))))
    if g.raised_at:
        where = pal.gray(f"{g.raised_in}()")
        if g.origin and not g.causes:
            where += "  " + pal.magenta(f"(in {g.origin})")
        rows.append(("Raised at", f"{pal.yours(g.raised_at)}  {where}"))
        rows += _code(root, g.raised_at, pal)
    elif g.origin:
        rows.append(("Raised at", pal.magenta(f"in {g.origin}") + pal.gray(", no frame of yours")))
    if g.logged_at:
        rows.append(("Logged at", pal.bold_blue(g.logged_at.id)))
        rows += _code(root, g.logged_at.id, pal)
    if len(g.path) > 1:
        rows.append(("Call path", style.path(g.path, g.origin)))
    span = _span(g.first_seen, g.last_seen)
    if span:
        rows.append(("Seen", pal.white(span)))
    rows += trend_rows(g, pal)
    rows += history_rows(g, pal)
    if g.services:
        services = ", ".join(f"{s} ({n:,})" for s, n in g.services.most_common(3))
        rows.append(("Services", pal.white(services)))
    ids = dict(g.sample.get("ids") or {})
    trace = ids.pop(trace_field, None) or ids.pop("trace_id", None)
    if trace:
        rows.append(("Example", pal.gray("logsite trace ") + pal.bold_yellow(trace)))
    if ids:
        rows.append(("IDs", pal.gray(" ".join(f"{k}={v}" for k, v in ids.items()))))
    for label, value in rows:
        lines.append(f"   {pal.gray(label.ljust(LABEL_WIDTH))}   {value}")
    if traceback and g.sample.get("traceback"):
        lines.append("")
        lines += _traceback(g.sample["traceback"], app, pal, "   ")
    return lines


def _code(root: Path, location: str, pal: Palette) -> list[tuple[str, str]]:
    path, _, line = location.rpartition(":")
    code = source_line(root, path, int(line)) if line.isdigit() else None
    if not code:
        return []
    return [("", f"{pal.gray(f'{line} │')} {pal.code(_cut(code, 110))}")]


def _traceback(text: str, app: AppFiles, pal: Palette, indent: str) -> list[str]:
    """The sample trace: the project's own frames highlighted and marked, library frames gray."""
    out = []
    own = False  # Python prints the source line under its frame: same colour as the frame
    for line in text.splitlines():
        frame = line_frame(line)
        if frame is not None:
            own = app.resolve(frame) is not None
            body = pal.bold_cyan(line) if own else pal.gray(line)
            out.append(f"{indent}{pal.bold_cyan('▶') if own else ' '} {body}")
        elif is_exception_line(line):
            own = False
            out.append(f"{indent}  {pal.bold_red(line)}")
        elif line[:1].isspace() and line.strip() and own and not line.strip().startswith("..."):
            out.append(f"{indent}  {pal.code(line)}")
        else:
            out.append(f"{indent}  {pal.gray(line)}")
    return out


def headline(summary: ErrorSummary, label: str, pal: Palette) -> str:
    groups = summary.groups
    new = sum(g.is_new for g in groups)
    parts = [f"{pal.bold_white(f'{summary.events:,} errors')} {pal.white(label)}"]
    parts.append(pal.white(f"{len(groups)} group(s)"))
    if new:
        parts.append(pal.badge_red(f" {new} NEW ") if pal.enabled else f"{new} new")
    back = sum(is_regression(g) for g in groups)
    if back:
        parts.append(pal.badge_red(f" {back} BACK ") if pal.enabled else f"{back} back")
    if summary.previous_events is not None:
        before = f"before: {summary.previous_events:,}"
        if summary.previous_events:
            before += f", {summary.events / summary.previous_events:.1f}x"
        parts.append(pal.gray(before))
    if summary.resolved:
        parts.append(pal.gray(f"{summary.resolved} gone since the window before"))
    ratio = summary.traffic_ratio
    if ratio is not None and abs(ratio - 1) >= TRAFFIC_NOTE:
        parts.append(pal.gray(f"traffic {ratio:.1f}x"))
    return pal.gray(" · ").join(parts)


def terminal_width(default: int = 120) -> int:
    return max(shutil.get_terminal_size((default, 24)).columns, 80)


def incident_card(
    n: int, inc, *, pal: Palette, width: int, root: Path, spiking, trace_field: str
) -> list[str]:
    """One incident: its cause, what followed, the deploy before it and suspect commits."""
    style = Style(pal, spiking)
    cause = inc.cause
    started = _when(inc.started)
    span = _span(inc.started, inc.last_seen)
    duration = span.split("(", 1)[1].rstrip(")") if "(" in span else ""
    head = f"Incident {n} · since {started[11:] or started} UTC"
    if duration:
        head += f" ({duration})"
    head += f" · {inc.events:,} errors · {len(inc.groups)} group(s)"
    tag = pal.badge_red(" NEW ") if inc.is_new and pal.enabled else ("new" if inc.is_new else "")
    bar = max(width - len(head) - 12, 3)
    paint = pal.red if inc.is_new else pal.yellow
    lines = ["", f"{paint('━━')} {pal.bold_white(head)} {paint('━' * bar)} {tag}".rstrip()]
    rows: list[tuple[str, str]] = []
    rows.append(
        ("Cause", f"{pal.bold_white(short_class(cause.title))}  {pal.gray(f'({cause.count:,})')}")
    )
    if cause.causes:
        name, sep, message = short_class(cause.causes[0]).partition(": ")
        text = pal.bold_red(name) + (pal.red(f"{sep}{message}") if sep else "")
        if cause.origin:
            text += "  " + pal.magenta(f"({cause.origin})")
        rows.append(("Root cause", text))
    if cause.raised_at:
        rows.append(("Raised at", pal.yours(cause.raised_at)))
        rows += _code(root, cause.raised_at, pal)
    elif cause.logged_at:
        rows.append(("Logged at", pal.bold_blue(cause.logged_at.id)))
    rows += history_rows(cause, pal)
    for i, g in enumerate(inc.effects):
        when = _when(g.first_seen)[11:] if g.first_seen else "     "
        why = inc.reasons.get(g.id)
        text = (
            f"{pal.gray(when)}  {pal.white(_cut(short_class(g.title), width - 50))}  "
            f"{pal.gray(f'({g.count:,})')}  {style.badge(g)}"
        )
        if why:
            text += "  " + pal.gray(why)
        rows.append(("Then" if i == 0 else "", text))
    if inc.deploy:
        ref, at = inc.deploy
        minutes = (inc.started - at).total_seconds() / 60 if inc.started else 0
        gap = f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f}h"
        rows.append(
            (
                "Deploy",
                pal.bold_yellow(ref)
                + " "
                + pal.gray(f"at {_when(at)} UTC, {gap} before it started"),
            )
        )
    for i, c in enumerate(inc.suspects):
        rows.append(
            (
                "Suspects" if i == 0 else "",
                f"{pal.yellow(c.sha[:8])} {pal.gray(f'{c.date:%Y-%m-%d}')} {c.author}: {c.summary}",
            )
        )
    ids = cause.sample.get("ids") or {}
    trace = ids.get(trace_field) or ids.get("trace_id")
    if trace:
        rows.append(("Example", pal.gray("logsite trace ") + pal.bold_yellow(trace)))
    d = inc.diagnosis
    if d is not None and d.strength == "unclear":
        rows.append(("Likely", pal.gray("unclear: not enough evidence to rank a cause")))
        rows.append(("", pal.gray("what was checked: logsite investigate --report")))
    elif d is not None and d.hypotheses:
        first, *rest = d.hypotheses
        rows.append(("Likely", pal.white(first.title) + pal.gray(f"  · {d.strength} evidence")))
        if rest:
            rows.append(("Or", pal.white(rest[0].title)))
        rows.append(("", pal.gray("why, and what to check next: logsite investigate --report")))
    for label, value in rows:
        lines.append(f"   {pal.gray(label.ljust(LABEL_WIDTH))}   {value}")
    return lines
