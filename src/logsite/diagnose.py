"""Ranked explanations for an incident, each with the evidence for and against it.

An incident (``incidents.py``) says what failed, when, the deploy before it and the commits
around the code that raised it. This turns that into candidate causes:

- **a change**: the release or commit before it, when it touched the code that raised;
- **traffic**: more requests, the same error rate;
- **a dependency**: a call to another service or database failing (timeouts, refused
  connections), which a code change can make worse but often isn't the cause.

Each gets the evidence that supports it, the evidence against it, what logsite can't see,
and the cheapest check that tells it apart from the others. Scores only rank them: they
aren't probabilities. Everything here is deterministic; nothing is guessed by a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from .errors import ErrorGroup
from .incidents import Incident
from .lint import LintConfig

CLOSE_DEPLOY = timedelta(minutes=30)
SAME_RATE = 1.3  # a rate within 30% of before: unchanged
BUSY = 1.5  # traffic up 50% or more
# root causes that are a call to something else going wrong
DEPENDENCY = re.compile(
    r"Timeout|TimedOut|timed out|Connect(ion)?(Error|Exception|Refused|Reset)|Refused|"
    r"Unavailable|UnknownHost|NameResolution|DNS|SSL|TLS|BrokenPipe|EOF|"
    r"OperationalError|PSQLException|SQLTransient|Deadlock|CannotGetJdbcConnection|"
    r"\b50[234]\b|Bad Gateway|Service Unavailable|ECONNRESET|ECONNREFUSED|ETIMEDOUT",
    re.I,
)


@dataclass
class Hypothesis:
    key: str  # change | traffic | dependency
    title: str
    score: int = 0
    supporting: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    next_check: str = ""

    def add(self, points: int, text: str) -> None:
        self.score += points
        (self.supporting if points > 0 else self.against).append(text)


@dataclass
class Diagnosis:
    hypotheses: list[Hypothesis]  # best first
    unknown: list[str] = field(default_factory=list)  # gaps that affect every hypothesis

    @property
    def strength(self) -> str:
        """How clearly the evidence points one way: strong, moderate, weak or unclear."""
        if not self.hypotheses or self.hypotheses[0].score < 2:
            return "unclear"
        top = self.hypotheses[0].score
        runner = self.hypotheses[1].score if len(self.hypotheses) > 1 else 0
        if top >= 5 and top - runner >= 3:
            return "strong"
        return "moderate" if top >= 3 and top > runner else "weak"


def _minutes(delta: timedelta) -> str:
    m = delta.total_seconds() / 60
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f}h"


def _short(name: str | None) -> str:
    return (name or "").split(":", 1)[0].rsplit(".", 1)[-1]


def _change(inc: Incident, cause: ErrorGroup, cfg: LintConfig) -> Hypothesis | None:
    if not inc.deploy and not inc.suspects:
        return None
    if inc.suspects:
        c = inc.suspects[0]
        where = f" in {inc.deploy[0]}" if inc.deploy else ""
        title = f"The change{where}: {c.short_sha} {c.summary}"
    else:
        title = f"The release {inc.deploy[0]}"
    h = Hypothesis("change", title)
    start = inc.started
    if inc.deploy and start:
        gap = start - inc.deploy[1]
        if gap <= CLOSE_DEPLOY:
            h.add(3, f"{inc.deploy[0]} was deployed {_minutes(gap)} before the errors started")
        elif gap <= timedelta(hours=24):
            h.add(1, f"{inc.deploy[0]} was deployed {_minutes(gap)} before the errors started")
        if gap > timedelta(hours=2):
            h.add(-1, f"the deploy was {_minutes(gap)} before it started: a long gap")
    where = cause.raised_at or (cause.logged_at.id if cause.logged_at else None)
    if inc.suspects and where:
        shas = ", ".join(c.short_sha for c in inc.suspects[:3])
        h.add(3, f"{shas} changed the lines around {where}, where it's raised")
    elif inc.deploy and where:
        h.add(-1, f"no commit{' in ' + inc.deploy[0] if inc.deploy else ''} touched {where}")
    if cause.is_new:
        h.add(1, "the error wasn't there in the window before")
    rate = cause.rate_ratio
    if rate is not None and rate >= cfg.spike_ratio:
        h.add(1, f"its rate is {rate:.1f}x the window before, more than traffic explains")
    if cause.previous and rate is not None and rate < SAME_RATE:
        h.add(-3, f"the error was already there at the same rate before ({rate:.1f}x)")
    hist = cause.history
    if hist is not None and getattr(hist, "regression", False):
        fixes = ", ".join(f["sha"] for f in hist.fixed_by) or "an earlier release"
        h.add(2, f"it was fixed before ({fixes}) and came back: the change may undo that fix")
    if not inc.deploy:
        h.unknown.append("no release tag before it: when it was deployed isn't known")
    if not cause.raised_at:
        h.unknown.append("no stack frame of yours: which code raised it isn't known")
    if inc.suspects:
        c = inc.suspects[0]
        path = where.rsplit(":", 1)[0] if where else ""
        show = f"git show {c.short_sha}" + (f" -- {path}" if path else "")
        h.next_check = (
            f"read `{show}`; revert it (or roll back) in staging and see whether the error stops"
        )
    else:
        h.next_check = (
            f"diff {inc.deploy[0]} with the release before it around {where or 'the code'}"
        )
    return h


def _traffic(inc: Incident, cause: ErrorGroup, ratio: float | None) -> Hypothesis | None:
    if ratio is None:
        return None
    h = Hypothesis("traffic", "More traffic, not a new failure")
    if ratio >= BUSY:
        h.add(2, f"the service logged {ratio:.1f}x as much as in the window before")
    else:
        h.add(-1, f"traffic is about the same ({ratio:.1f}x)")
    rate = cause.rate_ratio
    if cause.is_new:
        h.add(-3, "the error didn't exist before: more traffic doesn't create a new error")
    elif rate is not None and rate < SAME_RATE:
        h.add(3, f"the error rate didn't change ({rate:.1f}x): the errors grew with the traffic")
    elif rate is not None and rate >= 2:
        h.add(-2, f"the error rate is {rate:.1f}x: more than the traffic explains")
    h.unknown.append("traffic is measured by log volume, not by requests")
    h.next_check = (
        "compare request counts per endpoint before and after; if the load is real, look at "
        "capacity (connection pools, timeouts, rate limits)"
    )
    return h if h.score > -4 else None


def _dependency(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    text = " ".join([cause.title, *cause.causes, cause.exc_type or ""])
    if not DEPENDENCY.search(text):
        return None
    root = _short(cause.causes[0] if cause.causes else cause.exc_type) or "the error"
    caller = f"{cause.raised_in}()" if cause.raised_in else None
    who = f"the service {caller} calls" if caller else "the service or database it calls"
    h = Hypothesis("dependency", f"A dependency failing: {root} from {who}")
    h.add(2, f"the root cause is {root}: a call to another service or database failed")
    if cause.origin:
        h.add(1, f"it was thrown inside {cause.origin}, not by your code")
    services = set().union(*(g.services.keys() for g in inc.groups))
    if len(services) > 1:
        h.add(1, f"{len(services)} services failed at the same time")
    if inc.suspects and cause.raised_at:
        h.add(-1, "but a recent commit changed the code that makes the call")
    h.unknown.append(f"logsite reads logs only: the latency and status of {who} aren't in them")
    since = f"{inc.started:%H:%M} UTC" if inc.started else "the start"
    h.next_check = (
        f"check the latency and error rate of {who} since {since} (its dashboard or status "
        "page); "
        "if it's slow for every caller, the change isn't the cause"
    )
    return h


def diagnose(inc: Incident, cfg: LintConfig, traffic_ratio: float | None = None) -> Diagnosis:
    cause = inc.cause
    found = [
        h
        for h in (_change(inc, cause, cfg), _traffic(inc, cause, traffic_ratio),
                  _dependency(inc, cause))
        if h is not None
    ]  # fmt: skip
    found.sort(key=lambda h: (-h.score, h.key))
    gaps = []
    if traffic_ratio is None:
        gaps.append("traffic before and after isn't known, so rates couldn't be compared")
    return Diagnosis(found, gaps)


def diagnosis_json(d: Diagnosis) -> dict:
    return {
        "strength": d.strength,
        "hypotheses": [
            {
                "cause": h.key,
                "title": h.title,
                "score": h.score,
                "supporting": h.supporting,
                "against": h.against,
                "unknown": h.unknown,
                "next_check": h.next_check,
            }
            for h in d.hypotheses
        ],
        "unknown": d.unknown,
        "note": "scores rank the explanations by evidence; they aren't probabilities",
    }


# --- the report ---------------------------------------------------------------------------------


def _md_group(g: ErrorGroup) -> str:
    return g.title.replace("|", "\\|")


def report_markdown(incidents: list[Incident], summary, label: str, cfg: LintConfig) -> str:
    """An incident report a person can check: what happened, the ranked explanations with
    their evidence, and what to check next."""
    from .report import short_class

    out = [f"# Incident report: errors {label}", ""]
    ratio = summary.traffic_ratio
    facts = f"{summary.events:,} errors in {len(summary.groups)} group(s)"
    if summary.previous_events is not None:
        facts += f" (window before: {summary.previous_events:,})"
    if ratio is not None:
        facts += f"; traffic {ratio:.1f}x the window before"
    out += [facts, ""]
    if not incidents:
        out += ["No incident: nothing new or spiking. See `logsite errors` for steady errors."]
        return "\n".join(out) + "\n"
    for n, inc in enumerate(incidents, 1):
        cause = inc.cause
        d = inc.diagnosis or diagnose(inc, cfg, ratio)
        started = f"{inc.started:%Y-%m-%d %H:%M} UTC" if inc.started else "unknown"
        out += [f"## {n}. {short_class(cause.title)}", ""]
        out += [f"**Started** {started} · **{inc.events:,} errors** in {len(inc.groups)} group(s)"]
        out += [""]
        out += ["### What happened", ""]
        if cause.causes:
            origin = f" (in `{cause.origin}`)" if cause.origin else ""
            out.append(f"- Root cause: `{cause.causes[0]}`{origin}")
        if cause.raised_at:
            out.append(f"- Raised at `{cause.raised_at}` in `{cause.raised_in}()`")
        elif cause.logged_at:
            out.append(f"- Logged at `{cause.logged_at.id}`")
        trend = "new" if cause.is_new else (cause.trend or "")
        rate = f", rate {cause.rate_ratio:.1f}x" if cause.rate_ratio is not None else ""
        out.append(f"- {cause.count:,} errors ({trend}{rate})")
        for g in inc.effects:
            why = inc.reasons.get(g.id, "")
            out.append(f"- Then: {_md_group(g)} ({g.count:,}){' — ' + why if why else ''}")
        if inc.deploy:
            out.append(f"- Deploy: `{inc.deploy[0]}` at {inc.deploy[1]:%Y-%m-%d %H:%M} UTC")
        for c in inc.suspects:
            out.append(f"- Commit: `{c.short_sha}` {c.date:%Y-%m-%d} {c.author}: {c.summary}")
        h = cause.history
        if h is not None and getattr(h, "regression", False):
            fixes = ", ".join(f"`{f['sha']}` {f['summary']}" for f in h.fixed_by) or "unknown"
            gone = f", gone after `{h.resolved_release}`" if h.resolved_release else ""
            out.append(f"- Seen before{gone}; fixed then by {fixes}")
        trace = (cause.sample.get("ids") or {}).get("trace_id")
        if trace:
            out.append(f"- Example request: `logsite trace {trace}`")
        if d.strength == "unclear":
            out += ["", "### Possible causes (the evidence is unclear)", ""]
            out += ["Not enough evidence to name a likely cause. What logsite checked:", ""]
        else:
            out += ["", f"### Likely causes ({d.strength} evidence)", ""]
        for i, hyp in enumerate(d.hypotheses, 1):
            weight = "unlikely, " if hyp.score <= 0 else ""
            out += [f"**{i}. {hyp.title}** ({weight}score {hyp.score})", ""]
            out += [f"- For: {t}" for t in hyp.supporting]
            out += [f"- Against: {t}" for t in hyp.against]
            out += [f"- Unknown: {t}" for t in hyp.unknown]
            if hyp.next_check:
                out.append(f"- **Next check:** {hyp.next_check}")
            out.append("")
        for gap in d.unknown:
            out.append(f"_Also unknown: {gap}._")
        out += ["", "_Scores rank the explanations by the evidence above; they aren't "
                "probabilities. Check before acting._", ""]  # fmt: skip
    return "\n".join(out).rstrip() + "\n"
