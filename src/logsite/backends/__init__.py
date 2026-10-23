from .base import LogBackend, TimeRange, parse_line
from .cloudwatch import CloudWatchBackend
from .file import FileBackend
from .loki import LokiBackend
from .opensearch import OpenSearchBackend
from .sentry import SentryBackend

__all__ = [
    "CloudWatchBackend",
    "FileBackend",
    "LogBackend",
    "LokiBackend",
    "OpenSearchBackend",
    "SentryBackend",
    "TimeRange",
    "parse_line",
    "from_config",
]


def from_config(cfg: dict) -> LogBackend | None:
    """Build a backend from the ``[backend]`` table of the logsite config.

    Raises ValueError for a bad config and RuntimeError if the backend's extra isn't installed.
    """
    kind = cfg.get("type")
    if not kind:
        return None
    try:
        return _build(kind, cfg)
    except KeyError as exc:
        raise ValueError(f"backend type {kind!r} needs {exc.args[0]!r}") from None


def _build(kind: str, cfg: dict) -> LogBackend:
    if kind == "file":
        return FileBackend(cfg["path"])
    if kind == "loki":
        return LokiBackend(
            cfg["url"],
            cfg["selector"],
            username=cfg.get("username"),
            password=cfg.get("password"),
            tenant_id=cfg.get("tenant_id"),
            aggregate=bool(cfg.get("aggregate", False)),
            parser=cfg.get("parser", "json"),
            file_label=cfg.get("file_label", "pathname"),
            line_label=cfg.get("line_label", "lineno"),
        )
    if kind in ("opensearch", "elasticsearch"):
        return OpenSearchBackend(
            cfg["url"],
            cfg["index"],
            query=cfg.get("query"),
            timestamp_field=cfg.get("timestamp_field", "@timestamp"),
            message_field=cfg.get("message_field", "message"),
            username=cfg.get("username"),
            password=cfg.get("password"),
            api_key=cfg.get("api_key"),
            aggregate=bool(cfg.get("aggregate", False)),
            file_field=cfg.get("file_field", "log.origin.file.name"),
            line_field=cfg.get("line_field", "log.origin.file.line"),
        )
    if kind == "cloudwatch":
        return CloudWatchBackend(
            cfg["log_group"],
            region=cfg.get("region"),
            profile=cfg.get("profile"),
            filter_pattern=cfg.get("filter_pattern"),
            log_stream_prefix=cfg.get("log_stream_prefix"),
        )
    if kind == "sentry":
        return SentryBackend(
            cfg["org"],
            cfg["project"],
            cfg.get("token"),
            url=cfg.get("url", "https://sentry.io"),
        )
    raise ValueError(
        f"unknown backend type: {kind!r} (supported: file, loki, opensearch, elasticsearch, "
        "cloudwatch, sentry)"
    )
