"""Sentry adapter using its web API (stdlib only).

Sentry already groups errors into issues; logsite reads the error events of a project (the
exception chain, each frame and its line of code, the trace id, the release) and puts them
through the same grouping, history, incidents and ``repro`` as errors from logs, mapped to
the code at your commit. Needs an auth token with ``event:read`` (``project:read`` too, on
self-hosted Sentry).
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from ..models import LogRecord, parse_iso
from .base import LogBackend, TimeRange

PAGE = 100  # events per request, Sentry's maximum
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


class SentryBackend(LogBackend):
    name = "sentry"

    def __init__(
        self,
        org: str,
        project: str,
        token: str | None,
        url: str = "https://sentry.io",
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError(
                "the sentry backend needs a token: set token_env, e.g. SENTRY_AUTH_TOKEN"
            )
        self.url = url.rstrip("/")
        self.org = org
        self.project = project
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    def _get(self, url: str) -> tuple[list, str | None]:
        """One page of results, and the next page's URL."""
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            m = _NEXT.search(resp.headers.get("Link") or "")
            return json.load(resp), (m[1] if m else None)

    def events(self, time_range: TimeRange, limit: int = 5000) -> list[dict]:
        params = {
            "full": "true",
            "start": time_range.start.isoformat(),
            "end": time_range.end.isoformat(),
            "per_page": str(PAGE),
        }
        org, project = urllib.parse.quote(self.org), urllib.parse.quote(self.project)
        url: str | None = (
            f"{self.url}/api/0/projects/{org}/{project}/events/?{urllib.parse.urlencode(params)}"
        )
        out: list[dict] = []
        while url and len(out) < limit:
            page, url = self._get(url)
            out += [e for e in page if isinstance(e, dict)]
        return out[:limit]

    def query_logs(
        self, time_range: TimeRange, contains: str | None = None, limit: int = 5000
    ) -> list[LogRecord]:
        records = [event_record(e, self.project) for e in self.events(time_range, limit)]
        if contains:
            records = [r for r in records if contains in r.message or contains in json.dumps(
                r.fields, default=str)]  # fmt: skip
        return records

    def query_errors(self, time_range: TimeRange, limit: int = 5000) -> list[LogRecord]:
        return self.query_logs(time_range, limit=limit)  # Sentry events are all errors


def event_record(event: dict, project: str | None = None) -> LogRecord:
    """A Sentry event as a record: its exception chain in structlog's ``dict_tracebacks``
    shape (the raised exception first), with each frame's line of code."""
    tags = {t.get("key"): t.get("value") for t in event.get("tags") or [] if isinstance(t, dict)}
    contexts = event.get("contexts") or {}
    platform = event.get("platform") or ""
    chain = []
    for entry in event.get("entries") or []:
        if entry.get("type") != "exception":
            continue
        for value in (entry.get("data") or {}).get("values") or []:  # oldest (root cause) first
            frames = ((value.get("stacktrace") or {}).get("frames")) or []
            chain.append({
                "exc_type": value.get("type") or "Error",
                "exc_value": value.get("value") or "",
                "frames": [_frame(f, platform) for f in frames if isinstance(f, dict)],
            })  # fmt: skip
    fields: dict = {
        "level": event.get("level") or tags.get("level") or "error",
        "service": tags.get("server_name") or project,
        "sentry.event_id": event.get("eventID") or event.get("id"),
        "sentry.issue_id": event.get("groupID"),
    }
    if chain:
        fields["exception"] = chain[::-1]  # the raised exception first, as structlog does
    trace = contexts.get("trace") if isinstance(contexts.get("trace"), dict) else {}
    if trace.get("trace_id"):
        fields["trace_id"] = trace["trace_id"]
    if tags.get("release") or event.get("release"):
        release = event.get("release")
        fields["release"] = tags.get("release") or (
            release.get("version") if isinstance(release, dict) else release
        )
    for key in ("environment", "transaction", "url"):
        if tags.get(key):
            fields[key] = tags[key]
    message = (
        event.get("title")
        or event.get("message")
        or (f"{chain[-1]['exc_type']}: {chain[-1]['exc_value']}" if chain else "error")
    )
    ts = event.get("dateCreated") or event.get("dateReceived")
    try:
        timestamp = parse_iso(ts) if isinstance(ts, str) else None
    except ValueError:
        timestamp = None
    return LogRecord(timestamp=timestamp, message=str(message), fields={
        k: v for k, v in fields.items() if v is not None})  # fmt: skip


def _frame(f: dict, platform: str) -> dict:
    """A Sentry frame as structlog's: ``filename``, ``lineno``, ``name``, ``line`` (its code)."""
    filename = f.get("filename") or ""
    abs_path = f.get("absPath") or ""
    path = abs_path if abs_path.startswith(("/", "\\")) or abs_path[1:3] == ":\\" else filename
    module = f.get("module") or ""
    if platform == "java" and module and "/" not in path and "." in module:
        # "Checkout.java" in com.acme.shop.Checkout -> com/acme/shop/Checkout.java
        path = module.rsplit(".", 1)[0].replace(".", "/") + "/" + path
    line = f.get("lineNo") or 0
    code = next(
        (text for n, text in f.get("context") or [] if n == line and isinstance(text, str)), None
    )
    return {
        "filename": path or module or "?",
        "lineno": line,
        "name": f.get("function") or "?",
        "line": code.strip() if code else None,
    }
