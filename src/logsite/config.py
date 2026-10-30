"""Load configuration from ``logsite.toml`` or ``[tool.logsite]`` in ``pyproject.toml``.

The project was called loglens before; ``loglens.toml`` and ``[tool.loglens]`` still work.

Example ``logsite.toml``::

    [backend]
    type = "loki"
    url = "http://localhost:3100"
    selector = '{app="checkout"}'
    password_env = "LOKI_PASSWORD"   # read secrets from the environment

    [lint]
    ignore = ["LL004"]
    required_fields = ["trace_id"]
    noisy_threshold = 50000

    [context]                        # LL006: required fields by situation
    all = ["request_id"]
    error = ["tenant_id"]
    in_retry = ["attempt"]
    near_dependency = ["dependency"]
    injected = ["trace_id"]          # added outside the code (e.g. by a log shipper)

    [timeline]                       # logsite trace <id>
    field = "trace_id"
    service = "checkout"             # this codebase's name in the timeline
    [timeline.backend]               # optional: a wider scope than [backend], so other
    type = "loki"                    # services' lines show up too
    url = "http://localhost:3100"
    selector = '{env="prod"}'

    [leaks]                          # logsite leaks
    ignore = ["email"]               # detector kinds to skip
    allow = ['@example\\.com$']       # regexes for values that are fine (test data)

    [pipeline]                       # logsite pipeline: Alloy drop / sample rules
    drop = ["app/health.py:12"]
    sample = { "app/api.py:88" = 0.01 }   # keep 1 in 100
    max_per_window = 100000          # sample any line above this down to it
    drop_levels = ["debug"]

    [alerts]                         # logsite alert-server: summaries with Grafana alerts
    slack_webhook_env = "SLACK_WEBHOOK_URL"
    token_env = "LOGSITE_ALERT_TOKEN"
    lookback_minutes = 30
    selector_labels = ["service_name"]

    [watch]                          # logsite watch: errors to Slack, as they happen
    every = "5m"
    alert_on = ["new", "regression", "spike", "release"]

    [store]                          # where logsite watch keeps every error
    url_env = "LOGSITE_DATABASE_URL"   # postgresql://...; default: a SQLite file
    retention_days = 90

    [release]                        # logsite release: a release's effect on errors
    window_hours = 24                # how long after a release to look, at most
    bad_rate = 3.0                   # errors/hour this many times the hours before: bad
    bad_count = 10                   # a new or regressed error this many times: bad

    [latency]                        # logsite slow: timings from the logs
    window_hours = 24                # this long, against as long before
    ratio = 2.0                      # slower: p95 this many times the window before
    min_calls = 100                  # ... over at least this many timed calls

    [cost]                           # logsite cost, $/mo in the editor
    price_per_gb = 0.50              # what your log store charges per GB ingested
    price_per_million_events = 0     # per-event indexing charges, if any

    window_hours = 24
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from .alerts import AlertsConfig
from .cost import CostConfig
from .export import ExportConfig
from .history import HistoryConfig
from .latency import LatencyConfig
from .leaks import LeakConfig
from .lint import LintConfig
from .pipeline import PipelineConfig
from .releases import ReleaseConfig
from .store import StoreConfig
from .watch import WatchConfig


@dataclass
class TimelineConfig:
    field_name: str = "trace_id"  # [timeline] field
    service: str | None = None  # defaults to the project directory's name
    backend: dict = field(default_factory=dict)  # empty: use [backend]


@dataclass
class Config:
    root: Path
    backend: dict = field(default_factory=dict)
    lint: LintConfig = field(default_factory=LintConfig)
    leaks: LeakConfig = field(default_factory=LeakConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    store: StoreConfig = field(default_factory=StoreConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    latency: LatencyConfig = field(default_factory=LatencyConfig)
    window_hours: float = 24.0
    excludes: list[str] = field(default_factory=list)


CONFIG_FILES = ("logsite.toml", "loglens.toml")  # loglens: the project's previous name


def find_root(start: Path) -> Path:
    start = start.resolve()
    for d in [start, *start.parents]:
        if (
            any((d / name).is_file() for name in CONFIG_FILES)
            or (d / "pyproject.toml").is_file()
            or (d / ".git").exists()
        ):
            return d
    return start


def has_config(d: Path) -> bool:
    """True if ``d`` holds a logsite config file or a ``[tool.logsite]`` table."""
    if any((d / name).is_file() for name in CONFIG_FILES):
        return True
    pyproject = d / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return "logsite" in tool or "loglens" in tool


def project_root(file: Path, workspace: Path) -> Path:
    """The project a file belongs to in the editor: the nearest directory between the file
    and the workspace root that has its own config (so a monorepo or a parent folder works),
    else the workspace root. Files outside the workspace use :func:`find_root`."""
    file, workspace = file.resolve(), workspace.resolve()
    if not file.is_relative_to(workspace):
        return find_root(file.parent)
    for d in file.parents:
        if d == workspace or has_config(d):
            return d
        if not d.is_relative_to(workspace):
            break
    return workspace


def _resolve_backend(backend: dict, root: Path) -> dict:
    """Read ``*_env`` keys from the environment and make file paths absolute."""
    for key in list(backend):
        if key.endswith("_env"):
            backend.setdefault(key[: -len("_env")], os.environ.get(backend[key]))
    if backend.get("type") == "file" and "path" in backend:
        backend["path"] = str((root / backend["path"]).resolve())
    return backend


def load_config(start: str | Path | None = None) -> Config:
    root = find_root(Path(start or Path.cwd()))
    data: dict = {}
    config_file = next((root / n for n in CONFIG_FILES if (root / n).is_file()), None)
    if config_file is not None:
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    elif (root / "pyproject.toml").is_file():
        tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {})
        data = tool.get("logsite", tool.get("loglens", {}))

    backend = _resolve_backend(dict(data.get("backend", {})), root)

    timeline = dict(data.get("timeline", {}))
    timeline_backend = _resolve_backend(dict(timeline.get("backend", {})), root)

    return Config(
        root=root,
        backend=backend,
        lint=LintConfig.from_dict(data.get("lint", {}), context=data.get("context", {})),
        leaks=LeakConfig.from_dict(data.get("leaks", {})),
        cost=CostConfig.from_dict(data.get("cost", {})),
        timeline=TimelineConfig(
            field_name=str(timeline.get("field", "trace_id")),
            service=timeline.get("service") or root.name,
            backend=timeline_backend,
        ),
        pipeline=PipelineConfig.from_dict(data.get("pipeline", {})),
        alerts=AlertsConfig.from_dict(data.get("alerts", {})),
        history=HistoryConfig.from_dict(data.get("history", {})),
        store=StoreConfig.from_dict(data.get("store", {})),
        watch=WatchConfig.from_dict(data.get("watch", {})),
        export=ExportConfig.from_dict(data.get("export", {})),
        release=ReleaseConfig.from_dict(data.get("release", {})),
        latency=LatencyConfig.from_dict(data.get("latency", {})),
        window_hours=float(data.get("window_hours", 24)),
        excludes=list(data.get("exclude", [])),
    )
