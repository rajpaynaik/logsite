"""MCP server: lets AI assistants query the log index, lint results, production counts,
spikes and git history, e.g. "which log lines spiked after the last deploy?"

Run it with ``logsite mcp`` (stdio). Every tool is read-only.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .backends import LogBackend, TimeRange, from_config
from .config import Config
from .context import detect_injected, measure_coverage, overall_coverage, satisfied
from .cost import monthly_costs
from .deploys import compare_deploy, resolve_deploy
from .errors import AppFiles, error_window, summarize_errors, summary_json
from .git import Commit, parse_target, site_history
from .index import DEFAULT_EXCLUDES, index_project
from .leaks import find_leaks
from .lint import RULES, describe_trend, find_spikes, lint_paths, lint_volume
from .matching import sample_records
from .models import LogSite, parse_iso
from .structure import production_score, score_project
from .timeline import find_timeline, timeline_json

INSTRUCTIONS = """\
logsite maps production logs back to the code (Python, Java, Go) that emitted them.
Log calls are identified as "PATH:LINE" (PATH relative to the project root).
Before changing code, call `production_context` with the file (and line or range): the
errors raised, logged or passing through there (and whether one came back after a fix, and
what fixed it), what its log calls cost, the last commits and logging problems. Don't undo
an earlier fix it reports, and avoid adding log calls to lines it shows as very busy.
For "what's failing in production?", start with `investigate`: errors linked into incidents,
each with its cause, effects, the deploy before it and suspect commits; `error_summary` lists
the groups themselves.
To follow one failed request across services, use `request_timeline` with its trace id.
After a release, start with `compare_deploy` (a git tag or sha): it reports lines that
spiked or started erroring since the deploy, with the release's commits that touched them.
Otherwise start with `spikes` to find lines whose volume jumped, then `log_history` on one
of them to see which commits changed the surrounding code. `lint` finds log-quality problems
(rules: `list_rules`). `log_costs` ranks log calls by monthly spend.
`context_coverage` shows which required context fields production records actually carry.
`structure_score` shows how much logging keeps a constant message with values in fields.
`find_leaks` finds secrets and personal data that actually reached production logs; sample
values are masked, so don't try to reconstruct them.
Tools that need production data fail if no backend is configured.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True)


def _commit(c: Commit) -> dict[str, str]:
    return {"sha": c.sha, "author": c.author, "date": c.date.isoformat(), "summary": c.summary}


def _site(s: LogSite) -> dict[str, Any]:
    return {"id": s.id, "level": s.level, "template": s.template, "logger": s.logger}


