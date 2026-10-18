"""Grafana dashboard: log cost and errors per code line, straight from Loki.

``logsite grafana`` writes a dashboard JSON that queries Loki itself (so it stays live), with
every log call's message template and a link to its source line baked in from the code.
Import it in Grafana (Dashboards → New → Import) or provision it from a file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from . import __version__
from .models import LogRecord, LogSite

MAX_MAPPINGS = 2000  # message templates baked into the table; more makes the JSON heavy
SECONDS_PER_MONTH = 30 * 24 * 3600
ERROR_LEVELS = "(?i)(error|err|critical|crit|fatal|exception|alert|emergency)"
PLAIN_ERROR = "\\b(ERROR|CRITICAL|FATAL)\\b"  # "2026-10-17 12:00:01 ERROR [app] ..."


@dataclass
class LokiShape:
    """How this service's records look in Loki."""

    selector: str  # e.g. {app="checkout"}
    parser: str = "json"  # json | logfmt
    file_field: str = "pathname"  # as Loki's parser names it
    line_field: str = "lineno"
    level_field: str = "level"
    prefix: str = ""  # deployed path prefix to strip, e.g. "/srv/"
    plain_text: bool = False  # records aren't JSON / logfmt: the level is a word in the line


def deployed_prefix(records: Iterable[LogRecord], sites: Iterable[LogSite]) -> str:
    """The prefix production puts before project paths (``/srv/`` in ``/srv/app/api.py``),
    the most common one among records whose file is a known project file."""
    paths = {s.path for s in sites}
    seen: Counter[str] = Counter()
    for r in records:
        src = (r.source_file or "").replace("\\", "/")
        for p in paths:
            if src == p or src.endswith("/" + p):
                seen[src[: len(src) - len(p)]] += 1
                break
    return seen.most_common(1)[0][0] if seen else ""


def level_field(records: Iterable[LogRecord]) -> str | None:
    """The field the records keep their level in, as Loki's json parser names it; None for
    plain-text records, whose level is a word in the line."""
    from .tracebacks import LEVEL_FIELDS

    found: Counter[str] = Counter()
    for r in records:
        for key in LEVEL_FIELDS:
            if key in r.fields:
                found[key] += 1
                break
    if not found:
        return None
    return found.most_common(1)[0][0].replace(
        ".", "_"
    )  # Loki's json parser flattens nested keys with "_"


