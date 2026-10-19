"""OpenSearch / Elasticsearch adapter using the ``_search`` REST API (stdlib only).

With ``aggregate = true``, counts come from a composite aggregation over the file and line
fields, so they aren't capped by how many documents logsite can download. Only documents
without a line field are still fetched and matched by template.
"""

from __future__ import annotations

import base64
import json
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from ..context import mentions
from ..matching import Matcher
from ..models import LogRecord, LogSite, Usage
from ..tracebacks import is_error
from .base import LogBackend, TimeRange, _parse_ts, measure, parse_line, tally

AGG_PAGE = 1000
# Keyword fields are case-sensitive, hence the spellings.
ERROR_QUERY = (
    "ERROR OR Error OR error OR CRITICAL OR critical OR FATAL OR fatal OR Traceback OR "
    "exception OR Exception"
)
EXCEPTION_FIELDS = (
    "error.stack_trace", "error.type", "exception.type", "exception.stacktrace", "exc_info",
    "exc_text", "stack_trace",
)  # fmt: skip


class OpenSearchBackend(LogBackend):
    name = "opensearch"

    def __init__(
        self,
        url: str,
        index: str,
        *,
        query: str | None = None,
        timestamp_field: str = "@timestamp",
        message_field: str = "message",
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        aggregate: bool = False,
        file_field: str = "log.origin.file.name",
        line_field: str = "log.origin.file.line",
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.index = index  # e.g. "logs-checkout-*"
        self.query = query  # optional Lucene query_string, e.g. 'service.name:checkout'
        self.timestamp_field = timestamp_field
        self.message_field = message_field
        self.aggregate = aggregate
        # Must be aggregatable (keyword / numeric), e.g. "pathname.keyword" for dynamic mappings
        self.file_field = file_field
        self.line_field = line_field
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _filters(self, time_range: TimeRange, contains: str | None = None) -> list[dict]:
        filters: list[dict[str, Any]] = [
            {
                "range": {
                    self.timestamp_field: {
                        "gte": time_range.start.isoformat(),
                        "lte": time_range.end.isoformat(),
                    }
                }
            }
        ]
        if self.query:
            filters.append({"query_string": {"query": self.query}})
        if contains:
            filters.append({"match_phrase": {self.message_field: contains}})
        return filters

    def build_query(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> dict[str, Any]:
        return {
            "size": limit,
            "sort": [{self.timestamp_field: {"order": "desc"}}],
            "query": {"bool": {"filter": self._filters(time_range, contains)}},
        }

    def build_count_query(self, time_range: TimeRange, after: dict | None = None) -> dict[str, Any]:
        composite: dict[str, Any] = {
            "size": AGG_PAGE,
            "sources": [
                {"file": {"terms": {"field": self.file_field}}},
                {"line": {"terms": {"field": self.line_field}}},
            ],
        }
        if after:
            composite["after"] = after
        return {
            "size": 0,
            "query": {"bool": {"filter": self._filters(time_range)}},
            "aggs": {"by_line": {"composite": composite}},
        }

    def count_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, int]:
        if not self.aggregate:
            return super().count_by_line(sites, time_range, limit)
        locations: dict[tuple[str, int], int] = {}
        after = None
        while True:
            agg = self._search(self.build_count_query(time_range, after))["aggregations"]
            buckets = agg["by_line"]["buckets"]
            for b in buckets:
                try:
                    key = (str(b["key"]["file"]), int(b["key"]["line"]))
                except (KeyError, TypeError, ValueError):
                    continue
                locations[key] = locations.get(key, 0) + b["doc_count"]
            after = agg["by_line"].get("after_key")
            if not after or len(buckets) < AGG_PAGE:
                break
        # Documents missing the file or line field can only be matched by template.
        located = [{"exists": {"field": f}} for f in (self.file_field, self.line_field)]
        unlocated = self.build_query(time_range, limit=limit)
        unlocated["query"]["bool"]["must_not"] = [{"bool": {"filter": located}}]
        rest = self.parse_response(
            self._search(unlocated), self.timestamp_field, self.message_field
        )
        return tally(Matcher(sites), locations, rest)

    def usage_by_line(
        self, sites: Iterable[LogSite], time_range: TimeRange, limit: int = 5000
    ) -> dict[str, Usage]:
        """Exact counts from the aggregation, bytes from each line's average document size
        in a sample (OpenSearch has no per-field byte totals without the mapper-size plugin).
        """
        if not self.aggregate:
            return super().usage_by_line(sites, time_range, limit)
        sites = list(sites)
        counts = self.count_by_line(sites, time_range, limit)
        sample = measure(Matcher(sites), self.query_logs(time_range, limit=limit))
        sampled = [u for u in sample.values() if u.count]
        overall = sum(u.bytes for u in sampled) / max(1, sum(u.count for u in sampled))
        usage: dict[str, Usage] = {}
        for sid, n in counts.items():
            s = sample.get(sid)
            avg = s.bytes / s.count if s and s.count else overall
            usage[sid] = Usage(count=n, bytes=round(avg * n), estimated=True)
        return usage

    def find_records(
        self, field: str, value: str, time_range: TimeRange, limit: int = 5000
    ) -> list[LogRecord]:
        """The value lives in its own field (``trace.id``, ``trace_id``...), not the message,
        so search every field for the exact phrase, then check it client-side."""
        body = self.build_query(time_range, limit=limit)
        body["query"]["bool"]["filter"].append(
            {"multi_match": {"query": value, "type": "phrase", "fields": ["*"], "lenient": True}}
        )
        body["sort"] = [{self.timestamp_field: {"order": "asc"}}]
        records = self.parse_response(self._search(body), self.timestamp_field, self.message_field)
        return [r for r in records if mentions(r, field, value)]

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        """Documents with an error level, an exception field, or error words anywhere; the
        level and traceback are then checked client-side."""
        body = self.build_query(time_range, limit=limit)
        body["query"]["bool"]["filter"].append(
            {
                "bool": {
                    "should": [
                        {"query_string": {"query": ERROR_QUERY, "fields": ["*"], "lenient": True}},
                        *({"exists": {"field": f}} for f in EXCEPTION_FIELDS),
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
        records = self.parse_response(self._search(body), self.timestamp_field, self.message_field)
        return [r for r in records if is_error(r)]

    def count_total(self, time_range: TimeRange) -> int | None:
        body = {"query": {"bool": {"filter": self._filters(time_range)}}}
        req = urllib.request.Request(
            f"{self.url}/{self.index}/_count",
            data=json.dumps(body).encode(),
            headers=self.headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return int(json.load(resp).get("count", 0))

    def _search(self, body: dict[str, Any]) -> dict:
        req = urllib.request.Request(
            f"{self.url}/{self.index}/_search",
            data=json.dumps(body).encode(),
            headers=self.headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        payload = self._search(self.build_query(time_range, contains, limit))
        return self.parse_response(payload, self.timestamp_field, self.message_field)

    @staticmethod
    def parse_response(
        payload: dict, timestamp_field: str = "@timestamp", message_field: str = "message"
    ) -> list[LogRecord]:
        records: list[LogRecord] = []
        for hit in payload.get("hits", {}).get("hits", []):
            fields = flatten(hit.get("_source", {}))
            ts = _parse_ts(fields.get(timestamp_field))
            message = fields.get(message_field)
            if message is None:
                continue
            record = parse_line(str(message), timestamp=ts)
            record.size = len(json.dumps(hit.get("_source", {})).encode())  # the stored document
            record.fields = {**fields, **record.fields}
            records.append(record)
        return records


def flatten(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{"log": {"origin": {"file": {"line": 3}}}}`` -> ``{"log.origin.file.line": 3}``.

    Documents can mix nested objects and dotted keys (ECS does); both end up dotted.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out
