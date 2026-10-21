"""Log pipeline rules: drop or sample chosen log calls before they're shipped, so the noisy
lines stop costing money without waiting for a code change. Written as a Grafana Alloy
``loki.process`` component.

Which lines get a rule comes from ``[pipeline]`` in logsite.toml (``drop``, ``sample``) and,
optionally, from production volume: lines above ``max_per_window`` are sampled down to it,
and lines at a ``drop_levels`` level are dropped. Only log calls at a known low level (see
``ELIGIBLE``) can get a rule: warning and error lines, levels set at runtime and unknown
levels never do, and every rule also skips lines that look like errors.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .cost import CostConfig, format_bytes, format_money, monthly_costs
from .models import LogSite, Usage

NEVER_TOUCH = {"warning", "warn", "error", "exception", "critical", "fatal"}
# Fail closed: only log calls whose level in the code is one of these can get a rule. A
# level set at runtime (``log.log(level, ...)``, indexed as "log") or one we don't know
# could be an error, so it's skipped like warning and error lines.
ELIGIBLE = {"trace", "debug", "info", "notice", "success"}
# Template rules match the raw line loosely; these keep error-looking lines out of them.
ERROR_GUARD = "(?i)(error|exception|traceback|critical|fatal)"
MIN_TEMPLATE_TEXT = 8  # fixed characters a template needs to be matched safely
MATCH_MODES = ("auto", "location", "template")
# Where a new record starts, for joining stack traces (RE2): a timestamp (2026-10-18 14:22:31,
# 2026-10-18T…, [2026-…], Oct 18 14:22:31, 14:22:31), a JSON object, logfmt time= / level=,
# a klog header (E1018 …), or a Go panic. Every other line ("\tat …", "Caused by: …",
# "Traceback …", "... 12 more", a wrapped message) continues the record above it.
MULTILINE_FIRSTLINE = (
    r"^(\[?\d{4}-\d{2}-\d{2}[T ]|\{|[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}|\d{2}:\d{2}:\d{2}"
    r"|(time|ts|level)=|[IWEF]\d{4} |panic: |fatal error: )"
)
MULTILINE_MAX_LINES = 500  # Alloy's default is 128: shorter than many Java traces
FORMATS = ("json", "logfmt")


@dataclass
class PipelineConfig:
    drop: list[str] = field(default_factory=list)  # site ids ("app/health.py:12")
    sample: dict[str, float] = field(default_factory=dict)  # site id -> share kept, 0..1
    max_per_window: int | None = None  # sample any line above this many per window down to it
    drop_levels: list[str] = field(default_factory=list)  # e.g. ["debug"]: drop in production
    match: str = "auto"  # how Alloy recognises a line: location (file + line fields) | template
    format: str | None = None  # json | logfmt (default: the Loki backend's parser, else json)
    file_field: str | None = None  # default: the Loki backend's file_label, else "pathname"
    line_field: str | None = None  # default: the Loki backend's line_label, else "lineno"
    selector: str | None = None  # stream selector for template rules (default: the backend's)
    forward_to: str = "loki.write.default.receiver"
    multiline: bool = False  # join stack traces into one entry before shipping
    multiline_firstline: str | None = None  # RE2 for where a record starts (MULTILINE_FIRSTLINE)

    @classmethod
    def from_dict(cls, data: Mapping) -> PipelineConfig:
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown [pipeline] key(s) {sorted(unknown)}")
        cfg = cls(**{k: v for k, v in data.items()})
        cfg.drop = [str(s) for s in cfg.drop]
        cfg.sample = {str(k): float(v) for k, v in dict(cfg.sample).items()}
        for sid, rate in cfg.sample.items():
            if not 0 < rate < 1:
                raise ValueError(f"[pipeline] sample rate for {sid} must be between 0 and 1")
        if cfg.match not in MATCH_MODES:
            raise ValueError(f"[pipeline] match must be one of {', '.join(MATCH_MODES)}")
        if cfg.format is not None and cfg.format not in FORMATS:
            raise ValueError(f"[pipeline] format must be one of {', '.join(FORMATS)}")
        cfg.drop_levels = [str(x).lower() for x in cfg.drop_levels]
        return cfg


@dataclass
class Rule:
    site: LogSite
    action: str  # "drop" | "sample"
    rate: float | None  # share kept, for "sample"
    reason: str
    usage: Usage
    saved_bytes_per_month: float = 0.0
    saved_per_month: float = 0.0

    @property
    def kept(self) -> float:
        return 0.0 if self.action == "drop" else (self.rate or 0.0)


@dataclass
class Plan:
    rules: list[Rule]
    skipped: list[tuple[str, str]]  # (site id, why there's no rule)
    window_hours: float

    @property
    def saved_bytes_per_month(self) -> float:
        return sum(r.saved_bytes_per_month for r in self.rules)

    @property
    def saved_per_month(self) -> float:
        return sum(r.saved_per_month for r in self.rules)


def _round_rate(rate: float) -> float:
    """Two significant digits, so the config reads ``0.012`` rather than ``0.0123456``."""
    return float(f"{rate:.2g}")


def make_plan(
    sites: Iterable[LogSite],
    usage: Mapping[str, Usage],
    cfg: PipelineConfig,
    cost: CostConfig,
    window_hours: float,
) -> Plan:
    by_id = {s.id: s for s in sites}
    monthly = {c.site.id: c for c in monthly_costs(by_id.values(), usage, window_hours, cost)}
    wanted: dict[str, tuple[str, float | None, str]] = {}
    skipped: list[tuple[str, str]] = []

    for sid in cfg.drop:
        wanted[sid] = ("drop", None, "configured")
    for sid, rate in cfg.sample.items():
        wanted.setdefault(sid, ("sample", rate, "configured"))
    for sid, u in usage.items():
        site = by_id.get(sid)
        if site is None or sid in wanted:
            continue
        if site.level in cfg.drop_levels and u.count:
            wanted[sid] = ("drop", None, f"{site.level} level")
        elif cfg.max_per_window and u.count > cfg.max_per_window:
            rate = _round_rate(cfg.max_per_window / u.count)
            limit = f"{cfg.max_per_window:,}"
            wanted[sid] = ("sample", rate, f"{u.count:,} per {window_hours:g}h, over {limit}")

    rules = []
    for sid, (action, rate, reason) in wanted.items():
        site = by_id.get(sid)
        if site is None:
            skipped.append((sid, "not a log call in this project"))
            continue
        if site.level not in ELIGIBLE:
            if site.level in NEVER_TOUCH:
                why = f"{site.level} lines are never dropped or sampled"
            elif site.level == "log":
                why = "its level is set at runtime (it could be an error), so it's never touched"
            else:
                why = f"level {site.level!r} isn't known to be low, so it's never touched"
            skipped.append((sid, why))
            continue
        u = usage.get(sid, Usage())
        rule = Rule(site, action, rate, reason, u)
        line = monthly.get(sid)
        if line is not None:
            gone = 1 - rule.kept
            rule.saved_bytes_per_month = line.bytes_per_month * gone
            rule.saved_per_month = line.cost_per_month * gone
        rules.append(rule)
    rules.sort(key=lambda r: (r.saved_per_month, r.saved_bytes_per_month), reverse=True)
    return Plan(rules, skipped, window_hours)


# --- Alloy -------------------------------------------------------------------------------


def _str(value: str) -> str:
    """An Alloy (and LogQL) string literal: Go-style escapes."""
    return json.dumps(value, ensure_ascii=False)


def re2_escape(text: str) -> str:
    """Escape for RE2 (Loki, Alloy): unlike Python's ``re.escape``, never ``\\ `` (RE2
    rejects escaped spaces), and ``.`` as ``[.]`` so the config stays readable."""
    out = []
    for c in text:
        if c == ".":
            out.append("[.]")
        elif c.isascii() and not c.isalnum() and c not in " _/:,=@'\"%!<>;~#&`-":
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def location_regex(site: LogSite) -> str:
    """Matches ``path:line`` as production writes it: absolute or relative, any prefix."""
    return f"(.*/)?{re2_escape(site.path)}:{site.line}"


def template_regex(site: LogSite) -> str | None:
    """RE2 version of the site's message regex (placeholders match anything)."""
    from .models import _PLACEHOLDER  # the same placeholders the matcher uses

    if not site.template or site.literal_len < MIN_TEMPLATE_TEXT:
        return None
    parts, pos = [], 0
    for m in _PLACEHOLDER.finditer(site.template):
        parts.append(re2_escape(site.template[pos : m.start()]))
        parts.append("%" if m.group() == "%%" else ".*?")
        pos = m.end()
    parts.append(re2_escape(site.template[pos:]))
    return "".join(parts)