def _q(s: str) -> str:
    """A LogQL string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Queries:
    def __init__(self, shape: LokiShape) -> None:
        self.s = shape
        f, line = shape.file_field, shape.line_field
        file_expr = f".{f}"
        if shape.prefix:
            file_expr = f"trimPrefix {_q(shape.prefix)} .{f}"
        self.located = (
            f"{shape.selector} | {shape.parser} | __error__=`` | {f}!=`` | {line}!=`` "
            f"| label_format logsite_file=`{{{{ {file_expr} }}}}`, "
            f"logsite_line=`{{{{ .{line} }}}}`, "
            f"logsite_site=`{{{{ {file_expr} }}}}:{{{{ .{line} }}}}`"
        )
        self.errors = (
            f"{shape.selector} |~ {_q(PLAIN_ERROR)}"
            if shape.plain_text
            else f"{shape.selector} | {shape.parser} | __error__=`` "
            f"| {shape.level_field}=~{_q(ERROR_LEVELS)}"
        )

    def by_site(self, fn: str, extra: str = "") -> str:
        return (
            "sum by (logsite_site, logsite_file, logsite_line) "
            f"({fn}({self.located}{extra} [$__range]))"
        )

    def monthly_cost(self) -> str:
        return (
            f"{self.by_site('bytes_over_time')} * {SECONDS_PER_MONTH} / $__range_s "
            f"/ 1000000000 * $price_per_gb"
        )


def _target(ref: str, expr: str, *, instant: bool = True, legend: str = "") -> dict:
    t = {
        "refId": ref,
        "datasource": {"type": "loki", "uid": "${datasource}"},
        "expr": expr,
        "queryType": "instant" if instant else "range",
    }
    if instant:
        t["instant"], t["range"] = True, False
    else:
        t["instant"], t["range"] = False, True
    if legend:
        t["legendFormat"] = legend
    return t


def _stat(pid: int, title: str, expr: str, unit: str, x: int, desc: str) -> dict:
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "description": desc,
        "gridPos": {"h": 4, "w": 6, "x": x, "y": 0},
        "datasource": {"type": "loki", "uid": "${datasource}"},
        "targets": [_target("A", expr)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                **({"decimals": 2} if unit.startswith(("currency", "prefix")) else {}),
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "none",
            "graphMode": "none",
        },
    }


def _site_mappings(sites: Iterable[LogSite]) -> list[dict]:
    options = {}
    for s in list(sites)[:MAX_MAPPINGS]:
        template = s.template.replace("\n", " ")
        if len(template) > 90:
            template = template[:89] + "…"
        options[s.id] = {"text": f"{s.id}  {s.level}({template!r})"}
    return [{"type": "value", "options": options}] if options else []


def _hidden(*names: str) -> list[dict]:
    return [
        {
            "matcher": {"id": "byName", "options": n},
            "properties": [{"id": "custom.hidden", "value": True}],
        }
        for n in names
    ]


def _renamed(name: str, title: str, unit: str | None = None) -> dict:
    props = [{"id": "displayName", "value": title}]
    if unit:
        props.append({"id": "unit", "value": unit})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def _site_link(source_url: str | None, ref: str) -> list[dict]:
    if not source_url:
        return []
    url = source_url.format(
        ref=ref, path="${__data.fields.logsite_file}", line="${__data.fields.logsite_line}"
    )
    return [{"title": "Open the log call", "url": url, "targetBlank": True}]


# The generic dashboard (grafana.com catalog): every project-specific value is a variable.
GENERIC_SHAPE = LokiShape(
    selector="${selector}",
    parser="${parser}",
    file_field="${file_field}",
    line_field="${line_field}",
    level_field="${level_field}",
    prefix="${path_prefix}",
)
PROJECT_URL = "https://github.com/rajpaynaik/logsite"
GENERIC_NOTE = f"""\
**Log cost per code line.** Every row is one log call (`file:line`), most expensive first, \
read live from Loki. Set the variables above to how your records look: the stream \
`selector`, the parser, and the fields that hold the file, line and level (Python logging: \
`pathname`, `lineno`, `levelname`). Records need file and line fields.

