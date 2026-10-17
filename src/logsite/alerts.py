"""Alert enrichment: when Grafana fires an alert, send the error summary with it.

``logsite alert-server`` receives Grafana's webhook contact point, summarizes the errors in
the alert's window (grouped by cause, with the line of code that raised them), and posts it
to Slack or another webhook. ``GET /errors`` serves the same summary as JSON.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .models import parse_iso

log = logging.getLogger(__name__)

MAX_BODY = 5 * 1024 * 1024  # Grafana batches alerts; this is plenty


@dataclass
class AlertsConfig:
    slack_webhook: str | None = None  # Slack incoming webhook URL
    webhook: str | None = None  # any URL: gets the summary as JSON
    token: str | None = None  # required as "Authorization: Bearer <token>" or ?token=
    lookback_minutes: float = 30.0  # summarize from this long before the alert started
    top: int = 3  # error groups per alert
    selector_labels: list[str] = field(default_factory=lambda: ["service_name"])

    @classmethod
    def from_dict(cls, data: Mapping) -> AlertsConfig:
        known = set(cls.__dataclass_fields__) | {
            f"{k}_env" for k in ("slack_webhook", "webhook", "token")
        }
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown [alerts] key(s) {sorted(unknown)}")
        values = {k: v for k, v in data.items() if not k.endswith("_env")}
        for key in ("slack_webhook", "webhook", "token"):
            env = data.get(f"{key}_env")
            if env and values.get(key) is None:
                values[key] = os.environ.get(env)
        cfg = cls(**values)
        cfg.selector_labels = [str(x) for x in cfg.selector_labels]
        return cfg


@dataclass
class Alert:
    name: str
    status: str  # firing | resolved
    labels: dict[str, str]
    starts_at: datetime | None
    url: str | None  # the alert rule in Grafana
    dashboard_url: str | None
    summary: str | None  # the rule's summary / description annotation
    silence_url: str | None = None


def parse_grafana(payload: Mapping[str, Any]) -> list[Alert]:
    """The alerts in a Grafana (unified alerting) or Alertmanager webhook payload."""
    alerts = []
    for a in payload.get("alerts") or []:
        if not isinstance(a, dict):
            continue
        labels = {str(k): str(v) for k, v in (a.get("labels") or {}).items()}
        annotations = a.get("annotations") or {}
        started = a.get("startsAt")
        try:
            starts_at = parse_iso(started) if started and not started.startswith("0001") else None
        except ValueError:
            starts_at = None
        alerts.append(
            Alert(
                name=labels.get("alertname", "alert"),
                status=str(a.get("status", payload.get("status", "firing"))),
                labels=labels,
                starts_at=starts_at,
                url=a.get("generatorURL") or None,
                dashboard_url=a.get("panelURL") or a.get("dashboardURL") or None,
                summary=annotations.get("summary") or annotations.get("description"),
                silence_url=a.get("silenceURL") or None,
            )
        )
    return alerts


_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def narrow_selector(selector: str, labels: Mapping[str, str], names: list[str]) -> str:
    """Add the alert's labels (those in ``names``) to a LogQL stream selector, so the
    summary covers the service that alerted: ``{env="prod"}`` + service_name=checkout ->
    ``{env="prod", service_name="checkout"}``. Labels already in the selector are kept."""
    body = selector.strip()
    if not (body.startswith("{") and body.endswith("}")):
        return selector
    inner = body[1:-1].strip()
    present = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!=|!~|=)", inner))
    extra = [
        f"{n}={json.dumps(labels[n])}"
        for n in names
        if n in labels and n not in present and _LABEL_NAME.match(n)
    ]
    if not extra:
        return selector
    return "{" + ", ".join([inner, *extra] if inner else extra) + "}"


def window_for(alert: Alert, lookback_minutes: float, now: datetime) -> tuple[datetime, datetime]:
    start = (alert.starts_at or now) - timedelta(minutes=lookback_minutes)
    return min(start, now - timedelta(minutes=1)), now


# --- messages ---------------------------------------------------------------------------------


def _when(ts: datetime | None) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M UTC") if ts else "?"


def _link(text: str, url: str | None, slack: bool) -> str:
    if not url:
        return f"`{text}`" if slack else text
    return f"<{url}|{text}>" if slack else f"{text} ({url})"


def render(
    alert: Alert,
    summary: dict,
    *,
    slack: bool,
    code_line: Any = None,
    code_url: Any = None,
    trace_field: str = "trace_id",
) -> str:
    """The message for one alert: ``summary`` is ``errors.summary_json``. ``code_line``
    (``path:line -> str | None``) and ``code_url`` (``path:line -> url | None``) add the
    source line and a link to it."""
    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    head = f"{':red_circle: ' if slack else ''}{bold(alert.name)}"
    if alert.summary:
        head += f": {alert.summary}"
    head += f" (firing since {_when(alert.starts_at)})"
    groups = summary["groups"]
    total, new = summary["events"], sum(1 for g in groups if g["new"])
    line2 = f"{total:,} error(s) in {summary['groups_total']} group(s)"
    if new:
        line2 += f", {bold(f'{new} new')}"
    if summary.get("truncated"):
        line2 += " (capped: counts are a lower bound)"
    if alert.url or alert.dashboard_url:
        line2 += " · " + _link("Open in Grafana", alert.dashboard_url or alert.url, slack)
    if alert.silence_url and slack:
        line2 += " · " + _link("Silence", alert.silence_url, slack)
    out = [head, line2]
    if not groups:
        out.append("No error logs in this window: the alert isn't about logged errors.")
    for i, g in enumerate(groups, 1):
        trend = "new" if g["new"] else (g["trend"] or "")
        out += [
            "",
            f"{i}. {bold(g['title'][:150])}  ·  {g['count']:,}"
            + (f"  ·  {bold(trend)}" if trend else ""),
        ]
        causes = g["causes"]
        if causes:
            origin = f" ({g['origin']})" if g.get("origin") else ""
            out.append(f"    root cause: {causes[0][:150]}{origin}")
        if g["raised_at"]:
            code = code_line(g["raised_at"]) if code_line else None
            url = code_url(g["raised_at"]) if code_url else None
            text = f"    raised: {_link(g['raised_at'], url, slack)}"
            if code:
                text += f"  `{code[:100]}`" if slack else f"   {code[:100]}"
            out.append(text)
        if g["logged_at"]:
            url = code_url(g["logged_at"]) if code_url else None
            out.append(f"    logged: {_link(g['logged_at'], url, slack)}")
        trace = (g["sample"].get("ids") or {}).get(trace_field) or (
            g["sample"].get("ids") or {}
        ).get("trace_id")
        if trace:
            out.append(f"    example: `logsite trace {trace}`")
    rest = summary["groups_total"] - len(groups)
    if rest > 0:
        out += ["", f"+ {rest} more group(s): `logsite errors`"]
    return "\n".join(out)


def post_json(url: str, body: dict, timeout: float = 10.0) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


# --- the server -------------------------------------------------------------------------------


class Enricher:
    """Turns alerts into summaries. ``summarize(start, end, selector_labels) -> dict``
    builds a summary (``errors.summary_json``) for a window, optionally narrowed by labels."""

    def __init__(self, cfg: AlertsConfig, summarize, *, code_line=None, code_url=None,
                 trace_field: str = "trace_id", send=post_json, now=None) -> None:  # fmt: skip
        self.cfg = cfg
        self.summarize = summarize
        self.code_line, self.code_url = code_line, code_url
        self.trace_field = trace_field
        self.send = send
        self.now = now or (lambda: datetime.now(timezone.utc))

    def handle(self, payload: Mapping[str, Any]) -> list[dict]:
        results = []
        for alert in parse_grafana(payload):
            if alert.status != "firing":
                results.append({"alert": alert.name, "status": alert.status, "skipped": True})
                continue
            start, end = window_for(alert, self.cfg.lookback_minutes, self.now())
            labels = {k: v for k, v in alert.labels.items() if k in self.cfg.selector_labels}
            summary = self.summarize(start, end, labels)
            summary["groups"] = summary["groups"][: self.cfg.top]
            kw = {
                "code_line": self.code_line,
                "code_url": self.code_url,
                "trace_field": self.trace_field,
            }
            text = render(alert, summary, slack=False, **kw)
            sent = []
            if self.cfg.slack_webhook:
                self.send(
                    self.cfg.slack_webhook, {"text": render(alert, summary, slack=True, **kw)}
                )
                sent.append("slack")
            if self.cfg.webhook:
                self.send(
                    self.cfg.webhook,
                    {"alert": alert.name, "labels": alert.labels, "text": text, "summary": summary},
                )
                sent.append("webhook")
            results.append(
                {
                    "alert": alert.name,
                    "status": "firing",
                    "sent": sent,
                    "text": text,
                    "summary": summary,
                }
            )
        return results


def make_server(host: str, port: int, enricher: Enricher, token: str | None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "logsite"

        def log_message(self, fmt, *args):  # quieter than the default stderr line per request
            log.info("%s %s", self.address_string(), fmt % args)

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            given = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
            if given is None:
                given = (parse_qs(urlparse(self.path).query).get("token") or [None])[0]
            return given is not None and secrets.compare_digest(given, token)

        def _reply(self, status: int, body: Any) -> None:
            data = json.dumps(body, indent=2).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/healthz":
                return self._reply(200, {"ok": True})
            if not self._authorized():
                return self._reply(401, {"error": "missing or wrong token"})
            if url.path != "/errors":
                return self._reply(404, {"error": "not found"})
            q = parse_qs(url.query)
            try:
                end = _time(q.get("to", [None])[0]) or datetime.now(timezone.utc)
                start = _time(q.get("from", [None])[0]) or end - timedelta(hours=1)
            except ValueError as exc:
                return self._reply(400, {"error": str(exc)})
            labels = {k: v[0] for k, v in q.items() if k in enricher.cfg.selector_labels}
            try:
                self._reply(200, enricher.summarize(start, end, labels))
            except Exception as exc:  # the backend is down: say so, keep serving
                self._reply(502, {"error": f"couldn't query the backend: {exc}"})

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path not in ("/grafana/alert", "/alert"):
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return self._reply(401, {"error": "missing or wrong token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, {"error": "payload too large"})
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "body is not JSON"})
            try:
                results = enricher.handle(payload)
            except Exception as exc:
                log.exception("couldn't enrich the alert")
                return self._reply(502, {"error": f"couldn't build the summary: {exc}"})
            self._reply(200, {"alerts": results})

    return ThreadingHTTPServer((host, port), Handler)


def _time(value: str | None) -> datetime | None:
    """A query time: ISO 8601, epoch seconds, or epoch milliseconds (Grafana's ${__from})."""
    if not value:
        return None
    if value.isdigit():
        n = int(value)
        return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, tz=timezone.utc)
    ts = parse_iso(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def with_selector(backend_cfg: dict, labels: Mapping[str, str], names: list[str]) -> dict:
    """The backend config narrowed to the alert's labels (Loki only; others unchanged)."""
    if backend_cfg.get("type") != "loki" or not labels:
        return backend_cfg
    return {**backend_cfg, "selector": narrow_selector(backend_cfg["selector"], labels, names)}


__all__ = [
    "Alert",
    "AlertsConfig",
    "Enricher",
    "make_server",
    "narrow_selector",
    "parse_grafana",
    "render",
    "with_selector",
]
