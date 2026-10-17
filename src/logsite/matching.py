"""Map production log records back to the log call that produced them.

Strategy, most reliable first:

1. Structured fields: if the record carries ``pathname``/``filename`` + ``lineno``
   (Python's ``logging`` provides both for free), match on file suffix + line.
2. Template matching: turn each site's message template into a regex and pick the
   matching site with the most literal text, so ``"%s"`` never beats ``"user %s logged in"``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .models import LogRecord, LogSite


class Matcher:
    def __init__(self, sites: Iterable[LogSite]) -> None:
        self.sites = list(sites)
        self._by_basename_line: dict[tuple[str, int], list[LogSite]] = defaultdict(list)
        # The other lines of a call that spans several: runtimes report different ones
        # (Go and Java often the line of the last method in a chain, ``.Msg(...)``).
        self._by_basename_span: dict[tuple[str, int], list[LogSite]] = defaultdict(list)
        for site in self.sites:
            basename = site.path.rsplit("/", 1)[-1]
            self._by_basename_line[(basename, site.line)].append(site)
            for line in range(site.line + 1, site.end_line + 1):
                self._by_basename_span[(basename, line)].append(site)

        compiled = [(s, s.template_regex()) for s in self.sites]
        # Longest literal text first: the first regex that matches is the most specific.
        self._templates = sorted(
            ((s, rx) for s, rx in compiled if rx is not None),
            key=lambda pair: pair[0].literal_len,
            reverse=True,
        )

    def match(self, record: LogRecord) -> LogSite | None:
        site = self._match_structured(record)
        if site is not None:
            return site
        # A record with a stack trace attached: the message is its first line.
        return self._match_template(record.message.split("\n", 1)[0])

    def _match_structured(self, record: LogRecord) -> LogSite | None:
        src, line = record.source_file, record.source_line
        if not src or line is None:
            return None
        return self.match_location(src, line)

    def match_location(self, src: str, line: int) -> LogSite | None:
        """The site at ``line`` of the file whose path ends like ``src`` (any OS separators)."""
        src = src.replace("\\", "/")
        key = (src.rsplit("/", 1)[-1], line)
        candidates = self._by_basename_line.get(key) or self._by_basename_span.get(key, [])
        if len(candidates) <= 1:
            return candidates[0] if candidates else None
        # Several files share a basename: prefer the longest matching path suffix.
        return max(candidates, key=lambda s: _common_suffix_len(s.path, src))

    def _match_template(self, message: str) -> LogSite | None:
        for site, rx in self._templates:
            if rx.search(message):
                return site
        return None


def sample_records(
    sites: Iterable[LogSite], records: Iterable[LogRecord], per_site: int = 200
) -> dict[str, list[LogRecord]]:
    """Up to ``per_site`` records for each site, from a batch of records."""
    matcher = Matcher(sites)
    out: dict[str, list[LogRecord]] = {}
    for record in records:
        site = matcher.match(record)
        if site is not None:
            sample = out.setdefault(site.id, [])
            if len(sample) < per_site:
                sample.append(record)
    return out


def _common_suffix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(reversed(a), reversed(b), strict=False):
        if x != y:
            break
        n += 1
    return n
