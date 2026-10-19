"""Log-quality linter.

Static rules (offline, LL0xx) look at the code alone. Volume rules (LL1xx) also need
per-line counts from a backend.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .context import detect_injected, measure_coverage, satisfied
from .index import index_calls, iter_source_files
from .models import _PLACEHOLDER, Diagnostic, LogRecord, LogSite

RULES: dict[str, str] = {
    "LL001": "eager string formatting in log call",
    "LL002": "possible PII or secret in log call",
    "LL003": "error logged in except block without traceback",
    "LL004": "info/debug log inside a loop",
    "LL005": "dynamic log message cannot be traced to production",
    "LL006": "log call missing required context field",
    "LL007": "failure logged below warning",
    "LL008": "values formatted into the message instead of structured fields (opt-in)",
    "LL100": "noisy log line",
    "LL101": "log line never emitted",
    "LL102": "log line spiked compared with the previous window",
    "LL104": "log level doesn't match what the line does in production",
    "LL105": "required context missing from production records",
}

# Rules that are off unless selected: LL008 flags every %-style message, which is idiomatic
# Python logging, so it's only for teams that have chosen structured logging.
OPT_IN = {"LL008"}

DEFAULT_PII_PATTERN = (
    r"passw(or)?d|passphrase|^pass$|secret|token|api_?key|auth(?!or)|credential|^ssn$|social_security"
    r"|credit_?card|card_?(number|no)|cvv|iban|email|phone|dob|date_of_birth"
)


DEFAULT_FAILURE_PATTERN = (
    r"\b(?:fail(?:s|ed|ure|ures|ing)?|errors?|errored|exceptions?|timed out|time ?out|"
    r"unable to|cannot|can't|could ?not|couldn't|refused|denied|unreachable|crash(?:ed)?|"
    r"fatal|panic(?:ked)?|abort(?:ed)?)\b"
)
# Mentions that aren't failures: "no errors", "0 failures", "error rate".
_NOT_A_FAILURE = re.compile(
    r"\b(?:no|without|zero|0)\s+(?:errors?|failures?|exceptions?)\b|\berror[_ ]?rates?\b",
    re.IGNORECASE,
)
LOW_LEVELS = {"debug", "info"}
ERROR_LEVELS = {"error", "critical", "exception"}


SITUATIONS = {
    "all": "every log",
    "error": "errors",
    "in_retry": "retry loops",
    "near_dependency": "dependency calls",
}


@dataclass
class ContextRules:
    """Which context fields LL006 requires, by situation (the ``[context]`` table)."""

    all: list[str] = field(default_factory=list)
    error: list[str] = field(default_factory=list)  # error / critical / exception calls
    in_retry: list[str] = field(default_factory=list)
    near_dependency: list[str] = field(default_factory=list)
    injected: set[str] = field(default_factory=set)  # declared: added outside the code
    detect: bool = True  # scan the project for filters / contextvars / OpenTelemetry
    min_coverage: float = 0.95  # LL105: share of production records that must carry a field
    min_records: int = 20  # LL105: ignore lines with fewer sampled records
    detected: dict[str, list[str]] = field(default_factory=dict)  # filled in at lint time

    @property
    def active(self) -> bool:
        return bool(self.all or self.error or self.in_retry or self.near_dependency)

    def available(self) -> set[str]:
        return self.injected | set(self.detected)

    def required(self, site: LogSite) -> list[tuple[str, str]]:
        """``(field, situation)`` pairs this call must carry, most general first."""
        out: list[tuple[str, str]] = [(f, "all") for f in self.all]
        if site.level in ERROR_LEVELS:
            out += [(f, "error") for f in self.error]
        if site.in_retry:
            out += [(f, "in_retry") for f in self.in_retry]
        if site.near_dependency:
            out += [(f, "near_dependency") for f in self.near_dependency]
        seen: set[str] = set()
        return [(f, why) for f, why in out if not (f in seen or seen.add(f))]

    @classmethod
    def from_dict(cls, data: Mapping) -> ContextRules:
        unknown = set(data) - {*SITUATIONS, "injected", "detect", "min_coverage", "min_records"}
        if unknown:
            raise ValueError(f"unknown [context] key(s) {sorted(unknown)}")
        return cls(
            all=list(data.get("all", [])),
            error=list(data.get("error", [])),
            in_retry=list(data.get("in_retry", [])),
            near_dependency=list(data.get("near_dependency", [])),
            injected=set(data.get("injected", [])),
            detect=bool(data.get("detect", True)),
            min_coverage=float(data.get("min_coverage", 0.95)),
            min_records=int(data.get("min_records", 20)),
        )


@dataclass
class LintConfig:
    select: set[str] = field(default_factory=lambda: set(RULES) - OPT_IN)
    ignore: set[str] = field(default_factory=set)
    pii_pattern: str = DEFAULT_PII_PATTERN
    # LL006 is off unless you list fields, e.g. ["trace_id"]; same as [context] all
    required_fields: list[str] = field(default_factory=list)
    context: ContextRules = field(default_factory=ContextRules)
    noisy_threshold: int = 100_000  # LL100: emits per window
    spike_ratio: float = 5.0  # LL102: current window >= ratio x previous window ...
    spike_min_count: int = 1_000  # ... and at least this many emits
    dead_levels: set[str] = field(default_factory=lambda: {"warning", "error", "critical"})
    failure_pattern: str = DEFAULT_FAILURE_PATTERN  # LL007 / LL104: words that mean "it failed"
    error_noisy_threshold: int = 10_000  # LL104: error lines firing this often per window

    def failure_word(self, text: str | None) -> str | None:
        """The first failure word in ``text`` (ignoring "no errors"-style mentions), or None."""
        if not text:
            return None
        m = re.search(self.failure_pattern, _NOT_A_FAILURE.sub(" ", text), re.IGNORECASE)
        return m.group(0) if m else None

    def required_context(self, site: LogSite) -> list[tuple[str, str]]:
        """``(field, situation)`` pairs a call must carry: ``required_fields`` + ``[context]``."""
        out = [(f, "all") for f in self.required_fields] + self.context.required(site)
        seen: set[str] = set()
        return [(f, why) for f, why in out if not (f in seen or seen.add(f))]

    def needs_samples(self) -> bool:
        """Whether volume linting should fetch records (LL104, or LL105 with rules set)."""
        context = self.required_fields or self.context.active
        return self.enabled("LL104") or (self.enabled("LL105") and bool(context))

    def enabled(self, code: str) -> bool:
        return code in self.select and code not in self.ignore

    @classmethod
    def from_dict(cls, data: Mapping, context: Mapping | None = None) -> LintConfig:
        cfg = cls()
        cfg.context = ContextRules.from_dict(context or {})
        if "select" in data:
            cfg.select = set(data["select"])
        cfg.select |= set(data.get("extend_select", []))
        unknown = (cfg.select | set(data.get("ignore", []))) - set(RULES)
        if unknown:
            raise ValueError(f"unknown lint rule(s) {sorted(unknown)}; see `logsite rules`")
        cfg.ignore = set(data.get("ignore", []))
        cfg.pii_pattern = data.get("pii_pattern", cfg.pii_pattern)
        cfg.required_fields = list(data.get("required_fields", []))
        cfg.noisy_threshold = int(data.get("noisy_threshold", cfg.noisy_threshold))
        cfg.spike_ratio = float(data.get("spike_ratio", cfg.spike_ratio))
        cfg.spike_min_count = int(data.get("spike_min_count", cfg.spike_min_count))
        if "dead_levels" in data:
            cfg.dead_levels = set(data["dead_levels"])
        cfg.failure_pattern = data.get("failure_pattern", cfg.failure_pattern)
        cfg.error_noisy_threshold = int(
            data.get("error_noisy_threshold", cfg.error_noisy_threshold)
        )
        return cfg


def is_spike(current: int, previous: float, cfg: LintConfig) -> bool:
    """True if a line went from ``previous`` to ``current`` emits fast enough to flag."""
    return current >= cfg.spike_min_count and current >= cfg.spike_ratio * max(previous, 1)


def find_spikes(
    current: Mapping[str, int], previous: Mapping[str, int], cfg: LintConfig
) -> list[tuple[str, int, int]]:
    """``(site_id, current, previous)`` for every spiking line, biggest jump first."""
    spikes = [(sid, n, previous.get(sid, 0)) for sid, n in current.items()]
    spikes = [t for t in spikes if is_spike(t[1], t[2], cfg)]
    spikes.sort(key=lambda t: t[1] / max(t[2], 1), reverse=True)
    return spikes


def describe_trend(current: int, previous: int) -> str:
    """``"8.0x"`` or ``"new"`` (no emits in the previous window)."""
    return f"{current / previous:.1f}x" if previous else "new"


def _where_fields_go(site: LogSite) -> str:
    return "in extra=" if site.library == "logging" else "as keyword arguments"


def _diag(site: LogSite, code: str, message: str, severity: str = "warning") -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        path=site.path,
        line=site.line,
        col=site.col,
        end_line=site.end_line,
        end_col=site.end_col,
        severity=severity,
    )


def _message_arg(site: LogSite, call: ast.Call) -> ast.expr | None:
    idx = 1 if site.level == "log" else 0
    if len(call.args) > idx:
        return call.args[idx]
    return next((k.value for k in call.keywords if k.arg == "msg"), None)


def _names_in(node: ast.AST) -> Iterable[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id
        elif isinstance(sub, ast.Attribute):
            yield sub.attr
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            # dict keys in extra={"password": ...}
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sub.value):
                yield sub.value


def _is_eager(node: ast.expr) -> str | None:
    if isinstance(node, ast.JoinedStr) and any(
        isinstance(v, ast.FormattedValue) for v in node.values
    ):
        return "f-string"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return "str.format()"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod) and isinstance(node.left, (ast.Constant, ast.JoinedStr)):
            return "% formatting"
        if isinstance(node.op, ast.Add):
            return "string concatenation"
    return None


def interpolated_values(site: LogSite, call: ast.Call, msg: ast.expr) -> list[ast.expr]:
    """The expressions formatted into a log message: %-args, f-string fields, .format() args."""
    if isinstance(msg, ast.JoinedStr):
        return [v.value for v in msg.values if isinstance(v, ast.FormattedValue)]
    if isinstance(msg, ast.Call) and isinstance(msg.func, ast.Attribute):
        if msg.func.attr == "format":
            return [*msg.args, *(k.value for k in msg.keywords)]
    if isinstance(msg, ast.BinOp) and isinstance(msg.op, ast.Mod):
        right = msg.right
        return list(right.elts) if isinstance(right, ast.Tuple) else [right]
    if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
        start = 2 if site.level == "log" else 1
        if site.library == "loguru":  # str.format: "{}" takes args, "{user}" takes user=
            fields = re.findall(r"(?<!\{)\{(\w*)[^{}]*\}", msg.value)
            named = {f for f in fields if f and not f.isdigit()}
            positional = list(call.args[start:]) if len(fields) > len(named) else []
            return positional + [k.value for k in call.keywords if k.arg in named]
        if _PLACEHOLDER.search(msg.value.replace("%%", "")):
            return list(call.args[start:])
    return []


def _extra_keys(call: ast.Call, site: LogSite | None = None) -> set[str] | None:
    """Context fields the call passes; None if they can't be known statically.

    stdlib: keys of a literal ``extra={...}``. structlog / loguru: keyword arguments, plus
    fields bound with ``.bind(...)``.
    """
    if site is not None and site.library != "logging":
        if any(k.arg is None for k in call.keywords):
            return None  # **ctx
        skip = {"exc_info", "stack_info", "event"}
        return {k.arg for k in call.keywords if k.arg not in skip} | set(site.bound_fields)
    for kw in call.keywords:
        if kw.arg == "extra":
            if isinstance(kw.value, ast.Dict):
                return {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
            return None  # extra=ctx: can't tell statically, don't flag
    return set()


def check_call(site: LogSite, call: ast.Call, cfg: LintConfig, pii: re.Pattern) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    msg = _message_arg(site, call)

    if cfg.enabled("LL001") and msg is not None:
        kind = _is_eager(msg)
        if kind:
            out.append(
                _diag(
                    site,
                    "LL001",
                    f"{kind} is evaluated even when the level is disabled and breaks "
                    'log grouping; pass args instead: logger.info("x=%s", x)',
                )
            )

    if cfg.enabled("LL002"):
        values: list[ast.AST] = list(call.args[1 if site.level == "log" else 0 :])
        values += [k.value for k in call.keywords if k.arg not in ("exc_info", "stack_info")]
        names = {n for v in values for n in _names_in(v)}
        if site.library != "logging":  # log.info("signup", password=pw): the key is the field
            names |= {k.arg for k in call.keywords if k.arg}
        hits = sorted({n for n in names if pii.search(n.lower())})
        if hits:
            out.append(_diag(site, "LL002", f"possible PII/secret logged: {', '.join(hits)}"))

    if cfg.enabled("LL003") and site.in_except and site.level in ("error", "critical"):
        if not (site.exc_opt or any(k.arg == "exc_info" for k in call.keywords)):
            fix = (
                "logger.exception(...) or logger.opt(exception=True).error(...)"
                if site.library == "loguru"
                else "logger.exception(...) or pass exc_info=True"
            )
            out.append(_diag(site, "LL003", f"traceback is lost; use {fix}"))

    if cfg.enabled("LL004") and site.in_loop and site.level in ("debug", "info"):
        out.append(
            _diag(
                site,
                "LL004",
                f"{site.level} log inside a loop can flood your log store",
                severity="info",
            )
        )

    if cfg.enabled("LL005") and site.template is None and msg is not None:
        out.append(
            _diag(
                site,
                "LL005",
                "message is built at runtime, so logsite can't match production logs to this "
                "line; use a literal template",
                severity="hint",
            )
        )

    if cfg.enabled("LL007") and site.level in LOW_LEVELS:
        word = cfg.failure_word(site.template)
        if word:
            out.append(
                _diag(
                    site,
                    "LL007",
                    f'"{word}" is logged at {site.level}, so alerts on warning/error never see '
                    "it; use logger.warning or logger.error",
                )
            )
        elif site.in_swallowing_except:
            out.append(
                _diag(
                    site,
                    "LL007",
                    f"a broad except swallows the error and logs it at {site.level}, so nothing "
                    "will alert on it; log at error with exc_info=True, or catch a narrower "
                    "exception",
                    severity="info",
                )
            )

    if cfg.enabled("LL008") and msg is not None:
        values = interpolated_values(site, call, msg)
        if values:
            out.append(
                _diag(
                    site,
                    "LL008",
                    f"{len(values)} value(s) formatted into the message; keep the message "
                    f"constant and pass values {_where_fields_go(site)} so they can be queried "
                    "(quick fix available in the editor)",
                    severity="info",
                )
            )

    if cfg.enabled("LL006") and (cfg.required_fields or cfg.context.active):
        keys = _extra_keys(call, site)
        if keys is not None:
            rules = cfg.context
            have = keys | rules.available()
            missing: dict[str, str] = {}
            for f, why in cfg.required_context(site):
                if not satisfied(f, have):
                    missing.setdefault(f, why)
            if missing:
                parts = [
                    f if why == "all" else f"{f} ({SITUATIONS[why]})" for f, why in missing.items()
                ]
                out.append(
                    _diag(
                        site,
                        "LL006",
                        f"missing context: {', '.join(parts)}; pass it {_where_fields_go(site)} "
                        "or add it to every log automatically (logging filter, contextvars)",
                    )
                )
    return out


def lint_source(
    source: str, path: str = "<string>", cfg: LintConfig | None = None
) -> list[Diagnostic]:
    cfg = cfg or LintConfig()
    pii = re.compile(cfg.pii_pattern)
    from .lang import MissingParser, language_of
    from .lang import index_calls as index_other

    language = language_of(path)
    out: list[Diagnostic] = []
    if language != "python":
        from .lang.rules import check_facts

        try:
            calls = index_other(language, source, path)
        except MissingParser:
            return []
        for site, facts in calls:
            out.extend(check_facts(site, facts, cfg, pii))
        return out
    for site, call in index_calls(source, path):
        out.extend(check_call(site, call, cfg, pii))
    return out


def lint_paths(
    paths: Iterable[str | Path], cfg: LintConfig | None = None, root: Path | None = None
) -> list[Diagnostic]:
    root = (root or Path.cwd()).resolve()
    cfg = cfg or LintConfig()
    if cfg.enabled("LL006") and cfg.context.active and cfg.context.detect:
        cfg.context.detected = detect_injected(root)
    out: list[Diagnostic] = []
    for p in paths:
        p = Path(p).resolve()
        files = [p] if p.is_file() else iter_source_files(p)
        for f in files:
            try:
                rel = f.relative_to(root).as_posix()
            except ValueError:
                rel = f.as_posix()
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(lint_source(text, rel, cfg))
    return out


def lint_volume(
    sites: Iterable[LogSite],
    counts: Mapping[str, int],
    cfg: LintConfig | None = None,
    window: str = "24h",
    previous: Mapping[str, int] | None = None,
    samples: Mapping[str, Sequence[LogRecord]] | None = None,
) -> list[Diagnostic]:
    """Rules that need production counts from a backend.

    ``previous`` holds counts for the window just before the current one; LL102 needs it.
    ``samples`` holds recent records per site id; LL104 and LL105 need it.
    """
    cfg = cfg or LintConfig()
    out: list[Diagnostic] = []
    for site in sites:
        n = counts.get(site.id, 0)
        if cfg.enabled("LL100") and n >= cfg.noisy_threshold:
            out.append(
                _diag(
                    site,
                    "LL100",
                    f"emitted {n:,} times in the last {window}; lower the level or sample it",
                )
            )
        if (
            cfg.enabled("LL101")
            and n == 0
            and site.level in cfg.dead_levels
            and site.template is not None
        ):
            out.append(
                _diag(
                    site,
                    "LL101",
                    f"not seen in production in the last {window}",
                    severity="hint",
                )
            )
        if cfg.enabled("LL102") and previous is not None:
            before = previous.get(site.id, 0)
            if is_spike(n, before, cfg):
                out.append(
                    _diag(
                        site,
                        "LL102",
                        f"spiked to {n:,} in the last {window}, up from {before:,} the window "
                        f"before; see recent changes: logsite history {site.id}",
                        severity="info",
                    )
                )
        if cfg.enabled("LL104"):
            out.extend(_severity_in_production(site, n, cfg, window, samples))
        if cfg.enabled("LL105") and samples:
            out.extend(_context_in_production(site, cfg, samples))
    return out


def _context_in_production(
    site: LogSite, cfg: LintConfig, samples: Mapping[str, Sequence[LogRecord]]
) -> list[Diagnostic]:
    rules = cfg.context
    records = samples.get(site.id, [])
    if len(records) < rules.min_records:
        return []
    coverage = measure_coverage([site], {site.id: records}, cfg.required_context)
    low = [c for c in coverage.get(site.id, []) if c.ratio < rules.min_coverage]
    if not low:
        return []
    parts = []
    for c in low:
        why = "" if c.situation == "all" else f" ({SITUATIONS[c.situation]})"
        parts.append(f"{c.field} {c.ratio:.0%}{why}")
    injected = [c.field for c in low if satisfied(c.field, rules.available())]
    hint = (
        f"; {', '.join(injected)} is meant to be added automatically, but that setup "
        "doesn't reach this code path"
        if injected
        else ""
    )
    return [
        _diag(
            site,
            "LL105",
            f"in {len(records)} sampled production records, required context is often missing: "
            f"{', '.join(parts)}{hint}. Context is usually lost in threads, async tasks, "
            "background jobs or a handler without the filter",
        )
    ]


def _severity_in_production(
    site: LogSite,
    n: int,
    cfg: LintConfig,
    window: str,
    samples: Mapping[str, Sequence[LogRecord]] | None,
) -> list[Diagnostic]:
    if site.level in ERROR_LEVELS and n >= cfg.error_noisy_threshold:
        return [
            _diag(
                site,
                "LL104",
                f"logged as {site.level} {n:,} times in the last {window}; an error that fires "
                "constantly teaches people to ignore alerts. Fix the cause, or lower the level "
                "if it's expected",
                severity="info",
            )
        ]
    messages = [r.message for r in (samples or {}).get(site.id, [])]
    if site.level in LOW_LEVELS and messages and not cfg.failure_word(site.template):
        words = [w for w in map(cfg.failure_word, messages) if w]
        if words:
            # Only the matched word is shown: the messages themselves may hold personal data.
            return [
                _diag(
                    site,
                    "LL104",
                    f"{len(words)} of {len(messages)} sampled production messages look like "
                    f'failures ("{words[0]}") but are logged at {site.level}; log failures at '
                    "warning or error",
                )
            ]
    return []
