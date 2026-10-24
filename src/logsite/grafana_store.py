"""A Grafana dashboard on the error store: errors by any time range, with no log scanning.

``logsite grafana --store postgres`` (or ``mysql``, ``mssql``) writes a dashboard whose panels
query logsite's tables through Grafana's SQL data source: counts, errors over time, the top
causes with the line of code, by service and release, and the latest occurrences. Grafana's
time picker drives every panel (``$__timeFilter``), and the database answers from its
indexes, instead of Loki reading every line in the range.
"""

from __future__ import annotations

PLUGINS = {"postgres": "grafana-postgresql-datasource", "mysql": "mysql", "mssql": "mssql"}
ALL = "__all"


def _q(kind: str, column: str) -> str:
    """A column name that's a reserved word in this database."""
    if kind == "mysql":
        return f"`{column}`"
    if kind == "mssql":
        return f"[{column}]"
    return column


def _top(kind: str, sql: str, n: int) -> str:
    if kind == "mssql":
        return sql.replace("select ", f"select top {n} ", 1)
    return f"{sql} limit {n}"


def _filters(kind: str, alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    release = _q(kind, "release")
    return (f"$__timeFilter({a}at) and ('$service' = '{ALL}' or {a}service = '$service')"
            f" and ('$release' = '{ALL}' or {a}{release} = '$release')")  # fmt: skip


def _panel(pid: int, title: str, kind: str, sql: str, panel_type: str, grid: dict,
           fmt: str = "table", **extra) -> dict:  # fmt: skip
    return {
        "id": pid, "type": panel_type, "title": title, "gridPos": grid,
        "datasource": {"type": PLUGINS[kind], "uid": "${datasource}"},
        "targets": [{"refId": "A", "rawQuery": True, "editorMode": "code", "format": fmt,
                     "rawSql": sql, "datasource": {"type": PLUGINS[kind],
                                                   "uid": "${datasource}"}}],
        **extra,
    }  # fmt: skip


def _variable(name: str, label: str, kind: str, sql: str) -> dict:
    return {
        "name": name, "label": label, "type": "query", "refresh": 2, "includeAll": True,
        "allValue": ALL, "current": {"text": "All", "value": "$__all"},
        "datasource": {"type": PLUGINS[kind], "uid": "${datasource}"},
        "query": sql, "definition": sql, "sort": 1,
    }  # fmt: skip


def build_store_dashboard(kind: str, title: str | None = None, uid: str | None = None) -> dict:
    if kind not in PLUGINS:
        raise ValueError(f"--store takes {', '.join(PLUGINS)} (ClickHouse: query it directly)")
    f, fe = _filters(kind), _filters(kind, "e")
    release = _q(kind, "release")
    group_by_time = "$__timeGroupAlias(at, $__interval)"
    panels = [
        _panel(1, "Errors", kind, f"select count(*) as errors from error_events where {f}",
               "stat", {"h": 4, "w": 6, "x": 0, "y": 0},
               options={"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"}),
        _panel(2, "Causes", kind,
               f"select count(distinct group_id) as causes from error_events where {f}",
               "stat", {"h": 4, "w": 6, "x": 6, "y": 0},
               options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(3, "Services failing", kind,
               f"select count(distinct service) as services from error_events where {f}",
               "stat", {"h": 4, "w": 6, "x": 12, "y": 0},
               options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(4, "Releases with errors", kind,
               f"select count(distinct {release}) as releases from error_events where {f}",
               "stat", {"h": 4, "w": 6, "x": 18, "y": 0},
               options={"reduceOptions": {"calcs": ["lastNotNull"]}}),
        _panel(5, "Errors over time", kind,
               f"select {group_by_time}, count(*) as errors from error_events where {f}"
               " group by 1 order by 1" if kind != "mssql" else
               f"select {group_by_time}, count(*) as errors from error_events where {f}"
               " group by $__timeGroup(at, $__interval) order by 1",
               "timeseries", {"h": 8, "w": 24, "x": 0, "y": 4}, fmt="time_series",
               fieldConfig={"defaults": {"custom": {"drawStyle": "bars", "fillOpacity": 80}},
                            "overrides": []}),
        _panel(6, "Top causes", kind, _top(kind,
               "select g.title as error, g.raised_at as raised_at, g.root_cause as root_cause,"
               " count(*) as count, max(e.at) as last_seen from error_events e"
               f" join error_groups g on g.id = e.group_id where {fe}"
               " group by g.title, g.raised_at, g.root_cause order by count desc", 50),
               "table", {"h": 10, "w": 24, "x": 0, "y": 12}),
        _panel(7, "By service", kind,
               f"select service, count(*) as errors from error_events where {f}"
               " group by service order by errors desc", "barchart",
               {"h": 8, "w": 12, "x": 0, "y": 22},
               options={"orientation": "horizontal", "xField": "service"}),
        _panel(8, "By release", kind,
               f"select {release} as release_name, count(*) as errors from error_events"
               f" where {f} group by {release} order by errors desc", "barchart",
               {"h": 8, "w": 12, "x": 12, "y": 22},
               options={"orientation": "horizontal", "xField": "release_name"}),
        _panel(9, "Latest occurrences", kind, _top(kind,
               f"select e.at as time, e.service as service, e.{release} as release_name,"
               " g.title as error, e.message as message, e.trace_id as trace_id"
               f" from error_events e join error_groups g on g.id = e.group_id where {fe}"
               " order by e.at desc", 200),
               "table", {"h": 10, "w": 24, "x": 0, "y": 30}),
    ]  # fmt: skip
    return {
        "title": title or "Production errors (logsite error store)",
        "uid": uid or "logsite-error-store",
        "tags": ["logsite", "errors"],
        "timezone": "browser",
        "schemaVersion": 39,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "1m",
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "label": "Database",
                    "type": "datasource",
                    "query": PLUGINS[kind],
                    "current": {},
                },
                _variable(
                    "service",
                    "Service",
                    kind,
                    "select distinct service from error_events where service is not null",
                ),
                _variable(
                    "release",
                    "Release",
                    kind,
                    f"select distinct {release} from error_events where {release} is not null",
                ),
            ]
        },  # fmt: skip
        "panels": panels,
        "description": "logsite's error store: every production error, grouped by cause, "
        "queried by time range without scanning logs. Written by logsite grafana --store.",
    }
