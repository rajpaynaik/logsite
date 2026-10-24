"""Watch production: check the logs on a schedule, keep every error, post what's new to Slack.

``logsite watch`` reads the errors of the last few minutes from the log store (any backend),
groups them by cause, stores each occurrence in the error store (``store.py``), records the
history, and sends one message when something needs a person:

- **new**: an error group never seen before. In the first hour, before the history knows
  what's normal, an error that wasn't in the window before; so the first check doesn't
  page about everything that already fails;
- **regression**: a group that had stopped, with the fix it had, is back;
- **spike**: a group well above its usual rate.

Each group alerts at most once per ``cooldown_minutes`` for each reason. The first check reads
the last day, so the errors are there to see at once, and sends nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .backends import LogBackend, TimeRange
from .errors import AppFiles, ErrorGroup, group_json, summarize_errors
from .store import Store, event_row

KINDS = ("new", "regression", "spike")
BACKFILL = timedelta(hours=24)  # the first check reads this far back, and alerts on nothing
LABELS = {"new": "new", "regression": "back", "spike": "spiking"}


@dataclass
class WatchConfig:
    every: str = "5m"  # how often to check
    lookback: str = "15m"  # each check reads this far back; overlaps are stored once
    alert_on: list[str] = field(default_factory=lambda: list(KINDS))
    cooldown_minutes: float = 60.0  # per group and reason
    top: int = 5  # error groups per message
    spike_min_count: int = 20  # a spike: at least this many in the window, and
    spike_ratio: float = 5.0  # ... this many times the usual rate

    @classmethod
    def from_dict(cls, data: Mapping) -> WatchConfig:
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown [watch] key(s) {sorted(unknown)}")
        cfg = cls(**dict(data))
        bad = set(cfg.alert_on) - set(KINDS)
        if bad:
            raise ValueError(f"[watch] alert_on: unknown {sorted(bad)} (use {list(KINDS)})")
        return cfg


@dataclass
class CheckResult:
    window: TimeRange
    events: int  # errors read in the window
    stored: int  # of them, new to the store
    groups: list[ErrorGroup]
    alerts: list[tuple[ErrorGroup, str]]  # (group, kind) sent this time
    message: str | None = None
    sent: list[str] = field(default_factory=list)
    first: bool = False  # the first check: the last day stored, nothing sent
    exported: int = 0  # events appended to the warehouse ([export])
    export_error: str | None = None


def alert_kind(g: ErrorGroup, watch: WatchConfig) -> str | None:
    """Why ``g`` needs a person now, if it does."""
    levels = {k.lower() for k in g.levels}
    if levels and not levels - {"warning", "warn", "info", "debug"}:
        return None  # warnings with a traceback: not a failure
    h = g.history
    if h is not None and h.regression:
        return "regression"
    if h is None or h.watching_since is None:
        # no history, or watching for less than an hour: new means not in the window before
        # (and, with a history, never recorded); what already failed before logsite started
        # was in the window before too, so it doesn't page
        if g.is_new and (h is None or not h.known):
            return "new"
    elif h.new_ever:
        return "new"
    if (g.baseline is not None and g.count >= watch.spike_min_count
            and g.count >= watch.spike_ratio * max(g.baseline, 1)):  # fmt: skip
        return "spike"
    return None


def check(
    cfg,
    backend: LogBackend,
    sites: list,
    app: AppFiles,
    store: Store,
    now: datetime,
    *,
    send: Callable[[str, dict], None] | None = None,
    code_line=None,
    code_url=None,
    limit: int = 5000,
) -> CheckResult:
    """One round: read, group, store, record the history, alert."""
    from . import history
    from .cli import _duration

    first = store.get_meta("checked_at") is None
    window = TimeRange(now - max(_duration(cfg.watch.lookback), BACKFILL if first else timedelta()),
                       now)  # fmt: skip
    rows: list[dict] = []

    def collect(group, record, info, service, level, trace_id):
        rows.append(event_row(group, record, info, service, level, trace_id, cfg.leaks))

    fields = list(dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"]))
    summary = summarize_errors(
        backend, sites, app, window, window.previous(), cfg.lint, cfg.leaks, limit, fields,
        on_event=collect,
    )  # fmt: skip
    history.apply(cfg, summary.groups, window.start, window.end, record=True)
    stored = store.write(summary.groups, rows)
    exported, export_error = _export(cfg, rows, summary.groups, now)
    _line_counts(store, backend, sites, now, limit)
    _prune(store, cfg.store.retention_days, now)
    store.set_meta("checked_at", now.astimezone(timezone.utc).isoformat())
    if first:  # what already failed today is stored and shown, not paged about
        return CheckResult(window, summary.events, stored, summary.groups, [], first=True,
                           exported=exported, export_error=export_error)  # fmt: skip

    cooldown = timedelta(minutes=cfg.watch.cooldown_minutes)
    alerts = []
    for g in summary.groups:
        kind = alert_kind(g, cfg.watch)
        if kind is None or kind not in cfg.watch.alert_on:
            continue
        last = store.last_alert(g.id, kind)
        if last is not None and now - last < cooldown:
            continue
        alerts.append((g, kind))
    result = CheckResult(window, summary.events, stored, summary.groups, alerts,
                         exported=exported, export_error=export_error)  # fmt: skip
    if not alerts:
        return result
    project = cfg.timeline.service or cfg.root.name
    shown = alerts[: cfg.watch.top]
    kw = {"code_line": code_line, "code_url": code_url, "trace_field": cfg.timeline.field_name}
    result.message = render(project, shown, len(alerts), window, slack=False, **kw)
    slack_text = render(project, shown, len(alerts), window, slack=True, **kw)
    acfg = cfg.alerts
    if send is not None and acfg.slack_webhook:
        send(acfg.slack_webhook, {"text": slack_text})
        result.sent.append("slack")
    if send is not None and acfg.webhook:
        send(acfg.webhook, {
            "text": result.message,
            "alerts": [{"kind": k, **group_json(g)} for g, k in alerts],
        })  # fmt: skip
        result.sent.append("webhook")
    for g, kind in alerts:
        store.record_alert(g.id, kind, result.sent, result.message, now)
    return result


def render(project: str, alerts: list[tuple[ErrorGroup, str]], total: int, window: TimeRange,
           *, slack: bool, code_line=None, code_url=None,
           trace_field: str = "trace_id") -> str:  # fmt: skip
    """The message: what's new, back or spiking, each with where it was raised."""
    from .alerts import group_lines

    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    counts = {k: sum(1 for _, kind in alerts if kind == k) for k in KINDS}
    parts = []
    if counts["new"]:
        parts.append(f"{counts['new']} new error(s)")
    if counts["regression"]:
        parts.append(f"{counts['regression']} back after a fix")
    if counts["spike"]:
        parts.append(f"{counts['spike']} spiking")
    minutes = round((window.end - window.start).total_seconds() / 60)
    head = f"{':red_circle: ' if slack else ''}{bold(project)}: {', '.join(parts)}"
    out = [f"{head} (last {minutes} min)"]
    for i, (g, kind) in enumerate(alerts, 1):
        out += group_lines(i, group_json(g), slack=slack, code_line=code_line,
                           code_url=code_url, trace_field=trace_field,
                           label=LABELS[kind])  # fmt: skip
    if total > len(alerts):
        out += ["", f"+ {total - len(alerts)} more: `logsite errors`"]
    return "\n".join(out)


