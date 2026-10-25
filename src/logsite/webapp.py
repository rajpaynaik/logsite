"""``logsite app``: set logsite up and see production errors in a browser (or the desktop app).

Serves the interface in ``app_ui/`` and a small JSON API: test a log store connection, send a
test Slack message, check a database, save the settings, read the error store, write a
reproduction test, and run ``logsite watch`` in the background.

Settings go to ``logsite.toml`` in the project; secrets (passwords, tokens, the Slack URL) go
to a private file in the user's config directory (``secrets.json``, readable only by them),
and ``logsite.toml`` names them (``password_env = "LOGSITE_LOKI_PASSWORD"``), so the config file
can be committed.

On ``127.0.0.1`` the page carries a random token that every API call must send back, and the
``Host`` header must be the server's own: another website open in the browser can't call the
API (cross-site requests can't read the token; DNS rebinding fails the host check). Listening
on another address needs ``--token``, which the page asks for.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

UI = Path(__file__).parent / "app_ui"
MAX_BODY = 1024 * 1024
TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}

# The fields each log store's form asks for: (key, label, kind, required, placeholder)
BACKENDS: dict[str, dict] = {
    "loki": {"label": "Grafana Loki", "fields": [
        ("url", "Loki URL", "text", True, "http://localhost:3100"),
        ("selector", "Which logs (LogQL selector)", "text", True, '{service_name="checkout"}'),
        ("username", "Username", "text", False, ""),
        ("password", "Password or API token", "secret", False, ""),
        ("tenant_id", "Tenant (X-Scope-OrgID)", "text", False, ""),
    ]},
    "opensearch": {"label": "OpenSearch / Elasticsearch", "fields": [
        ("url", "URL", "text", True, "https://search.example.com:9200"),
        ("index", "Index pattern", "text", True, "logs-checkout-*"),
        ("username", "Username", "text", False, ""),
        ("password", "Password", "secret", False, ""),
        ("api_key", "API key", "secret", False, ""),
    ]},
    "cloudwatch": {"label": "AWS CloudWatch Logs", "fields": [
        ("log_group", "Log group", "text", True, "/ecs/checkout"),
        ("region", "Region", "text", False, "us-east-1"),
        ("profile", "AWS profile", "text", False, "default"),
    ]},
    "sentry": {"label": "Sentry", "fields": [
        ("org", "Organization slug", "text", True, "acme"),
        ("project", "Project slug", "text", True, "checkout"),
        ("token", "Auth token (event:read)", "secret", True, ""),
        ("url", "Sentry URL", "text", False, "https://sentry.io"),
    ]},
    "file": {"label": "A log file (or OpenTelemetry export)", "fields": [
        ("path", "Path to the log file", "text", True, "/var/log/app/app.log"),
    ]},
}  # fmt: skip


# --- secrets ----------------------------------------------------------------------------------


def secrets_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "logsite" / "secrets.json"


def load_secrets() -> dict[str, str]:
    try:
        data = json.loads(secrets_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_secrets(values: dict[str, str]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**load_secrets(), **values}
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # only this user reads it
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)
    apply_secrets()


def apply_secrets() -> None:
    """Put the saved secrets in the environment, where ``*_env`` config keys read them; a
    variable already set (by the shell, the container) wins."""
    for name, value in load_secrets().items():
        os.environ.setdefault(name, value)
        if os.environ.get(name) != value and name in _ours:
            os.environ[name] = value  # one we set earlier, now changed
        _ours.add(name)


_ours: set[str] = set()


def secret_name(section: str, key: str) -> str:
    return f"LOGSITE_{section}_{key}".upper().replace(".", "_")


# --- logsite.toml -----------------------------------------------------------------------------


def dump_toml(data: dict) -> str:
    """A small TOML writer for logsite's config: tables of strings, numbers, booleans, lists
    and nested tables."""
    out: list[str] = []

    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{_key(k)} = {value(x)}" for k, x in v.items()) + " }"
        return json.dumps(str(v), ensure_ascii=False)  # a valid TOML basic string

    def table(prefix: str, d: dict) -> None:
        plain = {k: v for k, v in d.items() if not isinstance(v, dict) or _inline(v)}
        nested = {k: v for k, v in d.items() if isinstance(v, dict) and not _inline(v)}
        if prefix:
            out.append(f"\n[{prefix}]")
        for k, v in plain.items():
            out.append(f"{_key(k)} = {value(v)}")
        for k, v in nested.items():
            table(f"{prefix}.{_key(k)}" if prefix else _key(k), v)

    table("", data)
    return "\n".join(out).lstrip("\n") + "\n"


def _inline(d: dict) -> bool:
    return (
        bool(d)
        and all(not isinstance(v, dict) for v in d.values())
        and len(d) <= 3
        and all(isinstance(v, (int, float)) for v in d.values())
    )  # e.g. sample = { "app/api.py:88" = 0.01 }


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


# --- the app ----------------------------------------------------------------------------------


class App:
    def __init__(self, root: Path, *, limit: int = 5000) -> None:
        apply_secrets()
        self.root = Path(root).resolve()
        self.limit = limit
        self.lock = threading.Lock()
        self.watch_thread: threading.Thread | None = None
        self.watch_stop = threading.Event()
        self.watch_state: dict = {"running": False, "last_check": None, "last": None,
                                  "error": None}  # fmt: skip

    def cfg(self):
        from .config import load_config

        apply_secrets()
        return load_config(self.root)

    def _config_file(self) -> Path:
        from .config import CONFIG_FILES

        return next((self.root / n for n in CONFIG_FILES if (self.root / n).is_file()),
                    self.root / "logsite.toml")  # fmt: skip

    def _raw(self) -> dict:
        from .config import tomllib

        path = self._config_file()
        try:
            return tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    # --- what the page shows -----------------------------------------------------------

    def status(self) -> dict:
        from .store import _safe, default_path

        try:
            cfg = self.cfg()
            error = None
        except Exception as exc:  # a broken logsite.toml: say so, let the forms fix it
            cfg, error = None, str(exc)
        backend = (cfg.backend if cfg else {}) or {}
        kind = backend.get("type")
        return {
            "project": str(self.root),
            "project_name": self.root.name,
            "config_file": str(self._config_file()),
            "config_error": error,
            "configured": bool(kind),
            "backend": {"type": kind, "label": BACKENDS.get(kind, {}).get("label", kind)}
            if kind else None,
            "slack": bool(cfg and cfg.alerts.slack_webhook),
            "store": _safe(cfg.store.url) if cfg and cfg.store.url
            else str(default_path(self.root)),
            "store_postgres": bool(cfg and cfg.store.url),
            "watch": {**self.watch_state, "every": cfg.watch.every if cfg else "5m"},
        }  # fmt: skip

    def settings(self) -> dict:
        """The forms' current values. Secrets show only whether one is saved."""
        raw = self._raw()
        backend = dict(raw.get("backend", {}))
        saved = load_secrets()
        values, have = {}, {}
        kind = backend.get("type", "loki")
        if kind == "elasticsearch":
            kind = "opensearch"
        for key, _label, field_kind, _req, _ph in BACKENDS.get(kind, {}).get("fields", []):
            if field_kind == "secret":
                env = backend.get(f"{key}_env")
                have[key] = bool(env and (os.environ.get(env) or saved.get(env)))
            elif key in backend:
                values[key] = backend[key]
        alerts = raw.get("alerts", {})
        slack_env = alerts.get("slack_webhook_env")
        store = raw.get("store", {})
        store_env = store.get("url_env")
        watch = raw.get("watch", {})
        return {
            "backends": {k: {"label": v["label"], "fields": [
                {"key": f[0], "label": f[1], "kind": f[2], "required": f[3], "placeholder": f[4]}
                for f in v["fields"]]} for k, v in BACKENDS.items()},
            "backend": {"type": kind if backend.get("type") else None, "values": values,
                        "secrets": have},
            "slack": {"saved": bool(alerts.get("slack_webhook") or (slack_env and (
                os.environ.get(slack_env) or saved.get(slack_env))))},
            "store": {"postgres": bool(store.get("url") or store_env),
                      "retention_days": store.get("retention_days", 90)},
            "watch": {"every": watch.get("every", "5m"),
                      "alert_on": watch.get("alert_on", ["new", "regression", "spike"])},
            "service": raw.get("timeline", {}).get("service", ""),
        }  # fmt: skip

    # --- testing a connection before saving it ----------------------------------------------

    def _backend_settings(self, form: dict) -> dict:
        """A ``[backend]`` table from the form; a secret left empty keeps the saved one."""
        kind = form.get("type")
        if kind not in BACKENDS:
            raise ValueError("pick where your logs are")
        current = dict(self._raw().get("backend", {}))
        out: dict = {"type": kind}
        values = form.get("values") or {}
        for key, label, field_kind, required, _ph in BACKENDS[kind]["fields"]:
            value = str(values.get(key) or "").strip()
            if field_kind == "secret" and not value and current.get("type") == kind:
                env = current.get(f"{key}_env")
                value = (os.environ.get(env) or "") if env else ""
            if required and not value:
                raise ValueError(f"{label} is needed")
            if value:
                out[key] = value
        if kind == "file":
            path = Path(out["path"]).expanduser()
            out["path"] = str(path if path.is_absolute() else (self.root / path))
        return out

    def test_backend(self, form: dict) -> dict:
        from .backends import TimeRange, from_config

        try:
            backend = from_config(self._backend_settings(form))
            if kind_is_file(form) and not Path(backend.path).is_file():
                return {"ok": False, "error": f"no file at {backend.path}"}
            window = TimeRange.last(24)
            if kind_is_file(form):  # a file holds what it holds: read all of it
                window = TimeRange(datetime(1970, 1, 1, tzinfo=timezone.utc), window.end)
            lines = backend.query_logs(window, limit=5)
            errors = backend.query_errors(window, limit=500)
        except Exception as exc:
            return {"ok": False, "error": _short(exc)}
        return {
            "ok": True,
            "lines": [{"time": r.timestamp.isoformat() if r.timestamp else None,
                       "message": r.message.split("\n", 1)[0][:200]} for r in lines],
            "errors": len(errors),
            "errors_capped": len(errors) >= 500,
            "scope": "the file" if kind_is_file(form) else "the last 24 hours",
        }  # fmt: skip

    def test_slack(self, form: dict) -> dict:
        from .alerts import post_json

        url = (form.get("webhook") or "").strip() or (self.cfg().alerts.slack_webhook or "")
        if not url.startswith("https://hooks.slack.com/"):
            return {"ok": False, "error": "paste the webhook URL Slack gave you "
                                          "(https://hooks.slack.com/services/...)"}  # fmt: skip
        try:
            post_json(url, {"text": f":white_check_mark: logsite is connected: errors from "
                                    f"*{self.root.name}* will be posted here."})  # fmt: skip
        except Exception as exc:
            return {"ok": False, "error": _short(exc)}
        return {"ok": True}

    def test_store(self, form: dict) -> dict:
        from .store import connect, default_path

        url = (form.get("url") or "").strip()
        try:
            store = connect(url or default_path(self.root))
            store.close()
        except Exception as exc:
            return {"ok": False, "error": _short(exc)}
        return {"ok": True}

    # --- saving -------------------------------------------------------------------------

    def save(self, form: dict) -> dict:
        """Merge the forms into ``logsite.toml``; secrets into the secrets file."""
        with self.lock:
            raw = self._raw()
            new_secrets: dict[str, str] = {}
            if "backend" in form:
                b = self._backend_settings(form["backend"])
                kind = b["type"]
                table: dict = {"type": kind}
                for key, _label, field_kind, _req, _ph in BACKENDS[kind]["fields"]:
                    if key not in b:
                        continue
                    if field_kind == "secret":
                        name = secret_name(kind, key)
                        new_secrets[name] = b[key]
                        table[f"{key}_env"] = name
                    elif kind == "file" and key == "path":
                        path = Path(b[key])
                        table[key] = (str(path.relative_to(self.root))
                                      if path.is_relative_to(self.root) else str(path))  # fmt: skip
                    else:
                        table[key] = b[key]
                raw["backend"] = table
            if form.get("slack", {}).get("webhook"):
                name = "LOGSITE_SLACK_WEBHOOK"
                new_secrets[name] = form["slack"]["webhook"].strip()
                alerts = raw.setdefault("alerts", {})
                alerts.pop("slack_webhook", None)
                alerts["slack_webhook_env"] = name
            if "store" in form:
                store = raw.setdefault("store", {})
                url = (form["store"].get("url") or "").strip()
                if form["store"].get("postgres") and url:
                    new_secrets["LOGSITE_DATABASE_URL"] = url
                    store.pop("url", None)
                    store["url_env"] = "LOGSITE_DATABASE_URL"
                elif not form["store"].get("postgres"):
                    store.pop("url", None)
                    store.pop("url_env", None)
                if form["store"].get("retention_days"):
                    store["retention_days"] = float(form["store"]["retention_days"])
                if not store:
                    raw.pop("store", None)
            if "watch" in form:
                watch = raw.setdefault("watch", {})
                if form["watch"].get("every"):
                    watch["every"] = str(form["watch"]["every"])
                if form["watch"].get("alert_on") is not None:
                    watch["alert_on"] = list(form["watch"]["alert_on"])
            if form.get("service") is not None:
                timeline = raw.setdefault("timeline", {})
                if form["service"]:
                    timeline["service"] = str(form["service"])
                else:
                    timeline.pop("service", None)
                if not timeline:
                    raw.pop("timeline", None)
            if new_secrets:
                save_secrets(new_secrets)
            text = dump_toml(raw)
            from .config import tomllib

            tomllib.loads(text)  # never write a file logsite can't read back
            self._config_file().write_text(text, encoding="utf-8")
        return self.status()

    # --- errors ---------------------------------------------------------------------------

    def _store(self):
        from .store import open_store

        return open_store(self.cfg())

    def errors(self, query: dict) -> dict:
        """The groups for the page's filters: ``from`` / ``to`` (ISO 8601) or ``hours``,
        ``service``, ``release``, ``q`` (text in the error, its cause or its file)."""
        since, until = _window(query)
        store = self._store()
        try:
            groups = store.groups(since=since, until=until, limit=200,
                                  service=query.get("service") or None,
                                  release=query.get("release") or None,
                                  search=(query.get("q") or "").strip() or None)  # fmt: skip
            facets = store.facets(since, until)
        finally:
            store.close()
        return {"from": since.isoformat(), "to": until.isoformat(), "groups": groups,
                "total": sum(g["recent"] for g in groups), "facets": facets}  # fmt: skip

    def error(self, group_id: str, query: dict | None = None) -> dict | None:
        from .cli import _code_links

        query = query or {}
        store = self._store()
        try:
            group = store.group(group_id)
            if group is None:
                return None
            now = datetime.now(timezone.utc)
            since, until = _window(query) if query.get("from") or query.get("hours") else (
                None, None)  # fmt: skip
            events = store.events(group_id, limit=20, since=since, until=until,
                                  service=query.get("service") or None,
                                  release=query.get("release") or None)  # fmt: skip
            hourly = store.hourly(group_id, now - timedelta(days=7))
            alerts = store._query(
                "select kind, sent_at, channels from alerts where group_id = ?"
                " order by sent_at desc limit 10", (group_id,))  # fmt: skip
        finally:
            store.close()
        code_line, code_url = _code_links(self.cfg())
        for key in ("raised_at", "logged_at"):
            where = group.get(key)
            group[key.replace("_at", "_code")] = code_line(where) if where else None
            group[key.replace("_at", "_url")] = code_url(where) if where else None
        return {"group": group, "events": events, "hourly": hourly, "alerts": alerts}

    def repro(self, group_id: str, write: bool = False) -> dict:
        from .backends import from_config
        from .repro import case_json, find_case, skeleton, skeleton_path

        cfg = self.cfg()
        store = self._store()
        try:
            group = store.group(group_id)
        finally:
            store.close()
        if group is None:
            return {"ok": False, "error": "no such error"}
        last = datetime.fromisoformat(str(group["last_seen"]).replace(" ", "T"))
        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600 + 1
        try:
            case, _ = find_case(cfg, group_id, backend=from_config(cfg.backend),
                                hours=max(hours, 1), limit=self.limit)  # fmt: skip
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            test = skeleton(case)
        except ValueError as exc:
            test, note = None, str(exc)
        else:
            note = None
        path = skeleton_path(cfg.root, case)
        out = {"ok": True, "case": case_json(case), "test": test, "note": note,
               "path": str(path.relative_to(cfg.root)), "exists": path.exists()}  # fmt: skip
        if write and test:
            if path.exists():
                return {**out, "ok": False, "error": f"{out['path']} exists already"}
            path.write_text(test, encoding="utf-8")
            out.update(written=True, exists=True)
        return out

    # --- watching in the background -------------------------------------------------------

    def start_watch(self) -> dict:
        with self.lock:
            if self.watch_thread and self.watch_thread.is_alive():
                return self.status()
            self.watch_stop.clear()
            self.watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
            self.watch_state["running"] = True
            self.watch_thread.start()
        return self.status()

    def stop_watch(self) -> dict:
        self.watch_stop.set()
        self.watch_state["running"] = False
        return self.status()

    def _watch_loop(self) -> None:
        from .alerts import post_json
        from .backends import from_config
        from .cli import _code_links, _duration
        from .errors import AppFiles
        from .index import DEFAULT_EXCLUDES, index_project
        from .store import open_store
        from .watch import check

        indexed_at, sites, app = 0.0, [], None
        while not self.watch_stop.is_set():
            started = time.monotonic()
            now = datetime.now(timezone.utc)
            every = 300.0
            try:
                cfg = self.cfg()
                every = _duration(cfg.watch.every).total_seconds()
                if started - indexed_at > 300:
                    excludes = DEFAULT_EXCLUDES | set(cfg.excludes)
                    sites, app = (
                        index_project(cfg.root, excludes),
                        AppFiles.scan(cfg.root, excludes),
                    )
                    indexed_at = started
                code_line, code_url = _code_links(cfg)
                store = open_store(cfg)
                try:
                    result = check(cfg, from_config(cfg.backend), sites, app, store, now,
                                   send=post_json, code_line=code_line, code_url=code_url,
                                   limit=self.limit)  # fmt: skip
                finally:
                    store.close()
                self.watch_state.update(error=None, last={
                    "events": result.events, "stored": result.stored,
                    "groups": len(result.groups), "alerts": len(result.alerts),
                    "sent": result.sent, "exported": result.exported,
                    "export_error": result.export_error})  # fmt: skip
            except Exception as exc:  # the log store down: show it, keep watching
                self.watch_state["error"] = _short(exc)
            self.watch_state["last_check"] = now.isoformat()
            self.watch_stop.wait(max(5.0, every - (time.monotonic() - started)))


