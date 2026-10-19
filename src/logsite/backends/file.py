"""Read logs from a local file (plain text or JSON lines). Handy for demos, tests and CI."""

from __future__ import annotations

from collections import deque
from datetime import timezone
from pathlib import Path

from ..models import LogRecord
from ..tracebacks import is_error, join_multiline
from .base import LogBackend, TimeRange, parse_line


class FileBackend(LogBackend):
    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        return self._read(time_range, contains, limit)

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        return self._read(time_range, None, limit, errors_only=True)

    def count_total(self, time_range: TimeRange) -> int | None:
        n = 0
        with self.path.open(encoding="utf-8", errors="replace") as fh:
            for raw in join_multiline(fh):
                if not raw.strip():
                    continue
                ts = parse_line(raw).timestamp
                if ts is not None and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts is not None and time_range.start <= ts <= time_range.end:
                    n += 1  # lines without a time would count in every window
        return n

    def _read(
        self,
        time_range: TimeRange,
        contains: str | None,
        limit: int,
        errors_only: bool = False,
    ) -> list[LogRecord]:
        # the newest ``limit``, like Loki's backward queries: when a window has more records
        # than the limit, the recent ones (a new error) matter, not the first of the day
        out: deque[LogRecord] = deque(maxlen=limit)
        last_ts = None
        with self.path.open(encoding="utf-8", errors="replace") as fh:
            for raw in join_multiline(fh):  # a multi-line traceback is one event
                if not raw.strip():
                    continue
                record = parse_line(raw)
                if record.timestamp is None:
                    # a line without a time (a Go runtime crash on stderr) happened right
                    # after the last line that had one, not in every window
                    record.timestamp = last_ts
                else:
                    last_ts = record.timestamp
                if contains and contains not in raw:
                    continue
                if errors_only and not is_error(record):
                    continue
                ts = record.timestamp
                if ts is not None:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if not (time_range.start <= ts <= time_range.end):
                        continue
                out.append(record)
        return list(out)