def _comment(rule: Rule, plan: Plan, cost: CostConfig) -> list[str]:
    template = rule.site.template.replace("\n", " ")[:80]
    lines = [f"// {rule.site.id}  {rule.site.level}({template!r})"]
    n = rule.usage.count
    if rule.action == "drop":
        what = f"drop ({rule.reason})"
    else:
        kept = rule.rate or 0
        if kept and (1 / kept).is_integer():
            keep = f"1 in {round(1 / kept):,}"
        else:
            keep = f"{kept:.0%}" if kept >= 0.01 else f"{kept:.2%}"
        what = f"keep {keep} ({rule.reason})"
    volume = f"{n:,} line(s)/{plan.window_hours:g}h" if n else f"none in {plan.window_hours:g}h"
    saved = ""
    if rule.saved_bytes_per_month:
        saved = f", saves {format_bytes(rule.saved_bytes_per_month)}/month"
        if cost.priced:
            saved += f" ({format_money(rule.saved_per_month, cost.currency)})"
    lines.append(f"// {what}: {volume}{saved}")
    return lines


def to_alloy(
    plan: Plan,
    cfg: PipelineConfig,
    cost: CostConfig,
    *,
    match: str,
    format: str,
    file_field: str,
    line_field: str,
    selector: str | None,
    generated_by: str,
) -> str:
    """A ``loki.process`` component applying the plan. ``match`` is ``location`` (by the
    file and line fields each record carries) or ``template`` (by the message text)."""
    if match == "template" and not selector and plan.rules:
        raise ValueError("template rules need a stream selector: set [pipeline] selector")
    out = [
        "// logsite pipeline rules: drop or sample chosen log calls before they're shipped.",
        f"// {generated_by}",
    ]
    total = f"// {len(plan.rules)} rule(s)" + (", stack traces joined" if cfg.multiline else "")
    if plan.saved_bytes_per_month:
        total += f", saving about {format_bytes(plan.saved_bytes_per_month)}/month"
        if cost.priced:
            total += f" ({format_money(plan.saved_per_month, cost.currency)}/month)"
    out += [
        total + ".",
        "//",
        "// Send logs through it: forward_to = [loki.process.logsite.receiver] in your",
        "// source (e.g. loki.source.file), and point forward_to below where they went before.",
        'loki.process "logsite" {',
        f"  forward_to = [{cfg.forward_to}]",
    ]
    if cfg.multiline:
        out += [
            "",
            "  // One entry per record: a stack trace's lines (at …, Caused by: …, Traceback …)",
            "  // join the line that logged it, so Loki and logsite see the whole exception.",
            "  // Reading /var/log/pods files directly? Put stage.cri {} before this.",
            "  stage.multiline {",
            f"    firstline     = {_str(cfg.multiline_firstline or MULTILINE_FIRSTLINE)}",
            f"    max_lines     = {MULTILINE_MAX_LINES}",
            '    max_wait_time = "3s"',
            "  }",
        ]
    if match == "location" and plan.rules:
        stage = "stage.json" if format == "json" else "stage.logfmt"
        key = "expressions" if format == "json" else "mapping"
        out += [
            "",
            '  // The code line that wrote each record, as "path:line".',
            f"  {stage} {{",
            f"    {key} = {{",
            f"      logsite_file = {_str(file_field)},",
            f"      logsite_line = {_str(line_field)},",
            "    }",
            "  }",
            "  stage.template {",
            '    source   = "logsite_site"',
            '    template = "{{ .logsite_file }}:{{ .logsite_line }}"',
            "  }",
            "  stage.labels {",
            '    values = { logsite_site = "" }',
            "  }",
        ]
    for rule in plan.rules:
        if match == "location":
            # the guard too: a line from this call can still be an error at runtime
            sel = "{logsite_site=~" + _str(location_regex(rule.site)) + "} !~ " + _str(ERROR_GUARD)
        else:
            rx = template_regex(rule.site)
            if rx is None:
                out += [
                    "",
                    f"  // {rule.site.id}: skipped, its message has too little fixed text to match",
                    "  // safely; log file and line fields to match it by location instead",
                ]
                continue
            sel = f"{selector} |~ {_str(rx)} !~ {_str(ERROR_GUARD)}"
        out.append("")
        out += ["  " + c for c in _comment(rule, plan, cost)]
        if rule.action == "drop":
            out += [
                "  stage.match {",
                f"    selector            = {_str(sel)}",
                '    action              = "drop"',
                '    drop_counter_reason = "logsite"',
            ]
        else:
            out += [
                "  stage.match {",
                f"    selector = {_str(sel)}",
                "",
                "    stage.sampling {",
                f"      rate                = {rule.rate}",
                '      drop_counter_reason = "logsite"',
                "    }",
            ]
        out.append("  }")
    if match == "location" and plan.rules:
        out += [
            "",
            "  // Only for matching above: don't send it to Loki as a label.",
            "  stage.label_drop {",
            '    values = ["logsite_site"]',
            "  }",
        ]
    out.append("}")
    return "\n".join(out) + "\n"


def plan_json(plan: Plan) -> dict:
    return {
        "window_hours": plan.window_hours,
        "saved_bytes_per_month": round(plan.saved_bytes_per_month),
        "saved_per_month": round(plan.saved_per_month, 2),
        "rules": [
            {
                "id": r.site.id,
                "template": r.site.template,
                "level": r.site.level,
                "action": r.action,
                "keep": r.kept,
                "reason": r.reason,
                "count": r.usage.count,
                "bytes": r.usage.bytes,
                "saved_bytes_per_month": round(r.saved_bytes_per_month),
                "saved_per_month": round(r.saved_per_month, 2),
            }
            for r in plan.rules
        ],
        "skipped": [{"id": sid, "reason": why} for sid, why in plan.skipped],
    }
