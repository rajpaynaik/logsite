"""Grafana Loki adapter using the HTTP API (stdlib only).

With ``aggregate = true``, counts are computed by Loki itself with one metric query
(``sum by (pathname, lineno) (count_over_time(...))``), so they aren't capped by the number
of log lines logsite can download. Only lines without file/line fields are still fetched
and matched by template.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import datetime, timezone

from ..matching import Matcher
from ..models import LogRecord, LogSite, Usage
from ..tracebacks import is_error
from .base import LogBackend, TimeRange, measure, parse_line, tally

ERROR_FILTER = "(?i)(error|exception|traceback|critical|fatal)"


class LokiBackend(LogBackend):
    name = "loki"

    def __init__(
        self,
        url: str,
        selector: str,
        *,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
        aggregate: bool = False,
        parser: str = "json",
        file_label: str = "pathname",
        line_label: str = "lineno",
        timeout: float = 30.0,
    ) -> None:
        if parser not in ("json", "logfmt"):
            raise ValueError(f"loki parser must be 'json' or 'logfmt', got {parser!r}")
        self.url = url.rstrip("/")
        self.selector = selector  # e.g. '{app="checkout"}'
        self.aggregate = aggregate
        self.parser = parser  # how Loki extracts file_label / line_label from each line
        self.file_label = file_label
        self.line_label = line_label
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"
        if tenant_id:
            self.headers["X-Scope-OrgID"] = tenant_id

    def build_query(self, contains: str | None = None) -> str:
        query = self.selector
        if contains:
            query += " |= " + json.dumps(contains)
        return query

    def build_count_query(self, time_range: TimeRange, fn: str = "count_over_time") -> str:
        """``fn`` is ``count_over_time`` (lines) or ``bytes_over_time`` (stored bytes)."""
        seconds = max(1, round((time_range.end - time_range.start).total_seconds()))
        return (
            f"sum by ({self.file_label}, {self.line_label}) ({fn}("
            f'{self.selector} | {self.parser} | __error__="" '
            f'| {self.file_label}!="" | {self.line_label}!="" '
            f"[{seconds}s]))"
        )

    def build_unlocated_query(self) -> str:
        """Lines Loki can't attribute to a file/line: unparseable, or missing the label."""
        return (
            f"{self.selector} | {self.parser} "
            f'| __error__!="" or {self.file_label}="" or {self.line_label}="" '
            "| drop __error__, __error_details__"
        )

    def count_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, int]:
        if not self.aggregate:
            return super().count_by_line(sites, time_range, limit)
        locations = self._located(time_range)
        rest = self.query_logs(time_range, limit=limit, query=self.build_unlocated_query())
        return tally(Matcher(sites), locations, rest)

    def usage_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, Usage]:
        if not self.aggregate:
            return super().usage_by_line(sites, time_range, limit)
        matcher = Matcher(sites)
        counts = self._located(time_range)
        sizes = self._located(time_range, "bytes_over_time")
        rest = self.query_logs(time_range, limit=limit, query=self.build_unlocated_query())
        usage = measure(matcher, rest)
        for loc, n in counts.items():
            site = matcher.match_location(*loc)
            if site is not None:
                u = usage.setdefault(site.id, Usage())
                u.count += n
                u.bytes += sizes.get(loc, 0)
        return usage

    def count_total(self, time_range: TimeRange) -> int | None:
        seconds = max(1, round((time_range.end - time_range.start).total_seconds()))
        payload = self._get(
            "query",
            {
                "query": f"sum(count_over_time({self.selector} [{seconds}s]))",
                "time": str(int(time_range.end.timestamp() * 1e9)),
            },
        )
        result = payload.get("data", {}).get("result", [])
        return int(float(result[0]["value"][1])) if result else 0

    def _located(
        self, time_range: TimeRange, fn: str = "count_over_time"
    ) -> dict[tuple[str, int], int]:
        payload = self._get(
            "query",
            {
                "query": self.build_count_query(time_range, fn),
                "time": str(int(time_range.end.timestamp() * 1e9)),
            },
        )
        return self.parse_count_response(payload, self.file_label, self.line_label)

    @staticmethod
    def parse_count_response(
        payload: dict, file_label: str = "pathname", line_label: str = "lineno"
    ) -> dict[tuple[str, int], int]:
        out: dict[tuple[str, int], int] = {}
        for series in payload.get("data", {}).get("result", []):
            metric = series.get("metric", {})
            try:
                key = (metric[file_label], int(metric[line_label]))
            except (KeyError, ValueError):
                continue
            out[key] = out.get(key, 0) + int(float(series["value"][1]))
        return out

    def _get(self, endpoint: str, params: dict[str, str]) -> dict:
        url = f"{self.url}/loki/api/v1/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def query_logs(
        self,
        time_range: TimeRange,
        contains: str | None = None,
        limit: int = 5000,
        query: str | None = None,
    ) -> list[LogRecord]:
        params = {
            "query": query or self.build_query(contains),
            "start": str(int(time_range.start.timestamp() * 1e9)),
            "end": str(int(time_range.end.timestamp() * 1e9)),
            "limit": str(limit),
            "direction": "backward",
        }
        return self.parse_response(self._get("query_range", params))

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        """A case-insensitive line filter narrows to likely errors server-side; levels and
        tracebacks are then checked client-side."""
        query = f"{self.selector} |~ {json.dumps(ERROR_FILTER)}"
        records = self.query_logs(time_range, limit=limit, query=query)
        return [r for r in records if is_error(r)]

    @staticmethod
    def parse_response(payload: dict) -> list[LogRecord]:
        records: list[LogRecord] = []
        for stream in payload.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts_ns, line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                record = parse_line(line, timestamp=ts)
                record.fields = {**labels, **record.fields}
                records.append(record)
        return records
