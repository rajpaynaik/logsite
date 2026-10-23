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
    # alerts with the same cause within this long get one line, not another full report
    dedupe_minutes: float = 30.0

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


_PACKAGE = re.compile(r"^(?:[a-z_$][\w$]*\.)+(?=[A-Z])")  # "com.example.orders." in a class name
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
    incidents = summary.get("incidents") or []
    if incidents:
        kw = {"code_line": code_line, "code_url": code_url, "trace_field": trace_field}
        shown: set[str] = set()
        for n, inc in enumerate(incidents[:MAX_INCIDENTS], 1):
            out += _incident_lines(n, inc, slack=slack, bold=bold, **kw)
            shown |= {inc["cause"]["id"], *(e["id"] for e in inc["effects"])}
        others = [g for g in groups if g["id"] not in shown]
        if others:
            out += ["", bold("Also failing")]
            for g in others[:3]:
                trend = "new" if g["new"] else (g["trend"] or "")
                out.append(
                    f"• {g['title'][:120]}  ·  {g['count']:,}" + (f"  ·  {trend}" if trend else "")
                )
        rest = summary["groups_total"] - len(shown) - min(len(others), 3)
        if rest > 0:
            out += [f"+ {rest} more group(s): `logsite errors`"]
        return "\n".join(out)
    if not groups:
        out.append("No error logs in this window: the alert isn't about logged errors.")
    for i, g in enumerate(groups, 1):
        out += group_lines(i, g, slack=slack, code_line=code_line, code_url=code_url,
                           trace_field=trace_field)  # fmt: skip
    rest = summary["groups_total"] - len(groups)
    if rest > 0:
        out += ["", f"+ {rest} more group(s): `logsite errors`"]
    return "\n".join(out)