[logsite]({PROJECT_URL}) (open source) generates this dashboard for your own code: each \
row shows its log message and links to the line, plus Alloy rules that drop or sample the \
expensive lines, and checks in the editor and in CI."""


def _textbox(name: str, label: str, value: str, description: str) -> dict:
    return {
        "name": name,
        "label": label,
        "description": description,
        "type": "textbox",
        "query": value,
        "current": {"text": value, "value": value},
    }


def _generic_variables() -> list[dict]:
    return [
        _textbox("selector", "Stream selector", '{service_name=~".+"}', "LogQL stream selector"),
        {
            "name": "parser",
            "label": "Parser",
            "type": "custom",
            "query": "json,logfmt",
            "current": {"text": "json", "value": "json"},
            "options": [
                {"text": "json", "value": "json", "selected": True},
                {"text": "logfmt", "value": "logfmt", "selected": False},
            ],
        },
        _textbox("file_field", "File field", "pathname", "Field with the source file"),
        _textbox("line_field", "Line field", "lineno", "Field with the line number"),
        _textbox("level_field", "Level field", "level", "Field with the log level"),
        _textbox("path_prefix", "Strip prefix", "", "Deployed path prefix, e.g. /srv/"),
        _textbox(
            "repo_url",
            "Code URL",
            "",
            "Links rows to the code, e.g. https://github.com/org/repo/blob/main",
        ),
    ]


INFINITY = "yesoreyeram-infinity-datasource"  # Grafana's free JSON / HTTP data source plugin
ERROR_GROUP_COLUMNS = [  # (field in logsite's /errors, column title, type)
    ("trend_label", "Trend", "string"),
    ("count", "Count", "number"),
    ("title_short", "Error", "string"),
    ("history_label", "History", "string"),
    ("root_cause_short", "Root cause", "string"),
    ("raised_name", "Raised at", "string"),
    ("raised_code", "Code", "string"),
    ("logged_name", "Logged at", "string"),
    ("first_seen_ever", "First seen", "string"),
    ("fixed_before", "Fixed before by", "string"),
    ("example_trace", "Example trace", "string"),
    ("raised_url", "raised_url", "string"),
    ("logged_url", "logged_url", "string"),
]


def _code_link(column: str, url_field: str) -> dict:
    link = {"title": "Open in the code", "url": f"${{__data.fields.{url_field}}}"}
    return {
        "matcher": {"id": "byName", "options": column},
        "properties": [{"id": "links", "value": [{**link, "targetBlank": True}]}],
    }


def _width(column: str, width: int) -> dict:
    return {
        "matcher": {"id": "byName", "options": column},
        "properties": [{"id": "custom.width", "value": width}],
    }


def error_groups_panel(pid: int, y: int) -> dict:
    """Errors grouped by cause, from ``logsite alert-server``'s ``/errors`` through the
    Infinity data source; it follows the dashboard's time range."""
    target = {
        "refId": "A",
        "datasource": {"type": INFINITY, "uid": "${logsite}"},
        "type": "json",
        "source": "url",
        "format": "table",
        "parser": "backend",
        "url": "${logsite_url}/errors",
        "url_options": {
            "method": "GET",
            "data": "",
            "params": [{"key": "from", "value": "${__from}"}, {"key": "to", "value": "${__to}"}],
        },
        "root_selector": "groups",
        "columns": [
            {"selector": f, "text": title, "type": kind} for f, title, kind in ERROR_GROUP_COLUMNS
        ],
    }
    trend = {
        "matcher": {"id": "byName", "options": "Trend"},
        "properties": [
            {"id": "custom.width", "value": 70},
            {
                "id": "mappings",
                "value": [
                    {
                        "type": "value",
                        "options": {
                            "new": {"color": "red", "index": 0},
                            "back": {"color": "red", "index": 1},
                        },
                    }
                ],
            },
            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
        ],
    }
    return {
        "id": pid,
        "type": "table",
        "title": "Error groups",
        "description": "Production errors grouped by cause (the exception chain and the frames "
        "in your code), new and spiking first, for the dashboard's time range. From logsite "
        "alert-server's /errors.",
        "gridPos": {"h": 9, "w": 24, "x": 0, "y": y},
        "datasource": {"type": INFINITY, "uid": "${logsite}"},
        "targets": [target],
        # Infinity sorts columns by name; keep the order above.
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "indexByName": {title: i for i, (_, title, _) in enumerate(ERROR_GROUP_COLUMNS)}
                },
            }
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto"}},
            "overrides": [
                *_hidden("raised_url", "logged_url"),
                _code_link("Raised at", "raised_url"),
                _code_link("Logged at", "logged_url"),
                trend,
                _width("Count", 70),
                _width("Error", 280),
                _width("Root cause", 260),
                _width("Raised at", 170),
                _width("Code", 190),
                _width("Logged at", 170),
                _width("History", 170),
                _width("First seen", 95),
                _width("Fixed before by", 220),
                _width("Example trace", 100),
            ],
        },
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def history_annotation() -> dict:
    """First seen / gone / came back markers on the time graphs, from logsite alert-server's
    ``/history/events`` through the Infinity data source."""
    return {
        "name": "logsite history",
        "enable": True,
        "iconColor": "red",
        # the type only: Grafana 13 doesn't run annotations whose data source is a variable;
        # with a type it uses the (first) Infinity data source
        "datasource": {"type": INFINITY},
        "target": {
            "refId": "Anno",
            "type": "json",
            "source": "url",
            "format": "table",
            "parser": "backend",
            "url": "${logsite_url}/history/events",
            "url_options": {
                "method": "GET",
                "data": "",
                "params": [
                    {"key": "from", "value": "${__from}"},
                    {"key": "to", "value": "${__to}"},
                ],
            },
            "root_selector": "events",
            "columns": [
                {"selector": "time", "text": "time", "type": "timestamp_epoch"},
                {"selector": "title", "text": "title", "type": "string"},
                {"selector": "text", "text": "text", "type": "string"},
                {"selector": "kind", "text": "tags", "type": "string"},
            ],
        },
    }


