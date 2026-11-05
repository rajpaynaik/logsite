"""Language server: lint diagnostics, production counts as code lenses above log calls, and
a hover with the trend and the commits that recently touched the surrounding code."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from . import __version__
from .backends import TimeRange, from_config
from .config import Config, load_config, project_root
from .context import detect_in_source
from .cost import LineCost, format_bytes, format_money, monthly_costs
from .errors import AppFiles, ErrorGroup, summarize_errors
from .fixes import structured_rewrite
from .git import Commit, site_history
from .index import DEFAULT_EXCLUDES, index_calls, index_project, index_source, iter_python_files
from .latency import Timing, format_seconds
from .lint import describe_trend, is_spike, lint_source, lint_volume
from .models import Diagnostic

log = logging.getLogger(__name__)

SEVERITY = {
    "error": types.DiagnosticSeverity.Error,
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "hint": types.DiagnosticSeverity.Hint,
}
REFRESH_SECONDS = 300
HOVER_COMMITS = 3


def humanize(n: int) -> str:
    for unit, div in (("M", 1_000_000), ("k", 1_000)):
        if n >= div:
            return f"{n / div:.1f}{unit}".replace(".0", "")
    return str(n)


class Project:
    """One logsite project in the workspace: its config, production counts and caches.

    A workspace can hold several (a monorepo, or a parent folder opened in the editor); each
    file belongs to the nearest directory with its own config, see ``project_root``."""

    def __init__(self, ls: LogsiteServer, cfg: Config) -> None:
        self.ls = ls
        self.cfg = cfg
        self.counts: dict[str, int] = {}
        self.costs: dict[str, LineCost] = {}  # only when [cost] has a price
        self.previous_counts: dict[str, int] = {}
        self.counts_ready = False
        self._history: dict[str, list[Commit]] = {}  # site id -> commits; reset on refresh
        self._injected: dict[str, dict[str, list[str]]] = {}  # file -> injected context
        self.raised: dict[tuple[str, int], list[ErrorGroup]] = {}  # (path, line) -> errors
        self.logged: dict[str, list[ErrorGroup]] = {}  # site id -> errors it logged
        self.timings: dict[str, Timing] = {}  # site id -> its timings in the window

    def rel_path(self, fs: str) -> str:
        try:
            return Path(fs).resolve().relative_to(self.cfg.root).as_posix()
        except ValueError:
            return Path(fs).name

    def start(self) -> None:
        self.scan_injected()
        threading.Thread(target=self.refresh_counts_forever, daemon=True).start()

    def refresh_counts_forever(self) -> None:
        try:
            backend = from_config(self.cfg.backend)
        except (ValueError, RuntimeError) as exc:
            self.ls.window_show_message(
                types.ShowMessageParams(
                    type=types.MessageType.Error,
                    message=f"logsite: bad [backend] config in {self.cfg.root}: {exc}",
                )
            )
            return
        if backend is None:
            return
        while not self.ls._stop.is_set():
            try:
                sites = index_project(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes))
                current = TimeRange.last(self.cfg.window_hours)
                if self.cfg.cost.priced:
                    usage = backend.usage_by_line(sites, current)
                    self.counts = {sid: u.count for sid, u in usage.items()}
                    costs = monthly_costs(sites, usage, self.cfg.window_hours, self.cfg.cost)
                    self.costs = {c.site.id: c for c in costs}
                else:
                    self.counts = backend.count_by_line(sites, current)
                self.previous_counts = backend.count_by_line(sites, current.previous())
                self._history.clear()
                self.refresh_errors(backend, sites, current)
                self.refresh_timings(backend, sites, current)
                self.counts_ready = True
                self.ls.workspace_code_lens_refresh(None)
                for uri in list(self.ls.workspace.text_documents):
                    found = self.ls.project_for(uri)
                    if found and found[0] is self:
                        self.ls.publish(uri)
            except Exception as exc:  # keep the server alive if the backend is down
                log.warning("logsite: failed to fetch counts from %s: %s", backend.name, exc)
                self.ls.window_show_message(
                    types.ShowMessageParams(
                        type=types.MessageType.Warning,
                        message=f"logsite: couldn't reach {backend.name} backend: {exc}",
                    )
                )
            self.ls._stop.wait(REFRESH_SECONDS)

    def refresh_timings(self, backend, sites, current: TimeRange) -> None:
        """How long the operation each log call reports took (latency.py), every endpoint
        together: p95 shows next to its count."""
        try:
            found = backend.timings_by_line(sites, current)
        except Exception as exc:  # latency is extra: counts still show
            log.warning("logsite: failed to read timings from %s: %s", backend.name, exc)
            return
        by_site: dict[str, list[float]] = {}
        for (site, _endpoint), values in found.items():
            if site:
                by_site.setdefault(site, []).extend(values)
        self.timings = {sid: t for sid, xs in by_site.items() if (t := Timing.of(xs))}

    def refresh_errors(self, backend, sites, current: TimeRange) -> None:
        """Group production errors, so the line that raised one shows how often it did."""
        try:
            app = AppFiles.scan(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes))
            summary = summarize_errors(
                backend, sites, app, current, current.previous(), self.cfg.lint, self.cfg.leaks
            )
        except Exception as exc:  # errors are extra: counts still show
            log.warning("logsite: failed to group errors from %s: %s", backend.name, exc)
            return
        raised: dict[tuple[str, int], list[ErrorGroup]] = {}
        logged: dict[str, list[ErrorGroup]] = {}
        for g in summary.groups:
            if g.raised_at:
                file, _, line = g.raised_at.rpartition(":")
                raised.setdefault((file, int(line)), []).append(g)
            if g.logged_at:
                logged.setdefault(g.logged_at.id, []).append(g)
        self.raised, self.logged = raised, logged

    def scan_injected(self, path: str | None = None, source: str | None = None) -> None:
        """Refresh auto-injected context for one file (on save) or the whole project."""
        rules = self.cfg.lint.context
        if not (rules.active and rules.detect):
            return
        if path is None:
            self._injected = {}
            for file in iter_python_files(self.cfg.root, DEFAULT_EXCLUDES | set(self.cfg.excludes)):
                try:
                    text = file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = file.relative_to(self.cfg.root).as_posix()
                self._injected[rel] = detect_in_source(text, rel)
        else:
            self._injected[path] = detect_in_source(source or "", path)
        merged: dict[str, list[str]] = {}
        for found in self._injected.values():
            for f, where in found.items():
                merged.setdefault(f, []).extend(where)
        rules.detected = merged

    def history(self, path: str, line: int) -> list[Commit]:
        key = f"{path}:{line}"
        commits = self._history.get(key)  # the refresh thread may clear the cache meanwhile
        if commits is None:
            commits = site_history(self.cfg.root, path, line, max_count=HOVER_COMMITS)
            self._history[key] = commits
        return commits


class LogsiteServer(LanguageServer):
    def __init__(self) -> None:
        super().__init__("logsite", __version__)
        self.root: Path | None = None  # the workspace folder; set on initialized
        self.projects: dict[Path, Project | None] = {}  # None: its config is broken
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # --- helpers ---------------------------------------------------------------
    def project_for(self, uri: str) -> tuple[Project, str] | None:
        """The project a document belongs to, and its path relative to that project."""
        fs = to_fs_path(uri)
        if not fs or self.root is None:
            return None
        root = project_root(Path(fs), self.root)
        with self._lock:
            if root not in self.projects:
                try:
                    self.projects[root] = Project(self, load_config(root))
                except ValueError as exc:
                    self.projects[root] = None
                    self.window_show_message(
                        types.ShowMessageParams(
                            type=types.MessageType.Error,
                            message=f"logsite: bad config in {root}: {exc}",
                        )
                    )
                else:
                    self.projects[root].start()
            project = self.projects[root]
        return (project, project.rel_path(fs)) if project else None

    def to_lsp(self, d: Diagnostic) -> types.Diagnostic:
        return types.Diagnostic(
            range=types.Range(
                start=types.Position(line=d.line - 1, character=d.col),
                end=types.Position(line=d.end_line - 1, character=d.end_col),
            ),
            message=d.message,
            code=d.code,
            source="logsite",
            severity=SEVERITY.get(d.severity, types.DiagnosticSeverity.Warning),
        )

    def publish(self, uri: str) -> None:
        found = self.project_for(uri)
        if found is None:
            return
        project, path = found
        cfg = project.cfg
        source = self.workspace.get_text_document(uri).source
        diags = lint_source(source, path, cfg.lint)
        if project.counts_ready:
            window = f"{cfg.window_hours:g}h"
            diags += lint_volume(
                index_source(source, path),
                project.counts,
                cfg.lint,
                window,
                previous=project.previous_counts,
            )
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=uri, diagnostics=[self.to_lsp(d) for d in diags])
        )


server = LogsiteServer()


@server.feature(types.INITIALIZED)
def on_initialized(ls: LogsiteServer, params: types.InitializedParams) -> None:
    ls.root = Path(ls.workspace.root_path or Path.cwd()).resolve()


@server.feature(types.SHUTDOWN)
def on_shutdown(ls: LogsiteServer, params: None) -> None:
    ls._stop.set()


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LogsiteServer, params: types.DidOpenTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LogsiteServer, params: types.DidChangeTextDocumentParams) -> None:
    ls.publish(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LogsiteServer, params: types.DidSaveTextDocumentParams) -> None:
    uri = params.text_document.uri
    found = ls.project_for(uri)
    if found and found[1].endswith(".py"):  # context detection reads Python
        project, path = found
        before = project.cfg.lint.context.detected
        project.scan_injected(path, ls.workspace.get_text_document(uri).source)
        if project.cfg.lint.context.detected.keys() != before.keys():
            for open_uri in list(ls.workspace.text_documents):  # a new filter changes every file
                ls.publish(open_uri)
            return
    ls.publish(uri)


@server.feature(types.TEXT_DOCUMENT_CODE_LENS)
def code_lens(ls: LogsiteServer, params: types.CodeLensParams) -> list[types.CodeLens]:
    uri = params.text_document.uri
    found = ls.project_for(uri)
    if found is None or not found[0].counts_ready:
        return []
    project, path = found
    window = f"{project.cfg.window_hours:g}h"
    lenses = []
    for site in index_source(ls.workspace.get_text_document(uri).source, path):
        n = project.counts.get(site.id, 0)
        title = f"$(graph) {humanize(n)} in {window}" if n else f"no logs in {window}"
        before = project.previous_counts.get(site.id, 0)
        if is_spike(n, before, project.cfg.lint):
            title += f" $(arrow-up) {describe_trend(n, before)}"
        timing = project.timings.get(site.id)
        if timing is not None:
            title += f" · p95 {format_seconds(timing.p95)}"
        cost = project.costs.get(site.id)
        if cost is not None:
            title += f" · {format_money(cost.cost_per_month, project.cfg.cost.currency)}/mo"
        pos = types.Position(line=site.line - 1, character=site.col)
        lenses.append(
            types.CodeLens(
                range=types.Range(start=pos, end=pos),
                command=types.Command(title=title, command=""),
            )
        )
    lines = ls.workspace.get_text_document(uri).lines
    for (file, line), groups in project.raised.items():
        if file != path or line > len(lines):
            continue
        text = lines[line - 1]
        pos = types.Position(line=line - 1, character=len(text) - len(text.lstrip()))
        title = "$(error) " + ", ".join(_error_brief(g) for g in groups) + f" in {window}"
        lenses.append(
            types.CodeLens(
                range=types.Range(start=pos, end=pos),
                command=types.Command(title=title, command=""),
            )
        )
    return lenses


def _error_brief(g: ErrorGroup) -> str:
    """``TimeoutError 3k (new)``"""
    name = (g.exc_type or g.title).rsplit(".", 1)[-1][:60]
    trend = " (new)" if g.is_new else (f" ({g.trend})" if g.trend and g.trend != "1.0x" else "")
    return f"{name} {humanize(g.count)}{trend}"


def _error_lines(groups: list[ErrorGroup], window: str, logged: bool) -> list[str]:
    out = []
    for g in groups[:5]:
        trend = "new" if g.is_new else (g.trend or "")
        where = ""
        if logged and g.raised_at:
            where = f", raised at `{g.raised_at}`"
        elif not logged and g.logged_at:
            where = f", logged at `{g.logged_at.id}`"
        out.append(f"- `{g.title[:100]}`: {g.count:,} in {window} ({trend}){where} `[{g.id}]`")
    return out


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LogsiteServer, params: types.HoverParams) -> types.Hover | None:
    uri = params.text_document.uri
    found = ls.project_for(uri)
    if found is None:
        return None
    project, path = found
    line = params.position.line + 1
    site = next(
        (
            s
            for s in index_source(ls.workspace.get_text_document(uri).source, path)
            if s.line <= line <= s.end_line
        ),
        None,
    )
    raised = project.raised.get((path, line), [])
    if site is None and not raised:
        return None

    parts: list[str] = []
    window = f"{project.cfg.window_hours:g}h"
    if raised:
        parts.append("**logsite**: errors raised here in production:")
        parts.append("\n".join(_error_lines(raised, window, logged=False)))
        parts.append("Details: `logsite errors --traceback`")
    if site is None:
        return types.Hover(
            contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value="\n\n".join(parts))
        )
    if project.counts_ready:
        n, before = project.counts.get(site.id, 0), project.previous_counts.get(site.id, 0)
        parts.append(f"**logsite**: {n:,} in the last {window} (previous {window}: {before:,})")
        if is_spike(n, before, project.cfg.lint):
            parts[-1] += f", **spiked {describe_trend(n, before)}**"
        timing = project.timings.get(site.id)
        if timing is not None:
            parts.append(
                f"Took p50 {format_seconds(timing.p50)} · p95 {format_seconds(timing.p95)} · "
                f"p99 {format_seconds(timing.p99)} · max {format_seconds(timing.max)} "
                f"({timing.n:,} timed in {window}). What got slower: `logsite slow`"
            )
        cost = project.costs.get(site.id)
        if cost is not None:
            approx = "≈" if cost.estimated else ""
            money = format_money(cost.cost_per_month, project.cfg.cost.currency)
            parts.append(
                f"Cost: {approx}{money}/month "
                f"({format_bytes(cost.bytes_per_month)}/month, "
                f"{format_bytes(cost.avg_bytes)} per line)"
            )
    logged = project.logged.get(site.id, [])
    if logged:
        parts.append("Errors logged here:")
        parts.append("\n".join(_error_lines(logged, window, logged=True)))
    commits = project.history(site.path, site.line)
    if commits:
        parts.append("Recent changes to this code:")
        parts.append(
            "\n".join(
                f"- `{c.short_sha}` {c.date:%Y-%m-%d} {c.author}: {c.summary}" for c in commits
            )
        )
    if not parts:
        return None
    return types.Hover(
        contents=types.MarkupContent(kind=types.MarkupKind.Markdown, value="\n\n".join(parts)),
        range=types.Range(
            start=types.Position(line=site.line - 1, character=site.col),
            end=types.Position(line=site.end_line - 1, character=site.end_col),
        ),
    )


@server.feature(
    types.TEXT_DOCUMENT_CODE_ACTION,
    types.CodeActionOptions(code_action_kinds=[types.CodeActionKind.QuickFix]),
)
def code_action(ls: LogsiteServer, params: types.CodeActionParams) -> list[types.CodeAction]:
    """LL008 quick fix: move formatted values into extra=."""
    wanted = [d for d in params.context.diagnostics if d.code == "LL008"]
    uri = params.text_document.uri
    found = ls.project_for(uri) if wanted else None
    if found is None:
        return []
    _, path = found
    if not path.endswith(".py"):
        return []  # the rewrite is Python-only for now
    source = ls.workspace.get_text_document(uri).source
    calls = {site.line: (site, call) for site, call in index_calls(source, path)}
    actions = []
    for diag in wanted:
        found = calls.get(diag.range.start.line + 1)
        new_text = structured_rewrite(source, *found) if found else None
        if new_text is None:
            continue
        site, _ = found
        edit = types.TextEdit(
            range=types.Range(
                start=types.Position(line=site.line - 1, character=site.col),
                end=types.Position(line=site.end_line - 1, character=site.end_col),
            ),
            new_text=new_text,
        )
        actions.append(
            types.CodeAction(
                title="logsite: move values into extra= (constant message)",
                kind=types.CodeActionKind.QuickFix,
                diagnostics=[diag],
                is_preferred=True,
                edit=types.WorkspaceEdit(changes={uri: [edit]}),
            )
        )
    return actions


def serve() -> None:
    server.start_io()