def _window(query: dict) -> tuple[datetime, datetime]:
    """The time range a request asks for: ``from`` / ``to``, else the last ``hours`` (24)."""
    from .models import parse_iso

    now = datetime.now(timezone.utc)

    def at(name):
        value = (query.get(name) or "").strip()
        if not value:
            return None
        t = parse_iso(value)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    until = at("to") or now
    since = at("from") or until - timedelta(hours=float(query.get("hours") or 24))
    if since >= until:
        raise ValueError("the start of the range must be before its end")
    return since, until


def kind_is_file(form: dict) -> bool:
    return form.get("type") == "file"


def _short(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    text = re.sub(r"https?://[^\s@]+@", "https://…@", text)  # no passwords in URLs
    return text[:300]


# --- HTTP -------------------------------------------------------------------------------------


def make_server(app: App, host: str, port: int, token: str | None) -> ThreadingHTTPServer:
    local = host in ("127.0.0.1", "localhost", "::1")
    session = token or secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        server_version = "logsite"

        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, default=str).encode())

        def _host_ok(self) -> bool:
            if not local:
                return True
            port_now = self.server.server_address[1]
            allowed = {f"127.0.0.1:{port_now}", f"localhost:{port_now}", f"[::1]:{port_now}"}
            return self.headers.get("Host", "") in allowed

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Logsite-Token", ""), session)

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"wrong host", "text/plain")
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            if not self._authorized():
                return self._json(401, {"error": "missing or wrong token"})
            q = parse_qs(url.query)
            try:
                if url.path == "/api/status":
                    return self._json(200, app.status())
                if url.path == "/api/settings":
                    return self._json(200, app.settings())
                one = {k: v[0] for k, v in q.items()}
                if url.path == "/api/errors":
                    return self._json(200, app.errors(one))
                m = re.fullmatch(r"/api/errors/([0-9a-f]{6,40})", url.path)
                if m:
                    found = app.error(m[1], one)
                    return self._json(200 if found else 404, found or {"error": "not found"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                return self._json(500, {"error": _short(exc)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"wrong host", "text/plain")
            if not self._authorized():
                return self._json(401, {"error": "missing or wrong token"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "not JSON"})
            path = urlparse(self.path).path
            routes = {
                "/api/test/backend": app.test_backend,
                "/api/test/slack": app.test_slack,
                "/api/test/store": app.test_store,
                "/api/settings": app.save,
                "/api/watch/start": lambda _b: app.start_watch(),
                "/api/watch/stop": lambda _b: app.stop_watch(),
            }
            try:
                if path in routes:
                    return self._json(200, routes[path](body))
                m = re.fullmatch(r"/api/errors/([0-9a-f]{6,40})/repro", path)
                if m:
                    return self._json(200, app.repro(m[1], write=bool(body.get("write"))))
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                return self._json(500, {"ok": False, "error": _short(exc)})
            return self._json(404, {"error": "not found"})

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
            file = (UI / name).resolve()
            if not file.is_relative_to(UI.resolve()) or not file.is_file():
                return self._send(404, b"not found", "text/plain")
            body = file.read_bytes()
            if name == "index.html":
                # on localhost the page carries the session token; elsewhere it asks for it
                meta = f'<meta name="logsite-token" content="{session if local else ""}">'
                body = body.replace(b"<!--token-->", meta.encode())
            self._send(200, body, TYPES.get(file.suffix, "application/octet-stream"))

    return ThreadingHTTPServer((host, port), Handler)
