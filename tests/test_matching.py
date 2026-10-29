from logsite.backends import parse_line
from logsite.index import index_source
from logsite.matching import Matcher

SRC = """
logger.info("user %s logged in from %s", uid, ip)
logger.info("%s", anything)
logger.warning(f"disk {pct}% full")
"""


def matcher():
    return Matcher(index_source(SRC, "svc/auth.py"))


def test_structured_match_uses_file_and_line():
    rec = parse_line('{"message": "whatever", "pathname": "/deploy/svc/auth.py", "lineno": 3}')
    assert matcher().match(rec).line == 3


def test_template_match_prefers_most_specific():
    rec = parse_line("2026-01-01 INFO user 7 logged in from 10.0.0.1")
    assert matcher().match(rec).line == 2


def test_fstring_with_percent():
    rec = parse_line("WARNING disk 93% full")
    assert matcher().match(rec).line == 4


def test_bare_placeholder_template_never_matches():
    rec = parse_line("totally unrelated")
    assert matcher().match(rec) is None


def test_punctuation_only_template_never_matches():
    # "{}: {}" has fixed text, but no words: it would claim any "x: y" line.
    m = Matcher(index_source('logger.error("%s: %s", where, err)\nlogger.info("ok")\n', "a.py"))
    assert m.match(parse_line("ERROR TypeError: fetch failed")) is None
    assert m.match(parse_line("INFO ok")).line == 2


def test_parse_iso_handles_z_and_offsets():
    from datetime import datetime, timedelta, timezone

    from logsite.models import parse_iso

    assert parse_iso("2026-10-17T00:35:51Z") == datetime(
        2026, 10, 17, 0, 35, 51, tzinfo=timezone.utc
    )
    assert parse_iso(" 2026-10-17T02:35:51+02:00\n").utcoffset() == timedelta(hours=2)
