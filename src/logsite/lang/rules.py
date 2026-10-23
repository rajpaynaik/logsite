"""The lint rules that don't depend on the language (LL001–LL005, LL007), checked from the
facts the Java, Go and TypeScript indexers collect. Python has its own, richer checks in
``lint.py``."""

from __future__ import annotations

import re

from ..lint import LOW_LEVELS, LintConfig, _diag
from ..models import Diagnostic, LogSite
from .base import CallFacts

HINTS = {
    "java": {
        "eager": 'use placeholders instead: log.info("order {} failed", orderId)',
        "lost": 'pass the exception as the last argument: log.error("payment failed", e)',
        "lost_message": (
            "e.getMessage() keeps the text but drops the stack trace; pass the exception "
            'itself as the last argument: log.error("payment failed", e)'
        ),
        "level": "use log.warn or log.error",
        "swallowed": (
            "a broad catch swallows the exception and logs it at {level}, so nothing will "
            "alert on it; log at error with the exception, or catch a narrower one"
        ),
    },
    "typescript": {
        "eager": 'pass values as an object instead: logger.info({ orderId }, "order failed")',
        "lost": 'pass the error itself: logger.error({ err }, "payment failed")',
        "lost_message": (
            "err.message keeps the text but drops the stack trace; pass the error itself: "
            'logger.error({ err }, "payment failed")'
        ),
        "level": "use logger.warn or logger.error",
        "swallowed": (
            "the error is caught here and logged at {level}, so nothing will alert on it; "
            "log at error with the error, or rethrow it"
        ),
    },
    "go": {
        "eager": 'pass values as fields instead: slog.Info("order failed", "order_id", id)',
        "eager_printf": "pass the values as arguments to the f-method instead",
        "lost": 'the error itself isn\'t logged; pass it: slog.Error("payment failed", "err", err)',
        "level": "use Warn or Error",
        "swallowed": (
            "the error is handled here and logged at {level}, so nothing will alert on it; "
            "log at error, or return it"
        ),
    },
}


def check_facts(
    site: LogSite, facts: CallFacts, cfg: LintConfig, pii: re.Pattern
) -> list[Diagnostic]:
    hints = HINTS[facts.language]
    out: list[Diagnostic] = []

    if cfg.enabled("LL001") and facts.eager:
        hint = hints["eager_printf"] if facts.printf and "eager_printf" in hints else hints["eager"]
        out.append(
            _diag(
                site,
                "LL001",
                f"{facts.eager} is evaluated even when the level is disabled and breaks log "
                f"grouping; {hint}",
            )
        )

    if cfg.enabled("LL002"):
        hits = sorted({n for n in facts.names if pii.search(n.lower())})
        if hits:
            out.append(_diag(site, "LL002", f"possible PII/secret logged: {', '.join(hits)}"))

    if cfg.enabled("LL003") and site.in_except and site.level in ("error", "critical"):
        if not facts.has_exception:
            key = "lost_message" if facts.exception_message_only else "lost"
            message = hints[key]
            if facts.language == "java" and key == "lost":
                message = f"stack trace is lost; {message}"
            out.append(_diag(site, "LL003", message))

    if cfg.enabled("LL004") and site.in_loop and site.level in ("debug", "info"):
        out.append(
            _diag(
                site,
                "LL004",
                f"{site.level} log inside a loop can flood your log store",
                severity="info",
            )
        )

    if cfg.enabled("LL005") and facts.dynamic:
        out.append(
            _diag(
                site,
                "LL005",
                "message is built at runtime, so logsite can't match production logs to this "
                "line; use a literal message",
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
                    f"it; {hints['level']}",
                )
            )
        elif site.in_swallowing_except:
            out.append(
                _diag(
                    site,
                    "LL007",
                    hints["swallowed"].format(level=site.level),
                    severity="info",
                )
            )
    return out