BUILTIN_ANNOTATIONS = {
    "builtIn": 1,
    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
    "enable": True,
    "hide": True,
    "iconColor": "rgba(0, 211, 255, 1)",
    "name": "Annotations & Alerts",
    "type": "dashboard",
}


def build_dashboard(
    sites: Iterable[LogSite],
    shape: LokiShape,
    *,
    title: str,
    price_per_gb: float = 0.5,
    currency: str = "$",
    source_url: str | None = None,
    ref: str = "HEAD",
    uid: str | None = None,
    generic: bool = False,
    error_groups_url: str | None = None,
) -> dict:
    """The dashboard for ``sites``, or with ``generic`` one for any project, whose shape and
    code links are dashboard variables (``shape`` and ``source_url`` are then ignored)."""
    sites = list(sites)
    if generic:
        shape, sites = GENERIC_SHAPE, []
        source_url = "${{repo_url}}/{path}#L{line}"
    q = Queries(shape)
    unit = "currencyUSD" if currency == "$" else f"prefix:{currency}"
    link = _site_link(source_url, ref)
    site_column = {
        "matcher": {"id": "byName", "options": "logsite_site"},
        "properties": [
            {"id": "displayName", "value": "Log call"},
            {"id": "mappings", "value": _site_mappings(sites)},
            {"id": "custom.width", "value": 560},
            *([{"id": "links", "value": link}] if link else []),
        ],
    }
    sel = shape.selector

    panels = [
        _stat(
            1,
            "Log volume",
            f"sum(bytes_over_time({sel} [$__range]))",
            "decbytes",
            0,
            "Bytes stored in the selected time range.",
        ),  # fmt: skip
        _stat(
            2,
            "Log lines",
            f"sum(count_over_time({sel} [$__range]))",
            "short",
            6,
            "Lines stored in the selected time range.",
        ),  # fmt: skip
        _stat(
            3,
            "Estimated cost / month",
            f"sum(bytes_over_time({sel} [$__range])) * {SECONDS_PER_MONTH} / $__range_s "
            "/ 1000000000 * $price_per_gb",
            unit,
            12,
            "The selected range's volume scaled to 30 days, at the price per GB set above.",
        ),
        _stat(
            4,
            "Error lines",
            f"sum(count_over_time({q.errors} [$__range]))",
            "short",
            18,
            f"Lines whose {shape.level_field} is error, critical or fatal.",
        ),  # fmt: skip
        {
            "id": 5,
            "type": "table",
            "title": "Cost by code line",
            "description": "Every log call by what it writes, most expensive first. Click a "
            "row's log call to open it in the code. Generated by logsite from the code: "
            "regenerate after big changes so new log calls show their message.",
            "gridPos": {"h": 12, "w": 24, "x": 0, "y": 4},
            "datasource": {"type": "loki", "uid": "${datasource}"},
            "targets": [
                _target("cost", q.monthly_cost()),
                _target("bytes", q.by_site("bytes_over_time")),
                _target("lines", q.by_site("count_over_time")),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {"id": "sortBy", "options": {"sort": [{"field": "Value #cost", "desc": True}]}},
            ],
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto"}},
                "overrides": [
                    site_column,
                    *_hidden("logsite_file", "logsite_line", "Time"),
                    _renamed("Value #cost", "Est. / month", unit),
                    _renamed("Value #bytes", "Volume in range", "decbytes"),
                    _renamed("Value #lines", "Lines in range", "short"),
                ],
            },
            "options": {"showHeader": True, "footer": {"show": False}},
        },
        {
            "id": 6,
            "type": "table",
            "title": "Errors by code line",
            "description": "Error-level lines per log call now and in the same range just "
            "before, to spot what started failing. `logsite errors` groups them by cause.",
            "gridPos": {"h": 10, "w": 12, "x": 0, "y": 16},
            "datasource": {"type": "loki", "uid": "${datasource}"},
            "targets": [
                _target(
                    "now",
                    q.by_site("count_over_time", f" | {shape.level_field}=~{_q(ERROR_LEVELS)}"),
                ),
                _target(
                    "before",
                    q.by_site(
                        "count_over_time", f" | {shape.level_field}=~{_q(ERROR_LEVELS)}"
                    ).replace("[$__range]", "[$__range] offset $__range"),
                ),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {"id": "sortBy", "options": {"sort": [{"field": "Value #now", "desc": True}]}},
            ],
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    {
                        **site_column,
                        "properties": [
                            {"id": "custom.width", "value": 340} if p["id"] == "custom.width" else p
                            for p in site_column["properties"]
                        ],
                    },  # fmt: skip
                    *_hidden("logsite_file", "logsite_line", "Time"),
                    _renamed("Value #now", "Errors in range", "short"),
                    _renamed("Value #before", "Range before", "short"),
                ],
            },
            "options": {"showHeader": True},
        },
        {
            "id": 7,
            "type": "timeseries",
            "title": "Loudest log calls",
            "description": "Bytes per second written by the 10 loudest log calls.",
            "gridPos": {"h": 10, "w": 12, "x": 12, "y": 16},
            "datasource": {"type": "loki", "uid": "${datasource}"},
            "targets": [
                _target(
                    "A",
                    f"topk(10, sum by (logsite_site) (bytes_rate({q.located} [$__auto])))",
                    instant=False,
                    legend="{{logsite_site}}",
                )
            ],
            "fieldConfig": {"defaults": {"unit": "Bps"}, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom"}},
        },
        {
            "id": 8,
            "type": "logs",
            "title": "Recent errors",
            "gridPos": {"h": 10, "w": 24, "x": 0, "y": 26},
            "datasource": {"type": "loki", "uid": "${datasource}"},
            "targets": [_target("A", q.errors, instant=False)],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
        },
    ]

    price = {
        "name": "price_per_gb",
        "label": f"Price per GB ({currency})",
        "type": "textbox",
        "query": f"{price_per_gb:g}",
        "current": {"text": f"{price_per_gb:g}", "value": f"{price_per_gb:g}"},
    }
    variables = [
        {
            "name": "datasource",
            "label": "Loki",
            "type": "datasource",
            "query": "loki",
            "current": {},
        },
        *(_generic_variables() if generic else []),
        price,
    ]
    if error_groups_url:
        for panel in panels:
            if panel["gridPos"]["y"] >= 4:
                panel["gridPos"]["y"] += 9
        panels.insert(4, error_groups_panel(10, 4))
        variables += [
            {
                "name": "logsite",
                "label": "logsite (Infinity)",
                "type": "datasource",
                "query": INFINITY,
                "current": {},
            },
            _textbox(
                "logsite_url",
                "logsite URL",
                error_groups_url.rstrip("/"),
                "Where logsite alert-server runs, as Grafana's server reaches it",
            ),
        ]
    extra: dict = {}
    if error_groups_url:
        extra["annotations"] = {"list": [BUILTIN_ANNOTATIONS, history_annotation()]}
    if generic:
        for panel in panels:
            panel["gridPos"]["y"] += 5
        panels.insert(
            0,
            {
                "id": 9,
                "type": "text",
                "title": "",
                "gridPos": {"h": 5, "w": 24, "x": 0, "y": 0},
                "options": {"mode": "markdown", "content": GENERIC_NOTE},
            },
        )
        extra["__requires"] = [
            {"type": "grafana", "id": "grafana", "name": "Grafana", "version": "10.0.0"},
            {"type": "datasource", "id": "loki", "name": "Loki", "version": "1.0.0"},
            *(
                [{"type": "datasource", "id": INFINITY, "name": "Infinity", "version": "2.0.0"}]
                if error_groups_url
                else []
            ),
            *(
                {"type": "panel", "id": t, "name": t.title(), "version": ""}
                for t in ("stat", "table", "timeseries", "logs", "text")
            ),
        ]
        extra["description"] = (
            "Log cost and errors per code line (file:line) from Loki. "
            f"Made by logsite: {PROJECT_URL}"
        )
    return {
        **extra,
        "title": title,
        "uid": uid,
        "tags": ["logsite", "logs", "cost"],
        "timezone": "browser",
        "schemaVersion": 39,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "5m",
        "description": extra.get(
            "description", f"Generated by logsite {__version__}: log cost and errors per code line."
        ),
        "templating": {"list": variables},
        "panels": panels,
    }