def _export(cfg, rows: list[dict], groups: list, now: datetime) -> tuple[int, str | None]:
    """Append this check's new events, and its groups, to the warehouse, if there's one."""
    export = getattr(cfg, "export", None)
    if export is None or export.type is None:
        return 0, None
    from .export import exporter
    from .export import rows as export_rows

    new = [r for r in rows if r.get("_new")]
    try:
        events, group_rows = export_rows(cfg.timeline.service or cfg.root.name, new, groups, now)
        exporter(export).append(events, group_rows)
    except Exception as exc:  # the warehouse down: the errors are in the store all the same
        return 0, f"{export.type}: {str(exc)[:200]}"
    return len(new), None


def _line_counts(store: Store, backend: LogBackend, sites: list, now: datetime,
                 limit: int) -> None:  # fmt: skip
    """Lines and bytes per log call for each hour that ended since the last check (at most
    a day's worth per round, starting with the last hour on the first)."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    done = store.get_meta("line_counts_until")
    start = datetime.fromisoformat(done) if done else hour - timedelta(hours=1)
    start = max(start, hour - timedelta(hours=24))
    by_id = {s.id: s for s in sites}
    while start < hour:
        end = start + timedelta(hours=1)
        try:
            usage = backend.usage_by_line(sites, TimeRange(start, end), limit=limit)
        except Exception:  # volume is extra; errors are stored without it
            return
        store.write_line_counts(start, usage, by_id)
        store.set_meta("line_counts_until", end.isoformat())
        start = end


def _prune(store: Store, retention_days: float, now: datetime) -> None:
    last = store.get_meta("pruned_at")
    if last and now - datetime.fromisoformat(last) < timedelta(days=1):
        return
    store.prune(retention_days, now)
    store.set_meta("pruned_at", now.astimezone(timezone.utc).isoformat())