def group_lines(i: int, g: dict, *, slack: bool, code_line=None, code_url=None,
                trace_field: str = "trace_id", label: str | None = None) -> list[str]:  # fmt: skip
    """One error group (``errors.group_json``) as a numbered block: the error, its count and
    trend (or ``label``), the root cause, where it was raised and logged, an example."""
    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    h = g.get("history") or {}
    trend = label or (
        "back" if h.get("regression") else ("new" if g["new"] else (g["trend"] or ""))
    )
    out = [
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
    ids = g["sample"].get("ids") or {}
    trace = ids.get(trace_field) or ids.get("trace_id")
    if trace:
        out.append(f"    example: `logsite trace {trace}`")
    return out + _history_lines(h)


MAX_INCIDENTS = 2
EXCERPT_LINES = 8


def _excerpt(sample: dict, raised_at: str | None) -> list[str]:
    """The exact issue as it was logged: the message, the exception chain (each exception's
    first line) and the frame of yours where it was raised. Values are already masked."""
    lines = [sample["message"]] if sample.get("message") else []
    tb = sample.get("traceback") or ""
    own = raised_at.rsplit("/", 1)[-1].rsplit(":", 1)[0] if raised_at else None
    for line in tb.splitlines()[1:] if lines and tb.startswith(lines[0][:40]) else tb.splitlines():
        text = line.rstrip()
        stripped = text.strip()
        if not stripped or stripped.startswith("..."):
            continue
        is_frame = stripped.startswith(("at ", "File ", "goroutine ")) or text[:1].isspace()
        if not is_frame or (own and own in stripped):
            if text not in lines:
                lines.append(text if not is_frame else "    " + stripped)
        if len(lines) >= EXCERPT_LINES:
            break
    return [x[:160] for x in lines]


def _incident_lines(n: int, inc: dict, *, slack: bool, bold, code_line, code_url,
                    trace_field: str) -> list[str]:  # fmt: skip
    """One incident as evidence: what failed, the log, where, what followed, the likely
    cause with what supports it, and what to check first."""
    g = inc["cause"]
    since = _when(_parse(inc["started"])) if inc.get("started") else "?"
    state = "new" if g["new"] else (g["trend"] or "")
    h = g.get("history") or {}
    if h.get("regression"):
        state = "back after a fix"
    out = ["", f"{bold(f'{n}. What failed')}: {g['title'][:150]}"]
    count = f"{g['count']:,} error(s) since {since}"
    if inc["events"] > g["count"]:
        count += f" ({inc['events']:,} with what followed)"
    out.append(f"    {count}" + (f" · {state}" if state else ""))
    if g.get("causes"):
        origin = f" (thrown in {g['origin']})" if g.get("origin") else ""
        out.append(f"    {bold('Root cause')}: {g['causes'][0][:150]}{origin}")
    where = g.get("raised_at") or g.get("logged_at")
    if where:
        url = code_url(where) if code_url else None
        code = code_line(where) if code_line else None
        fn = f" in {g['raised_in']}()" if g.get("raised_at") and g.get("raised_in") else ""
        label = "Raised at" if g.get("raised_at") else "Logged at"
        text = f"    {bold(label)}: {_link(where, url, slack)}{fn}"
        if code:
            text += f"  `{code[:110]}`" if slack else f"   {code[:110]}"
        out.append(text)
    shared = inc.get("shared_root_cause")
    if shared and len(shared["entry_points"]) > 1:
        entries = shared["entry_points"]
        out.append(f"    {bold('Same root cause')} from {len(entries)} entry points: "
                   + ", ".join(entries))  # fmt: skip
    for text in inc.get("impact") or []:
        out.append(f"    {bold('Impact')}: {text}")
    tenant = g.get("tenant")
    if tenant and tenant["share"] >= 0.5:
        out.append(f"    {bold('Tenant')}: {tenant['share']:.0%} from {tenant['top']} "
                   f"({tenant['tenants']} affected)")  # fmt: skip
    excerpt = _excerpt(g.get("sample") or {}, g.get("raised_at"))
    if excerpt:
        stamp = (g.get("sample") or {}).get("timestamp")
        when = f" (latest, {_when(_parse(stamp))})" if stamp else ""
        out.append(f"    {bold('From the log')}{when}:")
        if slack:
            out.append("```" + "\n".join(excerpt) + "```")
        else:
            out += [f"      {x}" for x in excerpt]
    warnings = [r for r in inc.get("related") or [] if r["level"] == "warning"]
    for i, r in enumerate(warnings[:2]):
        head = f"{bold('Warnings in those requests')}: " if i == 0 else "      "
        out.append(f"    {head}{r['message'][:200]}")
    for i, e in enumerate(inc.get("effects", [])[:3]):
        why = f" — {e['linked_by']}" if e.get("linked_by") else ""
        head = f"{bold('Then')}: " if i == 0 else "      "
        out.append(f"    {head}{e['title'][:110]} · {e['count']:,}{why}")
    if h.get("regression"):
        fixes = ", ".join(f"{c['sha']} {c['summary']}" for c in h.get("fixed_by") or [])
        gone = f" → {(h.get('resolved_at') or '')[:10]}" if h.get("resolved_at") else ""
        if h.get("resolved_release"):
            gone += f", gone after {h['resolved_release']}"
        out.append(f"    {bold('Seen before')}: {(h.get('first_seen') or '')[:10]}{gone}"
                   + (f"; fixed then by {fixes}" if fixes else ""))  # fmt: skip
    d = inc.get("diagnosis") or {}
    hyps = d.get("hypotheses") or []
    if d.get("strength") == "unclear" or not hyps:
        out.append(f"    {bold('Likely cause')}: unclear, not enough evidence to rank one")
    else:
        top = hyps[0]
        out.append(f"    {bold('Likely cause')} ({d['strength']} evidence): {top['title']}")
        out += [f"      • {t}" for t in top["supporting"][:4]]
        out += [f"      • against: {t}" for t in top["against"][:2]]
        if len(hyps) > 1 and hyps[1]["score"] > 0:
            out.append(f"    {bold('Or')}: {hyps[1]['title']}")
        if top.get("next_check"):
            out.append(f"    {bold('Check first')}: {top['next_check']}")
    for gap in d.get("unknown") or []:
        if "order" in gap:
            out.append(f"    {bold('Order')}: {gap}")
    trace = ((g.get("sample") or {}).get("ids") or {}).get(trace_field) or g.get("example_trace")
    if trace:
        out.append(f"    {bold('Example run')}: `logsite trace {trace}`")
    return out


def _parse(value: str) -> datetime:
    return parse_iso(value)


def _cause_key(summary: dict) -> str | None:
    incidents = summary.get("incidents") or []
    if incidents:
        return incidents[0]["cause"]["id"]
    groups = summary.get("groups") or []
    return groups[0]["id"] if groups else None


def _cause_title(summary: dict) -> str:
    incidents = summary.get("incidents") or []
    g = incidents[0]["cause"] if incidents else (summary.get("groups") or [{}])[0]
    return (g.get("root_cause") or g.get("title") or "")[:150]


def _services(summary: dict) -> set[str]:
    out: set[str] = set()
    for inc in summary.get("incidents") or []:
        for g in [inc["cause"], *inc["effects"]]:
            out |= set(g.get("services") or {})
    for g in summary.get("groups") or []:
        out |= set(g.get("services") or {})
    return out


def render_followup(alert: Alert, summary: dict, first: dict, *, slack: bool) -> str:
    """One line for an alert whose cause was already reported."""
    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    head = f"{':link: ' if slack else ''}{bold(alert.name)}: same cause as {first['alert']} "
    head += f"({first['at']:%H:%M} UTC): {first['title']}"
    new = first.get("new_services") or []
    if new:
        head += f" · now also in {', '.join(new)}"
    n = len(first["alerts"])
    return head + f" · {n} alerts for this incident so far"


def _history_columns(h: dict) -> dict:
    """Flat history fields for a dashboard table: History, First seen, Fixed before by."""
    label = None
    if h.get("regression"):
        label = "↩ back" + (
            f", gone after {h['resolved_release']}" if h.get("resolved_release") else ""
        )
    elif h.get("new_ever"):
        label = "first time ever"
    elif h.get("known") and h.get("days_seen"):
        label = f"seen on {h['days_seen']} day(s)"
    fixes = h.get("fixed_by") or []
    fixed = "; ".join(f"{c['sha']} {c['summary']}" for c in fixes) or None
    if fixed and h.get("resolved_release"):
        fixed += f" ({h['resolved_release']})"
    return {
        "history_label": label,
        "first_seen_ever": (h.get("first_seen") or "")[:10] or None,
        "fixed_before": fixed,
    }


def _history_lines(h: dict) -> list[str]:
    if h.get("regression"):
        first, gone = (h.get("first_seen") or "")[:10], (h.get("resolved_at") or "")[:10]
        text = f"    ↩ came back: seen {first} → {gone}"
        if h.get("resolved_release"):
            text += f", gone after {h['resolved_release']}"
        lines = [text]
        for c in h.get("fixed_by") or []:
            lines.append(f"    fixed before by: {c['sha']} {c.get('author', '')}: {c['summary']}")
        return lines
    if h.get("new_ever"):
        return ["    first time ever"]
    if h.get("known") and h.get("first_seen"):
        return [f"    seen since {h['first_seen'][:10]} ({h['days_seen']} day(s))"]
    return []


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
                 trace_field: str = "trace_id", send=post_json, now=None,
                 events=None) -> None:  # fmt: skip
        self.cfg = cfg
        self.events = events  # (start, end) -> history events, for Grafana annotations
        self.summarize = summarize
        self.code_line, self.code_url = code_line, code_url
        self.trace_field = trace_field
        self.send = send
        self.sent: dict[str, dict] = {}  # cause id -> the first message about it
        self.now = now or (lambda: datetime.now(timezone.utc))

    def with_links(self, summary: dict) -> dict:
        """Add each group's code links and a short trend, for a dashboard table."""
        for g in summary.get("groups", []):
            for key in ("raised_at", "logged_at"):
                where = g.get(key)
                g[key.replace("_at", "_url")] = (
                    self.code_url(where) if where and self.code_url else None
                )
                g[key.replace("_at", "_code")] = (
                    self.code_line(where) if where and self.code_line else None
                )
                # "OrderRepository.java:87": short enough for a table column
                g[key.replace("_at", "_name")] = where.rsplit("/", 1)[-1] if where else None
            h = g.get("history") or {}
            g["trend_label"] = (
                "back" if h.get("regression") else "new" if g.get("new") else (g.get("trend") or "")
            )
            g.update(_history_columns(h))
            # "OrderProcessingException: ..." without its package, for narrow columns
            g["title_short"] = _PACKAGE.sub("", g.get("title") or "")
            g["root_cause_short"] = _PACKAGE.sub("", g.get("root_cause") or "") or None
        return summary

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
            first = self._same_cause(alert, summary)
            if first is not None:
                text = render_followup(alert, summary, first, slack=False)
                slack_text = render_followup(alert, summary, first, slack=True)
            else:
                text = render(alert, summary, slack=False, **kw)
                slack_text = render(alert, summary, slack=True, **kw)
            sent = []
            if self.cfg.slack_webhook:
                self.send(self.cfg.slack_webhook, {"text": slack_text})
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
                    "same_as": first["alert"] if first else None,
                }
            )
        return results

    def _same_cause(self, alert: Alert, summary: dict) -> dict | None:
        """The earlier message about this alert's cause, if it's recent; else remember this
        one as the first. One incident firing ten alerts sends one report."""
        key = _cause_key(summary)
        if key is None or self.cfg.dedupe_minutes <= 0:
            return None
        now = self.now()
        first = self.sent.get(key)
        services = _services(summary)
        if first and now - first["at"] <= timedelta(minutes=self.cfg.dedupe_minutes):
            first["alerts"].append(alert.name)
            new = services - first["services"]
            first["services"] |= services
            return {**first, "new_services": sorted(new)}
        self.sent[key] = {"at": now, "alert": alert.name, "alerts": [alert.name],
                          "services": services, "title": _cause_title(summary)}  # fmt: skip
        return None


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
            if url.path not in ("/errors", "/history/events"):
                return self._reply(404, {"error": "not found"})
            q = parse_qs(url.query)
            try:
                end = _time(q.get("to", [None])[0]) or datetime.now(timezone.utc)
                start = _time(q.get("from", [None])[0]) or end - timedelta(hours=1)
            except ValueError as exc:
                return self._reply(400, {"error": str(exc)})
            if url.path == "/history/events":
                if enricher.events is None:
                    return self._reply(404, {"error": "the history is turned off"})
                return self._reply(200, {"events": enricher.events(start, end)})
            labels = {
                k: v[0] for k, v in q.items() if k in enricher.cfg.selector_labels and v[0]
            }  # an empty dashboard variable means "all"
            try:
                self._reply(200, enricher.with_links(enricher.summarize(start, end, labels)))
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