def build_server(cfg: Config) -> MCPServer:
    mcp = MCPServer("logsite", version=__version__, instructions=INSTRUCTIONS)
    window_default = cfg.window_hours

    def sites() -> list[LogSite]:
        # Re-indexed on every call so answers follow the code as it changes.
        return index_project(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))

    def backend() -> LogBackend:
        try:
            b = from_config(cfg.backend)
        except (ValueError, RuntimeError) as exc:
            raise ToolError(f"bad [backend] config: {exc}") from exc
        if b is None:
            raise ToolError("no [backend] configured in logsite.toml or [tool.logsite]")
        return b

    def counts(b: LogBackend, all_sites: list[LogSite], time_range: TimeRange) -> dict[str, int]:
        try:
            return b.count_by_line(all_sites, time_range)
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc

    @mcp.tool(annotations=READ_ONLY)
    def list_log_calls(
        path_contains: str | None = None, level: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """List log calls in the codebase with their level and message template.

        Placeholders in templates are normalised: %-style stays as is, f-string and
        .format() fields become "{}". template is null when the message is built at runtime.
        """
        out = [
            _site(s)
            for s in sites()
            if (path_contains is None or path_contains in s.path)
            and (level is None or s.level == level)
        ]
        return out[:limit]

    @mcp.tool(annotations=READ_ONLY)
    def list_rules() -> dict[str, str]:
        """logsite lint rules: LL0xx are static, LL1xx need production counts."""
        return dict(RULES)

    @mcp.tool(annotations=READ_ONLY)
    def lint(paths: list[str] | None = None, volume: bool = False) -> list[dict[str, Any]]:
        """Run the log-quality linter.

        paths are relative to the project root (default: the whole project). volume=True also
        runs the rules that need production counts (noisy, never emitted, spiked).
        """
        targets = [cfg.root / p for p in paths] if paths else [cfg.root]
        for t in targets:
            if not t.resolve().is_relative_to(cfg.root):
                raise ToolError(f"{t} is outside the project root")
        diags = lint_paths(targets, cfg.lint, root=cfg.root)
        if volume:
            b, all_sites = backend(), sites()
            current = TimeRange.last(window_default)
            now = counts(b, all_sites, current)
            before = counts(b, all_sites, current.previous()) if cfg.lint.enabled("LL102") else None
            samples = None
            if cfg.lint.needs_samples():
                try:
                    samples = sample_records(all_sites, b.query_logs(current))
                except Exception as exc:
                    raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
            diags += lint_volume(
                all_sites,
                now,
                cfg.lint,
                window=f"{window_default:g}h",
                previous=before,
                samples=samples,
            )
        diags.sort(key=lambda d: (d.path, d.line, d.code))
        return [asdict(d) for d in diags]

    @mcp.tool(annotations=READ_ONLY)
    def log_counts(hours: float | None = None, top: int = 20) -> list[dict[str, Any]]:
        """The log calls that emitted the most lines in production over the last `hours`."""
        all_sites = sites()
        by_id = {s.id: s for s in all_sites}
        now = counts(backend(), all_sites, TimeRange.last(hours or window_default))
        ranked = sorted(now.items(), key=lambda kv: kv[1], reverse=True)[:top]
        return [{**_site(by_id[sid]), "count": n} for sid, n in ranked]

    @mcp.tool(annotations=READ_ONLY)
    def log_costs(hours: float | None = None, top: int = 20) -> dict[str, Any]:
        """The log calls that cost the most: events, bytes and estimated spend per month,
        projected from the last `hours`. Spend needs [cost] price_per_gb in the config;
        without it, lines are ranked by volume.
        """
        hours = hours or window_default
        all_sites, b = sites(), backend()
        try:
            usage = b.usage_by_line(all_sites, TimeRange.last(hours))
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        costs = monthly_costs(all_sites, usage, hours, cfg.cost)
        return {
            "currency": cfg.cost.currency,
            "priced": cfg.cost.priced,
            "total_cost_per_month": round(sum(c.cost_per_month for c in costs), 2),
            "total_bytes_per_month": round(sum(c.bytes_per_month for c in costs)),
            "lines": [
                {
                    **_site(c.site),
                    "events_per_month": round(c.events_per_month),
                    "bytes_per_month": round(c.bytes_per_month),
                    "cost_per_month": round(c.cost_per_month, 2),
                    "avg_bytes": round(c.avg_bytes),
                    "estimated": c.estimated,
                }
                for c in costs[:top]
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def spikes(
        hours: float | None = None,
        ratio: float | None = None,
        min_count: int | None = None,
        commits: int = 3,
    ) -> list[dict[str, Any]]:
        """Log calls whose volume over the last `hours` grew by at least `ratio`x compared
        with the `hours` before, with the commits that touched the surrounding function in
        that time (the likely causes). Defaults come from the project's logsite config.
        """
        lint_cfg = replace(
            cfg.lint,
            spike_ratio=cfg.lint.spike_ratio if ratio is None else ratio,
            spike_min_count=cfg.lint.spike_min_count if min_count is None else min_count,
        )
        b, all_sites = backend(), sites()
        by_id = {s.id: s for s in all_sites}
        current = TimeRange.last(hours or window_default)
        now = counts(b, all_sites, current)
        before = counts(b, all_sites, current.previous())
        out = []
        for sid, n, prev in find_spikes(now, before, lint_cfg):
            site = by_id[sid]
            entry = {**_site(site), "count": n, "previous": prev, "trend": describe_trend(n, prev)}
            if commits:
                history = site_history(
                    cfg.root,
                    site.path,
                    site.line,
                    since=current.previous().start,
                    max_count=commits,
                )
                entry["commits"] = [_commit(c) for c in history]
            out.append(entry)
        return out

    @mcp.tool(name="compare_deploy", annotations=READ_ONLY)
    def compare_deploy_tool(
        ref: str,
        base: str | None = None,
        deployed_at: str | None = None,
        commits: int = 3,
    ) -> dict[str, Any]:
        """What changed in production after a release: log lines that spiked, and error lines
        that never fired before it, each with the commits in that release that touched the
        surrounding code.

        ref is a git tag or sha. base is the previous release (default: the tag before ref).
        deployed_at is the real deploy time in ISO 8601 (default: the tag or commit date).
        Compares deploy..now with an equally long window just before the deploy.
        """
        try:
            at = parse_iso(deployed_at) if deployed_at else None
            deploy = resolve_deploy(cfg.root, ref, at=at, base=base)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        b, all_sites = backend(), sites()
        try:
            report = compare_deploy(cfg.root, all_sites, b, deploy, cfg.lint, commits=commits)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        return {
            "ref": deploy.ref,
            "sha": deploy.sha,
            "deployed_at": deploy.at.isoformat(),
            "base": deploy.base,
            "release_commits": report.release_size,
            "before": {
                "start": report.before.start.isoformat(),
                "end": report.before.end.isoformat(),
            },
            "after": {"start": report.after.start.isoformat(), "end": report.after.end.isoformat()},
            "changes": [
                {
                    **_site(c.site),
                    "kind": c.kind,
                    "before": c.before,
                    "after": c.after,
                    "trend": c.trend,
                    "commits": [_commit(x) for x in c.commits],
                }
                for c in report.changes
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def error_summary(
        hours: float | None = None,
        since_deploy: str | None = None,
        deployed_at: str | None = None,
        top: int = 10,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Production errors grouped by cause: the answer to "what's breaking right now?".

        Thousands of error records and tracebacks become a few groups, ranked new first, then
        spiking, then by count. Each has the exception (and its causes), how often it
        happened vs the window before, where it was raised in this project, the log call
        that logged it, the call path, and a sample with its trace id (pass that to
        `request_timeline` to see the whole request). Values in messages are masked as <n>,
        <str>, <id> when they differ within a group.

        hours: window (default: the configured window). since_deploy: a git tag or sha;
        summarizes deploy..now vs an equally long window before it. deployed_at: the real
        deploy time in ISO 8601 (default: the tag or commit date).
        """
        try:
            window, _ = error_window(cfg.root, hours or window_default, since_deploy, deployed_at)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        b = backend()
        app = AppFiles.scan(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
        fields = dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"])
        try:
            summary = summarize_errors(
                b, sites(), app, window, window.previous(), cfg.lint, cfg.leaks, limit, fields
            )
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        from . import history

        history.apply(cfg, summary.groups, window.start, window.end)
        return summary_json(summary, top)

    @mcp.tool(annotations=READ_ONLY)
    def investigate(
        hours: float | None = None,
        since_deploy: str | None = None,
        deployed_at: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Production errors linked into incidents: "what's the outage, and what caused it?".

        Error groups that the same requests ran into, or that started together while new or
        spiking, are one incident. Each has its cause (the earliest group with an exception:
        root cause, the line of this project that raised it), its effects in the order they
        started, the deploy (git tag) just before it started, and suspect commits that
        changed the code around the line that raised it. Steady background errors are
        listed separately as "ongoing". Pass an example trace id to `request_timeline` to
        follow one failed request.
        """
        from datetime import datetime, timezone

        from .incidents import add_context, find_incidents, incident_json

        try:
            window, _ = error_window(cfg.root, hours or window_default, since_deploy, deployed_at)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        b = backend()
        app = AppFiles.scan(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
        fields = dict.fromkeys([cfg.timeline.field_name, "trace_id", "request_id"])
        try:
            summary = summarize_errors(
                b, sites(), app, window, window.previous(), cfg.lint, cfg.leaks, limit, fields
            )
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        from . import history

        history.apply(cfg, summary.groups, window.start, window.end)
        incidents, ongoing = find_incidents(summary.groups, cfg.lint)
        now = datetime.now(timezone.utc)
        from .diagnose import explain

        for inc in incidents:
            add_context(inc, cfg.root, now)
            explain(inc, summary, cfg.lint, b, cfg.timeline.field_name)
        from .errors import group_json

        return {
            "incidents": [incident_json(i) for i in incidents],
            "ongoing": [group_json(g) for g in ongoing[:20]],
        }

    @mcp.tool(name="find_leaks", annotations=READ_ONLY)
    def find_leaks_tool(hours: float | None = None, limit: int = 5000) -> dict[str, Any]:
        """Secrets (tokens, keys, passwords) and personal data (emails, card numbers, IBANs,
        SSNs) found in production log contents, grouped by the log call that wrote them.

        Scans up to `limit` recent records from the last `hours`. Sample values are masked.
        """
        b = backend()
        try:
            records = b.query_logs(TimeRange.last(hours or window_default), limit=limit)
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        leaks, scanned = find_leaks(sites(), records, cfg.leaks)
        return {
            "scanned": scanned,
            "capped": scanned >= limit,
            "leaks": [
                {
                    "id": e.id,
                    "template": e.site.template if e.site else None,
                    "records": e.records,
                    "kinds": e.kinds,
                    "samples": {k: asdict(f) for k, f in e.samples.items()},
                }
                for e in leaks
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def context_coverage(hours: float | None = None, limit: int = 5000) -> dict[str, Any]:
        """How often production records actually carry the context fields the project
        requires ([context] in logsite.toml: request_id, tenant_id, ...), overall and per log
        call. Low coverage on a field that's added automatically means that setup doesn't run
        on some code paths (threads, async tasks, background jobs).
        """
        rules = cfg.lint.context
        all_sites, b = sites(), backend()
        if rules.detect:
            rules.detected = detect_injected(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
        try:
            records = b.query_logs(TimeRange.last(hours or window_default), limit=limit)
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        per_site = measure_coverage(
            all_sites, sample_records(all_sites, records), cfg.lint.required_context
        )
        return {
            "min_coverage": rules.min_coverage,
            "fields": [
                {
                    "field": c.field,
                    "coverage": round(c.ratio, 4),
                    "records": c.total,
                    "added_automatically": satisfied(c.field, rules.available()),
                }
                for c in overall_coverage(per_site)
            ],
            "gaps": [
                {
                    "id": sid,
                    "field": c.field,
                    "situation": c.situation,
                    "coverage": round(c.ratio, 4),
                    "records": c.total,
                }
                for sid, cov in per_site.items()
                for c in cov
                if c.ratio < rules.min_coverage and c.total >= rules.min_records
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    def structure_score(production: bool = False, hours: float | None = None) -> dict[str, Any]:
        """How much of the codebase logs a constant message with values in extra= fields
        (searchable, groupable) versus values formatted into the text. production=True also
        weights by real volume and lists the loudest unstructured log calls.
        """
        report = score_project(cfg.root, DEFAULT_EXCLUDES | set(cfg.excludes))
        out: dict[str, Any] = {
            "structured": report.overall.structured,
            "total": report.overall.total,
            "score": round(report.overall.ratio, 4),
            "by_area": {
                name: {"structured": s.structured, "total": s.total, "score": round(s.ratio, 4)}
                for name, s in report.by_area.items()
            },
        }
        if production:
            b = backend()
            all_sites = sites()
            now = counts(b, all_sites, TimeRange.last(hours or window_default))
            p = production_score(report, now)
            out["production"] = {
                "structured_lines": p.structured,
                "lines": p.total,
                "score": round(p.ratio, 4),
            }
            out["loudest_unstructured"] = [
                {**_site(s), "count": now[s.id]}
                for s in sorted(report.unstructured, key=lambda s: -now.get(s.id, 0))[:10]
                if now.get(s.id)
            ]
        return out

    @mcp.tool(annotations=READ_ONLY)
    def request_timeline(
        value: str, field: str | None = None, hours: float | None = None, limit: int = 1000
    ) -> dict[str, Any]:
        """Every log line for one request, across services, in time order: the way to follow
        a failed request end to end. value is the trace id (or request id; set `field`).
        Lines written by this codebase include the log call ("site") that wrote them;
        secrets and personal data in messages are masked.
        """
        tcfg = cfg.timeline
        try:
            b = from_config(tcfg.backend) if tcfg.backend else backend()
        except (ValueError, RuntimeError) as exc:
            raise ToolError(f"bad [timeline.backend] config: {exc}") from exc
        if b is None:
            raise ToolError("no [backend] configured in logsite.toml or [tool.logsite]")
        try:
            tl = find_timeline(
                b,
                sites(),
                field or tcfg.field_name,
                value,
                TimeRange.last(hours or window_default),
                limit=limit,
                here=tcfg.service,
                leaks=cfg.leaks,
            )
        except Exception as exc:
            raise ToolError(f"couldn't query the {b.name} backend: {exc}") from exc
        out = timeline_json(tl)

        def brief(e):
            if e is None:
                return None
            site = e.site.id if e.site else None
            return {"service": e.service, "level": e.level, "message": e.message, "site": site}

        out["first_problem"] = brief(tl.first_problem)
        out["first_error"] = brief(tl.first_error)
        return out

    @mcp.tool(annotations=READ_ONLY)
    def log_history(target: str, max_count: int = 10) -> list[dict[str, str]]:
        """Commits (newest first) that changed the function around a log call.

        target is "PATH:LINE" with PATH relative to the project root, e.g. "app/checkout.py:42".
        """
        try:
            path, line = parse_target(target, cfg.root, base=cfg.root)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return [_commit(c) for c in site_history(cfg.root, path, line, max_count=max_count)]

    @mcp.tool(annotations=READ_ONLY)
    def production_context(
        path: str, line: int | None = None, end_line: int | None = None, hours: float | None = None
    ) -> dict[str, Any]:
        """What production says about a piece of code, before you change it.

        path is relative to the project root. With `line` alone, the function around it
        (Python) or the lines near it; with `end_line`, that range; with neither, the file.
        Returns the error groups raised, logged or passing through there in the last
        `hours` (with their history: `history.regression` means it came back after a fix,
        `history.fixed_by` the commits that fixed it then), the log calls there with volume
        and monthly cost, recent commits, and lint findings. Without a backend, the
        production parts are listed under `unavailable` and the rest is still returned.
        """
        from .about import about, about_json, parse_span

        target = path if line is None else f"{path}:{line}" + (f"-{end_line}" if end_line else "")
        try:
            span = parse_span(cfg.root, target)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return about_json(about(cfg, span, hours))

    return mcp


def serve(cfg: Config) -> None:
    build_server(cfg).run("stdio")
