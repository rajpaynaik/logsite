"""``logsite errors -i``: browse the error store in the terminal.

A list of the error groups for a time range (searchable, by service), and for each one its
case file: the code, how often and since when, the commits before it started, examples. From
either, ``p`` copies the case file as a prompt for an AI assistant and ``r`` writes the
reproduction test.

``Browser`` holds the state and turns keys into changes and the screen into lines, so it's
tested without a terminal; ``run`` draws it with curses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import _dt

RANGES = [(1, "last hour"), (24, "last 24 hours"), (168, "last 7 days"), (720, "last 30 days"),
          (None, "everything stored")]  # fmt: skip
SPARK = " ▁▂▃▄▅▆▇█"


class Source:
    """Where the browser gets its data and does its actions (the store, the clipboard)."""

    def __init__(self, cfg, store) -> None:
        self.cfg, self.store = cfg, store

    def groups(self, since, service, search) -> list[dict]:
        return self.store.groups(since=since, limit=500, service=service, search=search)

    def facets(self, since) -> dict:
        return self.store.facets(since)

    def case(self, gid: str) -> dict:
        from .prompt import gather

        return gather(self.cfg, self.store, gid)

    def copy(self, text: str) -> bool:
        from .cli import _copy

        return _copy(text)

    def repro(self, gid: str) -> str:
        """Write the reproduction test; the outcome as one line."""
        from .webapp import App

        result = App(self.cfg.root).repro(gid, write=True)
        if result.get("written"):
            return f"wrote {result['path']}: run it, it should fail like production did"
        return f"no test: {result.get('error') or result.get('note') or 'not enough to go on'}"


@dataclass
class Line:
    text: str
    style: str = ""  # "", "bold", "dim", "title", "selected", "code", "good", "bad"


@dataclass
class Browser:
    source: Source
    range_index: int = 1
    search: str = ""
    service: str | None = None
    groups: list[dict] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    selected: int = 0
    top: int = 0  # first row shown in the list
    view: str = "list"  # list | detail | help
    view_before_help: str = "list"
    case: dict | None = None
    detail: list[Line] = field(default_factory=list)
    scroll: int = 0
    typing: bool = False  # in the search box
    message: str = ""
    now: datetime | None = None

    # --- data -----------------------------------------------------------------------------

    def since(self) -> datetime | None:
        hours = RANGES[self.range_index][0]
        now = self.now or datetime.now(timezone.utc)
        return now - timedelta(hours=hours) if hours else None

    def load(self) -> None:
        since = self.since()
        chosen = self.groups[self.selected]["id"] if self.groups else None
        self.groups = self.source.groups(since, self.service, self.search or None)
        self.services = [f["value"] for f in self.source.facets(since).get("services", [])]
        ids = [g["id"] for g in self.groups]
        self.selected = ids.index(chosen) if chosen in ids else 0
        self.top = min(self.top, self.selected)

    def open(self) -> None:
        if not self.groups:
            return
        from .prompt import render

        self.case = self.source.case(self.groups[self.selected]["id"])
        self.detail = case_lines(self.case, render(self.case), self.now)
        self.view, self.scroll = "detail", 0

    def current(self) -> dict | None:
        if self.view == "detail" and self.case:
            return self.case["group"]
        return self.groups[self.selected] if self.groups else None

    # --- keys -----------------------------------------------------------------------------

    def key(self, k: str, height: int = 24) -> bool:
        """Handle one key (a character, or "up", "down", "pgup", "pgdn", "home", "end",
        "enter", "esc", "backspace"). Returns False to quit."""
        self.message = ""
        if self.typing:
            return self._type(k)
        page = max(1, height - 6)
        if self.view == "help":
            self.view = self.view_before_help
            return True
        if k == "?":
            self.view_before_help, self.view = self.view, "help"
            return True
        if self.view == "detail":
            if k in ("q", "esc", "backspace", "h", "left"):
                self.view = "list"
            elif k in ("down", "j"):
                self.scroll = min(self.scroll + 1, max(0, len(self.detail) - page))
            elif k in ("up", "k"):
                self.scroll = max(0, self.scroll - 1)
            elif k in ("pgdn", " "):
                self.scroll = min(self.scroll + page, max(0, len(self.detail) - page))
            elif k == "pgup":
                self.scroll = max(0, self.scroll - page)
            elif k in ("p", "r"):
                self._act(k)
            return True
        if k in ("q", "esc"):
            return False
        n = len(self.groups)
        if k in ("down", "j"):
            self.selected = min(self.selected + 1, max(0, n - 1))
        elif k in ("up", "k"):
            self.selected = max(0, self.selected - 1)
        elif k == "pgdn":
            self.selected = min(self.selected + page, max(0, n - 1))
        elif k == "pgup":
            self.selected = max(0, self.selected - page)
        elif k in ("home", "g"):
            self.selected = 0
        elif k in ("end", "G"):
            self.selected = max(0, n - 1)
        elif k in ("enter", "l", "right"):
            self.open()
        elif k == "/":
            self.typing = True
        elif k == "t":
            self.range_index = (self.range_index + 1) % len(RANGES)
            self.load()
        elif k == "s":
            options = [None, *self.services]
            i = options.index(self.service) if self.service in options else 0
            self.service = options[(i + 1) % len(options)]
            self.load()
        elif k == "R":
            self.load()
            self.message = "refreshed"
        elif k in ("p", "r"):
            self._act(k)
        return True

    def _type(self, k: str) -> bool:
        if k == "enter":
            self.typing = False
        elif k == "esc":
            self.typing, self.search = False, ""
        elif k == "backspace":
            self.search = self.search[:-1]
        elif len(k) == 1 and k.isprintable():
            self.search += k
        else:
            return True
        self.load()
        return True

    def _act(self, k: str) -> None:
        group = self.current()
        if group is None:
            return
        if k == "p":
            from .prompt import render

            case = self.case if self.view == "detail" and self.case else self.source.case(
                group["id"])  # fmt: skip
            ok = self.source.copy(render(case))
            if ok:
                self.message = f"copied the prompt for {group['id']}: paste it into your assistant"
            else:
                self.message = "no clipboard tool found (pbcopy, wl-copy, xclip)"
        else:
            self.message = self.source.repro(group["id"])

    # --- the screen -----------------------------------------------------------------------

    def screen(self, width: int, height: int) -> list[Line]:
        if self.view == "help":
            body = help_lines()
        elif self.view == "detail":
            body = self.detail[self.scroll : self.scroll + height - 3]
        else:
            body = self._list(width, height - 3)
        title = f"logsite · {RANGES[self.range_index][1]}"
        if self.service:
            title += f" · service {self.service}"
        if self.search or self.typing:
            title += f" · search: {self.search}" + ("▏" if self.typing else "")
        total = sum(g.get("recent", 0) for g in self.groups)
        title += f" · {total} error(s) in {len(self.groups)} group(s)"
        keys = {
            "list": "↑↓ move  ⏎ open  / search  t time  s service  p prompt  r test  ? help  q",
            "detail": "↑↓ scroll  p copy prompt  r write test  ← back  ? help",
            "help": "any key: back",
        }[self.view]  # fmt: skip
        if self.typing:
            keys = "type to search  ⏎ done  esc clear"
        rows = [Line(_fit(title, width), "title"), *body]
        rows += [Line("")] * max(0, height - 2 - len(rows))
        rows.append(Line(_fit(self.message, width), "good" if self.message else ""))
        rows.append(Line(_fit(keys, width), "dim"))
        return rows[:height]

    def _list(self, width: int, rows: int) -> list[Line]:
        if not self.groups:
            hint = "No errors stored for this range."
            if RANGES[self.range_index][0] is not None:
                hint += " Press t for a longer one."
            return [Line(""), Line("  " + hint, "dim")]
        rows -= 1  # the header
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + rows:
            self.top = self.selected - rows + 1
        now = self.now or datetime.now(timezone.utc)
        out = [Line(_fit(f"  {'ID':<9}{'COUNT':>7}  {'LAST':>8}  ERROR", width), "bold")]
        for i, g in enumerate(self.groups[self.top : self.top + rows], self.top):
            where = g.get("raised_at") or g.get("logged_at") or ""
            text = (f"  {g['id']:<9}{g.get('recent', 0):>7}  {_ago(g.get('last_in'), now):>8}  "
                    f"{g.get('title') or ''}")  # fmt: skip
            if where:
                text += f"  · {where}"
            out.append(Line(_fit(text, width), "selected" if i == self.selected else ""))
        return out


def case_lines(case: dict, text: str, now: datetime | None = None) -> list[Line]:
    """The case file (``logsite prompt``'s text) as styled lines, with an hourly chart."""
    out: list[Line] = []
    code = False
    for raw in text.splitlines():
        if raw.startswith("```"):
            code = not code
        elif code:
            out.append(Line(raw, "code"))
        elif raw.startswith("# "):
            out.append(Line(raw[2:], "bold"))
            spark = sparkline(week(case.get("hourly", []), now))
            if spark:
                out.append(Line(f"last 7 days  {spark}", "dim"))
        elif raw.startswith("## "):
            out += [Line(""), Line(raw[3:], "bold")]
        elif not raw.startswith("I need help fixing"):
            out.append(Line(raw.replace("**", "").replace("`", "")))
    return out


def week(hourly: list[dict], now: datetime | None = None) -> list[int]:
    """The last 168 hours, oldest first, with the hours without errors as 0."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    counts = {h["hour"]: h["n"] for h in hourly}
    hours = [(now - timedelta(hours=167 - i)).strftime("%Y-%m-%d %H") for i in range(168)]
    return [counts.get(h, 0) for h in hours]


def sparkline(counts: list[int], width: int = 56) -> str:
    if not counts or not any(counts):
        return ""
    step = max(1, -(-len(counts) // width))
    buckets = [sum(counts[i : i + step]) for i in range(0, len(counts), step)]
    top = max(buckets)
    return "".join(SPARK[min(8, -(-8 * n // top))] if n else SPARK[0] for n in buckets)


def help_lines() -> list[Line]:
    rows = [
        ("↑ ↓  j k", "move (in a case file: scroll)"), ("⏎  l", "open the error's case file"),
        ("← esc  h", "back to the list"), ("/", "search the error, its cause or its file"),
        ("t", "time range: hour, day, week, month, everything"), ("s", "next service"),
        ("p", "copy the case file as a prompt for your AI assistant"),
        ("r", "write a failing test that reproduces the error (logsite repro)"),
        ("R", "refresh"), ("q", "quit"),
    ]  # fmt: skip
    return [Line(""), Line("  Keys", "bold")] + [Line(f"  {k:<10} {v}") for k, v in rows]


def _fit(text: str, width: int) -> str:
    return text if len(text) < width else text[: max(0, width - 2)] + "…"


def _ago(value, now: datetime) -> str:
    at = _dt(value)
    if at is None:
        return ""
    s = max(0, int((now - at).total_seconds()))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# --- the terminal -------------------------------------------------------------------------------

KEYS = {"KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right",
        "KEY_PPAGE": "pgup", "KEY_NPAGE": "pgdn", "KEY_HOME": "home", "KEY_END": "end",
        "KEY_ENTER": "enter", "\n": "enter", "\r": "enter", "\x1b": "esc",
        "KEY_BACKSPACE": "backspace", "\x7f": "backspace", "\b": "backspace"}  # fmt: skip


def run(browser: Browser) -> None:
    import curses

    def main(screen) -> None:
        curses.curs_set(0)
        curses.set_escdelay(25)
        styles = {"": curses.A_NORMAL, "bold": curses.A_BOLD, "dim": curses.A_DIM,
                  "title": curses.A_REVERSE | curses.A_BOLD, "selected": curses.A_REVERSE,
                  "code": curses.A_NORMAL, "good": curses.A_BOLD, "bad": curses.A_BOLD}  # fmt: skip
        if curses.has_colors():
            curses.use_default_colors()
            for n, color in enumerate((curses.COLOR_GREEN, curses.COLOR_CYAN, curses.COLOR_RED), 1):
                curses.init_pair(n, color, -1)
            styles.update(code=curses.color_pair(1), good=curses.color_pair(2) | curses.A_BOLD,
                          bad=curses.color_pair(3) | curses.A_BOLD)  # fmt: skip
        browser.load()
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            for y, line in enumerate(browser.screen(width, height)):
                try:
                    screen.addnstr(y, 0, line.text, width - 1, styles.get(line.style, 0))
                except curses.error:  # the bottom-right cell, or a tiny window
                    pass
            screen.refresh()
            try:
                raw = screen.get_wch()
            except KeyboardInterrupt:
                return
            name = curses.keyname(raw).decode() if isinstance(raw, int) else raw
            if name == "KEY_RESIZE":
                continue
            if not browser.key(KEYS.get(name, name), height):
                return

    curses.wrapper(main)
