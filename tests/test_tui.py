"""logsite errors -i: the browser's keys and screens, without a terminal."""

from datetime import datetime, timedelta, timezone

from logsite.tui import RANGES, Browser, sparkline, week

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def group(gid, n, minutes_ago, title, service="checkout"):
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {"id": gid, "recent": n, "last_in": at, "title": title,
            "raised_at": f"app/{gid}.py:1", "service": service}  # fmt: skip


class Fake:
    def __init__(self):
        self.all = [group("aaaa1111", 40, 3, "KeyError: 'id'"),
                    group("bbbb2222", 7, 90, "TimeoutError: timed out", "payments"),
                    group("cccc3333", 2, 60 * 30, "ValueError: bad sku")]  # fmt: skip
        self.calls, self.copied, self.repros = [], [], []

    def groups(self, since, service, search):
        self.calls.append((since, service, search))
        rows = [g for g in self.all if since is None or g["last_in"] >= since.isoformat()]
        rows = [g for g in rows if service in (None, g["service"])]
        return [g for g in rows if not search or search.lower() in g["title"].lower()]

    def facets(self, since):
        return {"services": [{"value": "checkout", "n": 42}, {"value": "payments", "n": 7}]}

    def case(self, gid):
        g = next(g for g in self.all if g["id"] == gid)
        return {"group": {**g, "events": g["recent"], "first_seen": g["last_in"],
                          "last_seen": g["last_in"]},
                "code": None, "frequency": {"last_24h": 3, "previous_24h": 1, "week": 9,
                                            "peak_hour": None},
                "services": [], "releases": [], "hourly": [{"hour": f"2026-10-24 {h:02}", "n": n}
                                                           for h, n in ((9, 1), (10, 4), (11, 2))],
                "started": {"at": g["last_in"], "release": None, "tag": None, "commits": []},
                "history": None, "shared": {"same": [], "varies": [], "occurrences": 0},
                "examples": [], "test": None, "url": None}  # fmt: skip

    def copy(self, text):
        self.copied.append(text)
        return True

    def repro(self, gid):
        self.repros.append(gid)
        return f"wrote tests/test_repro_{gid}.py"


def browser():
    b = Browser(Fake(), now=NOW)
    b.load()
    return b


def text(b, width=100, height=20):
    return "\n".join(line.text for line in b.screen(width, height))


def test_the_list():
    b = browser()
    screen = text(b)
    assert "logsite · last 24 hours · 47 error(s) in 2 group(s)" in screen
    assert "aaaa1111      40    3m ago  KeyError: 'id'  · app/aaaa1111.py:1" in screen
    assert "bbbb2222       7    1h ago  TimeoutError" in screen
    assert "cccc3333" not in screen  # 30 hours ago: outside the last 24
    assert b.screen(100, 20)[2].style == "selected"  # the first row under the header
    assert "q" in b.screen(100, 20)[-1].text  # the keys


def test_moving_and_the_time_range():
    b = browser()
    b.key("down")
    b.key("down")  # stops at the last
    assert b.selected == 1
    b.key("t")  # last 7 days
    assert RANGES[b.range_index][1] == "last 7 days" and len(b.groups) == 3
    assert b.selected == 1 and b.groups[1]["id"] == "bbbb2222"  # still on the same error
    b.key("G")
    assert b.selected == 2
    for _ in range(3):
        b.key("t")  # 30 days, everything, then back to the last hour
    assert RANGES[b.range_index][0] == 1 and [g["id"] for g in b.groups] == ["aaaa1111"]
    assert b.selected == 0


def test_search_and_service():
    b = browser()
    b.key("/")
    for ch in "time":
        b.key(ch)
    assert b.typing and [g["id"] for g in b.groups] == ["bbbb2222"]
    assert "search: time▏" in text(b)
    b.key("enter")
    assert not b.typing and b.search == "time"
    b.key("/")
    b.key("esc")  # clears it
    assert b.search == "" and len(b.groups) == 2
    b.key("s")
    assert b.service == "checkout" and [g["id"] for g in b.groups] == ["aaaa1111"]
    b.key("s")
    b.key("s")  # payments, then all again
    assert b.service is None


def test_the_case_file_and_back():
    b = browser()
    b.key("enter")
    assert b.view == "detail"
    screen = text(b)
    assert (
        "KeyError: 'id'" in screen
        and "last 7 days  " in screen
        and screen.count("last 7 days") == 1
    )
    assert "▂█" in screen and "#" not in screen.split("How often")[0][60:]
    assert "How often" in screen and "```" not in screen and "**" not in screen
    b.key("esc")
    assert b.view == "list" and b.key("q") is False


def test_copy_the_prompt_and_write_the_test():
    b = browser()
    b.key("down")
    b.key("p")
    assert b.source.copied[0].startswith("# Production error: TimeoutError")
    assert "copied the prompt for bbbb2222" in text(b)
    b.key("r")
    assert b.source.repros == ["bbbb2222"] and "wrote tests/test_repro_bbbb2222.py" in text(b)
    b.key("enter")
    b.key("p")  # from the case file too
    assert len(b.source.copied) == 2


def test_help_and_an_empty_range():
    b = browser()
    b.key("?")
    assert b.view == "help" and "copy the case file as a prompt" in text(b)
    b.key("x")
    assert b.view == "list"
    b.source.all = []
    b.load()
    assert "No errors stored for this range. Press t for a longer one." in text(b)
    b.key("enter")  # nothing to open
    assert b.view == "list"


def test_narrow_and_short_screens():
    b = browser()
    rows = b.screen(30, 6)
    assert len(rows) == 6 and all(len(r.text) < 30 for r in rows)


def test_week():
    hourly = [{"hour": "2026-10-24 11", "n": 3}, {"hour": "2026-10-17 12", "n": 1},
              {"hour": "2026-10-10 00", "n": 9}]  # fmt: skip
    counts = week(hourly, NOW)
    assert len(counts) == 168 and counts[-1] == 0 and counts[-2] == 3 and counts[0] == 0
    assert sum(counts) == 3  # 2026-10-17 12:00 is 168 hours back: just outside


def test_sparkline():
    assert sparkline([]) == "" and sparkline([0, 0]) == ""
    assert sparkline([0, 1, 2, 4, 8]) == " ▁▂▄█"
    assert len(sparkline(list(range(168)), width=56)) == 56
