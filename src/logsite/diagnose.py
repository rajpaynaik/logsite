"""Ranked explanations for an incident, each with the evidence for and against it.

An incident (``incidents.py``) says what failed, when, the deploy before it and the commits
around the code that raised it. This turns that into candidate causes:

- **a change**: the release or commit before it, when it touched the code that raised;
- **traffic**: more requests, the same error rate;
- **a dependency**: a call to another service or database failing (timeouts, refused
  connections), which a code change can make worse but often isn't the cause;
- **capacity**: a pool, a connection limit, memory or threads running out (``too many
  clients``, ``Connection is not available``, ``OutOfMemoryError``), usually with a warning
  in the same requests that says so (``total=40, active=40, waiting=126``).

Each gets the evidence that supports it, the evidence against it, what logsite can't see,
and the cheapest check that tells it apart from the others. Scores only rank them: they
aren't probabilities. Everything here is deterministic; nothing is guessed by a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from .errors import ErrorGroup, normalize
from .incidents import Incident
from .lint import LintConfig

CLOSE_DEPLOY = timedelta(minutes=30)
SAME_RATE = 1.3  # a rate within 30% of before: unchanged
BUSY = 1.5  # traffic up 50% or more
# root causes that are a call to something else going wrong
DEPENDENCY = re.compile(
    r"Timeout|TimedOut|timed out|Connect(ion)?(Error|Exception|Refused|Reset)|Refused|"
    r"Unavailable|UnknownHost|NameResolution|DNS|SSL|TLS|BrokenPipe|EOF|"
    r"OperationalError|PSQLException|SQLTransient|CannotGetJdbcConnection|"
    r"\b50[234]\b|Bad Gateway|Service Unavailable|ECONNRESET|ECONNREFUSED|ETIMEDOUT",
    re.I,
)


CAPACITY = re.compile(
    r"too many (clients|connections|open files)|max_connections|remaining connection slots|"
    r"Connection is not available|connection pool|pool (is )?(exhausted|full)|"
    r"Timeout waiting for (idle )?(object|connection)|Unable to acquire JDBC Connection|"
    r"active=(\d+).{0,20}idle=0|waiting=[1-9]|OutOfMemoryError|out of memory|"
    r"GC overhead limit|Too many open files|RejectedExecution|thread pool|queue (is )?full|"
    r"no space left on device|ENOSPC|disk (is )?full|quota exceeded|EMFILE|"
    r"ResourceExhausted|resource exhausted|rate limit|429",
    re.I,
)
IMPACT = re.compile(
    r"(?:method[=:]\s*\"?(?P<method>[A-Z]+)\"?.{0,40}?)?path[=:]\s*\"?(?P<path>/\S*?)\"?[\s,].{0,60}?"
    r"status[=:]\s*\"?(?P<status>[1-5]\d\d)\b(?:.{0,40}?duration(?:_ms)?[=:]\s*(?P<ms>\d+))?",
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


def _change(
    inc: Incident, cause: ErrorGroup, cfg: LintConfig, had_before: bool
) -> Hypothesis | None:
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
    close = bool(inc.deploy and start and start - inc.deploy[1] <= timedelta(hours=24))
    if inc.suspects and where and close:
        shas = ", ".join(c.short_sha for c in inc.suspects[:3])
        h.add(3, f"{shas}, in {inc.deploy[0]}, changed the lines around {where}, where it's raised")
    elif inc.suspects and where:
        shas = ", ".join(c.short_sha for c in inc.suspects[:3])
        h.add(1, f"{shas} changed the lines around {where} recently")
        h.unknown.append("no deploy ties that commit to when the errors started")
    elif inc.deploy and where:
        h.add(-1, f"no commit{' in ' + inc.deploy[0] if inc.deploy else ''} touched {where}")
    if cause.is_new and had_before:
        h.add(1, "the error wasn't there in the window before")
    elif cause.is_new:
        h.unknown.append("no logs in the window before: whether the error is new isn't known")
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


def _traffic(
    inc: Incident, cause: ErrorGroup, ratio: float | None, had_before: bool
) -> Hypothesis | None:
    if ratio is None:
        return None
    h = Hypothesis("traffic", "More traffic, not a new failure")
    if ratio >= BUSY:
        h.add(2, f"the service logged {ratio:.1f}x as much as in the window before")
    else:
        h.add(-1, f"traffic is about the same ({ratio:.1f}x)")
    rate = cause.rate_ratio
    if cause.is_new and had_before:
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


DNS = re.compile(
    r"UnknownHostException|Name or service not known|nodename nor servname|NXDOMAIN|"
    r"no such host|getaddrinfo ENOTFOUND|Temporary failure in name resolution|"
    r"NameResolutionError|could not translate host name",
    re.I,
)


def _dependency(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    text = " ".join([cause.title, *cause.causes, cause.exc_type or ""])
    if not DEPENDENCY.search(text):
        return None
    root = _short(cause.causes[0] if cause.causes else cause.exc_type) or "the error"
    caller = f"{cause.raised_in}()" if cause.raised_in else None
    dns = DNS.search(text)
    host = re.search(r"https?://([\w.-]+)", text) or (dns and re.search(
        r"UnknownHostException: ([\w.-]+)|no such host (?:for )?([\w.-]+)|ENOTFOUND ([\w.-]+)|"
        r"lookup ([\w.-]+)", text))  # fmt: skip
    if host:
        who = next(x for x in host.groups() if x) if host.lastindex else host.group(0)
    else:
        who = f"the service {caller} calls" if caller else "the service or database it calls"
    h = Hypothesis("dependency", f"A dependency failing: {root} from {who}")
    h.add(2, f"the root cause is {root}: a call to another service or database failed")
    if cause.origin:
        h.add(1, f"it was thrown inside {cause.origin}, not by your code")
    services = set().union(*(g.services.keys() for g in inc.groups))
    if len(services) > 1:
        h.add(1, f"{len(services)} services failed at the same time")
    deployed = inc.deploy and inc.started and inc.started - inc.deploy[1] <= timedelta(hours=24)
    if inc.suspects and cause.raised_at and deployed:
        h.add(-1, f"but {inc.deploy[0]}, just before, changed the code that makes the call")
    h.unknown.append(f"logsite reads logs only: the latency and status of {who} aren't in them")
    since = f"{inc.started:%H:%M} UTC" if inc.started else "the start"
    if dns:
        h.add(1, f"the name doesn't resolve: {dns.group(0)}")
        h.next_check = (
            f"check that {who} resolves where the service runs (`nslookup {who}` from a pod): "
            "a renamed or deleted service, a DNS outage, or a wrong namespace or search domain"
        )
        return h
    h.next_check = (
        f"check the latency and error rate of {who} since {since} (its dashboard or status "
        "page); "
        "if it's slow for every caller, the change isn't the cause"
    )
    return h


# --- failure signatures: errors whose type and message already say what kind of failure it is


def _text(g: ErrorGroup) -> str:
    return " ".join([g.title, *g.causes])


DEADLOCK = re.compile(r"deadlock|lock wait timeout|could not serialize access", re.I)
RACE = re.compile(
    r"concurrent map (read and map )?writes?|DATA RACE|ConcurrentModificationException|"
    r"database is locked|lock timeout|could not obtain lock",
    re.I,
)


def _deadlock(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    t = _text(cause)
    if not DEADLOCK.search(t):
        return _race(inc, cause, t)
    h = Hypothesis("concurrency", "Concurrency: two transactions locked each other (deadlock)")
    root = next((c for c in cause.causes if re.search(r"deadlock", c, re.I)), cause.title)
    h.add(4, f"the database says so: {root[:200]}")
    if cause.raised_at:
        h.add(1, f"it happened in {cause.raised_in or 'the update'}() at {cause.raised_at}")
    h.unknown.append("the other transaction's query is in the database's own log, not in these")
    h.next_check = (
        "find the other side in the database log at that time (Postgres logs both processes' "
        "queries on a deadlock); look for two code paths that update the same rows in a "
        "different order, and retry the transaction on a deadlock"
    )
    return h


def _race(inc: Incident, cause: ErrorGroup, t: str) -> Hypothesis | None:
    m = RACE.search(t)
    if not m:
        return None
    where = cause.raised_at or "the shared state"
    if "lock" in m.group(0).lower():
        h = Hypothesis("concurrency", "Concurrency: waiting on a lock another writer holds")
        h.add(4, f"the error says so: {m.group(0)}")
        h.next_check = ("find who holds the lock that long: a long transaction or a second "
                        "writer on the same rows or file; shorten it or retry")  # fmt: skip
        return h
    h = Hypothesis("concurrency", f"Concurrency: a data race at {where}")
    h.add(4, f"the runtime says so: {m.group(0)}")
    if cause.raised_at:
        h.add(1, f"in {cause.raised_in or 'the code'}() at {cause.raised_at}")
    h.next_check = (
        f"protect the shared state at {where} (a mutex, or a concurrent map), and run the "
        "tests with the race detector (`go test -race`) to find the other writer"
    )
    return h


CERT = re.compile(
    r"CertificateExpired|certificate (has )?expired|x509: certificate|PKIX path|"
    r"unable to find valid certification path|certificate verify failed|SSLCertVerification|"
    r"self[- ]signed certificate|hostname .* doesn't match|CERT_HAS_EXPIRED",
    re.I,
)
CREDENTIALS = re.compile(
    r"password authentication failed|Access denied for user|authentication failed for user|"
    r"invalid_client|invalid credentials|InvalidAccessKeyId|SignatureDoesNotMatch|"
    r"ExpiredToken|token (has )?expired|401 Unauthorized|403 Forbidden|AccessDenied|"
    r"NOAUTH|WRONGPASS|Login failed for user",
    re.I,
)
MISSING_CONFIG = re.compile(
    r"Could not resolve placeholder|NoSuchBeanDefinition|environment variable .* (not set|"
    r"missing)|(?-i:KeyError: '[A-Z][A-Z0-9_]{2,}')|missing required (config|setting|env)|"
    r"ConfigurationException|No such file or directory: '[^']*\.(ya?ml|json|toml|ini|pem|key)'",
    re.I,
)


def _config(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    t = _text(cause)
    host = re.search(r"https?://([\w.-]+)", t)
    target = host.group(1) if host else None
    if (m := CERT.search(t)) is not None:
        on = f" for {target}" if target else ""
        h = Hypothesis("config", f"Configuration: a TLS certificate problem{on}")
        expired = re.search(r"NotAfter: ([^\n]+?)(?:$|\s{2})", t)
        h.add(
            4,
            f"the error says so: {m.group(0)}"
            + (f" (valid until {expired.group(1)})" if expired else ""),
        )
        h.next_check = (
            f"check the certificate{on} (`openssl s_client -connect {target or 'host'}:443 "
            "</dev/null | openssl x509 -noout -dates -subject`); renew it, or fix the trust store "
            "the service uses"
        )
    elif (m := CREDENTIALS.search(t)) is not None:
        user = re.search(r'for user "?([\w.@-]+)"?', t)
        who = f' for "{user.group(1)}"' if user else ""
        h = Hypothesis("config", f"Configuration: credentials rejected{who}")
        h.add(4, f"the error says so: {m.group(0)}")
        h.next_check = (
            "check the secret the service uses against the account (rotated, expired, or a "
            "different one per environment); pods read secrets at start, so restart them "
            "after a change"
        )
    elif (m := MISSING_CONFIG.search(t)) is not None:
        h = Hypothesis("config", "Configuration: a setting the code needs is missing")
        h.add(4, f"the error says so: {m.group(0)}")
        h.next_check = (
            "check the service's configuration and environment against what the code expects"
        )
    else:
        return None
    if inc.deploy and inc.started and inc.started - inc.deploy[1] <= CLOSE_DEPLOY:
        h.add(1, f"{inc.deploy[0]} went out just before: it may have changed the configuration")
    h.unknown.append("the configuration itself isn't in the logs")
    return h


RECOVERED = timedelta(minutes=5)  # real failures stopped this long before the rejections


def _real_failures(name: str, breaker: ErrorGroup, groups) -> list[ErrorGroup]:
    """The groups of calls to ``name`` that failed for real (not rejected by the breaker)."""
    stem = re.sub(r"(service|svc|client|api)$", "", name.lower()).strip("-_.") or name.lower()
    out = []
    for g in groups:
        if g is breaker or BREAKER.search(_text(g)):
            continue
        text = " ".join([_text(g), g.sample.get("message") or "", g.sample.get("traceback") or ""])
        if stem and stem in text.lower():
            out.append(g)
    return out


BREAKER = re.compile(r"CircuitBreaker '[^']+' is OPEN|CallNotPermittedException|short-circuited|"
                     r"circuit (breaker )?(is )?open", re.I)  # fmt: skip


def _breaker_effect(inc: Incident, cause: ErrorGroup, others=()) -> Hypothesis | None:
    """A breaker's rejections among the incident's effects (the real failures are the
    cause): only worth saying when the breaker outlived the failures."""
    for g in inc.effects:
        if BREAKER.search(_text(g)):
            h = _breaker(inc, g, others)
            if h is not None and "recovered" in h.title:
                return h
    return None


def _breaker(inc: Incident, cause: ErrorGroup, others=()) -> Hypothesis | None:
    t = _text(cause)
    if not BREAKER.search(t):
        return None
    named = re.search(r"CircuitBreaker '([^']+)'|circuit breaker '?([\w.-]+)'? is open", t, re.I)
    name = (named.group(1) or named.group(2)) if named else "the dependency"
    h = Hypothesis("dependency", f"A dependency failing: {name} (its circuit breaker is open)")
    h.add(4, f"the circuit breaker for {name} is open: calls to it failed enough to stop trying")
    h.add(1, "this error is the protection working: the cause is the failures that opened it")
    real = _real_failures(name, cause, [*inc.groups, *others])
    rejected_until = cause.last_seen
    if real:
        n = sum(g.count for g in real)
        last = max(g.last_seen for g in real if g.last_seen)
        h.supporting.append(f"real failures calling {name}: {n:,} ({real[0].title[:90]}), "
                            f"rejected calls: {cause.count:,}")  # fmt: skip
        if rejected_until and last and rejected_until - last >= RECOVERED:
            gap = _minutes(rejected_until - last)
            h.title = f"{name} may have recovered: its circuit breaker is still rejecting calls"
            h.add(1, f"the real failures stopped {gap} before the rejections did: since "
                     f"{last:%H:%M} UTC only the breaker has been failing calls")  # fmt: skip
            h.next_check = (
                f"check whether {name} is healthy now, and the breaker's state and settings "
                "(half-open probes, wait duration): it may be failing calls that would succeed"
            )
            return h
    else:
        h.unknown.append(
            f"the failures that opened it aren't in this window: look earlier, or in {name}"
        )
    since = f"{inc.started:%H:%M} UTC" if inc.started else "it opened"
    h.next_check = (
        f"look for the errors calling {name} before {since} (timeouts, 5xx, refused "
        f"connections): `logsite errors` over an earlier window; then check {name} itself"
    )
    return h


_CODE_BUG = re.compile(
    r"NullPointerException|NoSuchElementException|ClassCastException|IndexOutOfBounds|"
    r"ArrayIndexOutOfBounds|IllegalStateException|UnsupportedOperationException|"
    r"ArithmeticException|AttributeError: 'NoneType'|TypeError: Cannot read propert|"
    r"invalid memory address or nil pointer|KeyError|ZeroDivisionError",
)
_BAD_DATA = re.compile(
    r"InvalidFormatException|MismatchedInputException|JsonParseException|"
    r"Cannot deserialize|deserializ|SerializationException|NumberFormatException|"
    r"DateTimeParseException|ValueError: invalid literal|JSONDecodeError|UnmarshalTypeError|"
    r"invalid character .* looking for",
    re.I,
)


def _code_or_data(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    t = _text(cause)
    # the root cause decides: an IllegalStateException wrapping a timeout isn't a bug
    root = cause.causes[0] if cause.causes else cause.title
    data = _BAD_DATA.search(root) or (_BAD_DATA.search(t) if cause.causes else None)
    bug = _CODE_BUG.search(root)
    if not data and not bug:
        return None
    where = cause.raised_at or "your code"
    if data:
        h = Hypothesis("data", "Bad input data: a message or request the code can't read")
        value = re.search(r'from String "([^"]*)"|value "([^"]*)"|\'([^\']+)\'', t)
        what = (value.group(1) or value.group(2) or value.group(3)) if value else None
        error = cause.causes[0] if cause.causes else cause.title
        h.add(3, f"the error is about reading data: {error[:200]}")
        if what:
            h.add(1, f'the value it couldn\'t read: "{what}"')
        place = re.search(r"partition [\w.-]+ at offset \d+|offset=\d+", t)
        h.next_check = (f"find who sent it ({place.group(0)})" if place else "find who sent it") + (
            "; check whether a producer or a client changed the format, and handle bad "
            "input (validate it, or send it to a dead-letter topic) instead of failing"
        )
    else:
        h = Hypothesis("code", f"A bug in the code: {_short(bug.group(0))} at {where}")
        h.add(3, f"{_short(cause.exc_type)} is a programming error, raised in your code")
        null = re.search(r'because (?:the return value of )?"?([\w.$]+\(\))"? is null', t)
        if null:
            h.add(1, f"the value that was null: {null.group(1)}")
            h.next_check = (
                f"find why {null.group(1)} returned null (missing data, a new code path, a "
                f"migration) and handle it at {where}"
            )
        else:
            h.next_check = f"reproduce it with an example request's input and fix it at {where}"
        h.unknown.append("whether the data or the code changed: an older record, or new code")
    return h


def _capacity(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    texts = [" ".join([g.title, *g.causes]) for g in inc.groups]
    in_error = next((t for t in texts if CAPACITY.search(t)), None)
    warnings = [r for r in inc.related if r.level == "warning" and CAPACITY.search(r.message)]
    if not in_error and not warnings:
        return None
    kind = _resource(" ".join([*texts, *(r.message for r in warnings)]))
    h = Hypothesis("capacity", f"Capacity: {kind} ran out")
    if in_error:
        root = next((c for g in inc.groups for c in g.causes if CAPACITY.search(c)), in_error)
        h.add(3, f"the error says so: {root[:160]}")
    for r in warnings[:2]:
        times = f" (in {r.count} of the requests looked at)" if r.count > 1 else ""
        h.add(3, f"a warning in the same request: {r.message[:200]}{times}")
    shared = shared_root(inc)
    if shared and len(shared[1]) > 1:
        h.add(1, f"the same root cause from {len(shared[1])} entry points: {', '.join(shared[1])}")
    if inc.deploy and inc.started and inc.started - inc.deploy[1] <= CLOSE_DEPLOY:
        h.unknown.append(f"{inc.deploy[0]} went out just before: it may add load or hold "
                         "connections longer")  # fmt: skip
    own = [f for f in cause.path if not f.startswith(("java.", "javax."))]
    if kind == "memory" and own and cause.origin in ("java.util", "java.io"):
        h.add(1, f"it ran out while {own[-1]}() was growing a buffer in {cause.origin}: the "
                 "whole result may be built in memory")  # fmt: skip
        h.next_check = (
            f"check how much {own[-1]}() holds at once: stream or page the output instead of "
            "building it in memory; meanwhile compare the heap size with the largest requests"
        )
        h.unknown.append(
            f"logsite reads logs only: how many {kind} are in use right now isn't in them"
        )
        return h
    h.unknown.append(f"logsite reads logs only: how many {kind} are in use right now isn't in them")
    h.next_check = NEXT_CHECK.get(kind, NEXT_CHECK["resources"])
    return h


NEXT_CHECK = {
    "database connections": (
        "check the database's connections (Postgres: `select count(*), state, application_name "
        "from pg_stat_activity group by 2, 3` against `max_connections`): which services hold "
        "them, long-running queries or transactions, and pool size × replicas"
    ),
    "connection pool": (
        "check the pool's metrics (active, idle, waiting) and what holds connections: slow "
        "queries, long transactions, a connection leak, or a pool too small for the load"
    ),
    "memory": "check the process memory and GC (heap size, recent growth); look for a leak or a "
    "load spike",
    "disk space": "check the volume (`df -h`, and what fills it: logs, temp files, uploads); free "
    "space or grow the volume, and clean up what writes without limit",
    "file descriptors": "check open files of the process (`ls /proc/<pid>/fd | wc -l` against "
    "`ulimit -n`): connections or files opened and never closed",
    "threads": "check the thread and queue metrics; slow downstream calls usually fill them",
    "resources": "check the resource the warning names (its usage and its limit) since the start",
}


def _resource(text: str) -> str:
    t = text.lower()
    if re.search(r"too many clients|max_connections|connection slots|too many connections", t):
        return "database connections"
    if re.search(r"connection is not available|connection pool|hikari|jdbc connection|pool", t):
        return "connection pool"
    if re.search(r"outofmemory|out of memory|gc overhead", t):
        return "memory"
    if re.search(r"no space left|enospc|disk (is )?full|quota exceeded", t):
        return "disk space"
    if re.search(r"too many open files|emfile", t):
        return "file descriptors"
    if re.search(r"thread|rejectedexecution|queue", t):
        return "threads"
    return "resources"


TENANT_SHARE = 0.8  # of the errors from one tenant
TENANT_MIN = 10


def _tenant(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    top = cause.top_tenant
    if top is None:
        return None
    tenant, share, seen = top
    n = cause.tenants[tenant]
    if share < TENANT_SHARE or n < TENANT_MIN:
        return None
    h = Hypothesis("tenant", f"One tenant: {tenant}")
    others = (
        f" ({seen - 1} other tenant(s) had {sum(cause.tenants.values()) - n:,})" if seen > 1 else ""
    )
    h.add(
        4 if share >= 0.9 else 3,
        f"{share:.0%} of these errors ({n:,}) come from tenant {tenant}{others}",
    )
    if seen == 1:
        h.unknown.append(
            "no other tenant had these errors: whether others use this path isn't known"
        )
    h.next_check = (
        f"look at what's different for {tenant}: its data (an unusual record or size), its "
        "configuration or plan, or a client sending something new; the rest of the platform "
        "isn't affected"
    )
    return h


def _cycle(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    """Two services each failing on a call to the other in the same requests: a wait cycle
    (A holds a request open waiting on B, which calls back into A and waits)."""
    from .incidents import _calls

    for a in inc.groups:
        for b in inc.groups:
            if a is b or not (_calls(a, b) and _calls(b, a)):
                continue
            if not (a.trace_ids & b.trace_ids):
                continue
            sa, sb = next(iter(a.services)), next(iter(b.services))
            h = Hypothesis("concurrency", f"A wait cycle: {sa} waits on {sb}, which waits on {sa}")
            shared = len(a.trace_ids & b.trace_ids)
            h.add(6, f"in the same requests ({shared:,}), {sa}'s errors are about calling {sb} "
                     f"and {sb}'s about calling {sa}")  # fmt: skip
            h.unknown.append(
                "which call started first (and so which side to break) isn't in the errors"
            )
            h.next_check = (
                f"follow one request (`logsite trace {sorted(a.trace_ids & b.trace_ids)[0]}`): the "
                f"call from {sb} back into {sa} while {sa} waits is the cycle; break it (async, "
                "a separate pool for callbacks, or no call back), and make the inner timeout "
                "shorter than the outer"
            )
            return h
    return None


def _orphans(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    if not inc.orphans:
        return None
    o = inc.orphans[0]
    n, looked = len(o["traces"]), o.get("looked") or len(o["traces"])
    who, caller = o["service"], o["caller"]
    h = Hypothesis("capacity", f"Orphaned work: {who} keeps working after {caller} gave up")
    h.add(4, f"in {n} of {looked} requests looked at, {who} kept logging up to "
             f"{o['seconds']:.0f}s after {caller} timed out (last: {o['example']})")  # fmt: skip
    h.add(1, "work nobody waits for still holds connections and threads, and retries add more")
    h.next_check = (
        f"pass the caller's deadline on and stop on cancellation in {o['service']} (a request "
        "context or gRPC deadline; stop when the client disconnects), and keep each "
        "downstream timeout shorter than its caller's"
    )
    return h


def _split_brain(inc: Incident, cause: ErrorGroup) -> Hypothesis | None:
    from .incidents import STALE

    t = " ".join(_text(g) for g in inc.groups)
    stale = STALE.search(t)
    if not stale:
        return None
    window = timedelta(minutes=15)
    claims = [c for c in inc.leadership if inc.started and abs(c["at"] - inc.started) <= window]
    nodes = sorted({c["node"] for c in claims})
    if len(nodes) < 2:
        h = Hypothesis("split-brain", "A stale leader: writes from a node that lost leadership")
        h.add(3, f"the errors say so: {stale.group(0)}")
        h.unknown.append("no leadership changes found in the logs around the start")
        h.next_check = ("find which node made the rejected writes and when it lost leadership; "
                        "check that writes carry and check a fencing token")  # fmt: skip
        return h
    h = Hypothesis("split-brain", f"Split-brain: {' and '.join(nodes[:3])} both acted as leader")
    h.add(3, f"the errors say so: {stale.group(0)}")
    for c in claims[:3]:
        h.add(1, f"{c['at']:%H:%M:%S} {c['node']}: {c['message'][:120]}")
    h.next_check = (
        "check the election: lease or session timeout against GC pauses and network blips, "
        "and whether the old leader kept writing after its lease; make writes carry a fencing "
        "token the storage checks, then reconcile the conflicting writes"
    )
    return h


def shared_root(inc: Incident) -> tuple[str, list[str]] | None:
    """``(root cause, entry points)`` when several groups of the incident share one root
    cause: one failure reached through different code paths (an API and a consumer)."""
    roots: dict[str, list[ErrorGroup]] = {}
    for g in inc.groups:
        if g.causes:
            roots.setdefault(normalize(g.causes[0]), []).append(g)
    if not roots:
        return None
    key, groups = max(roots.items(), key=lambda kv: sum(g.count for g in kv[1]))
    if len(groups) < 2:
        return None
    entries = []
    for g in groups:
        entry = g.path[0] if g.path else (g.logged_at.id if g.logged_at else None)
        if entry and entry not in entries:
            entries.append(entry)
    return groups[0].causes[0], entries


def impact(inc: Incident) -> list[str]:
    """What the failing requests did, from the lines that close them: ``POST /api/v1/orders
    → 500 after 30042 ms``."""
    out = []
    for r in inc.related:
        m = IMPACT.search(r.message)
        if m and m["status"].startswith("5"):
            text = f"{(m['method'] + ' ') if m['method'] else ''}{m['path']} → {m['status']}"
            if m["ms"]:
                text += f" after {int(m['ms']):,} ms"
            if text not in out:
                out.append(text)
    return out


def had_data_before(summary) -> bool:
    """Whether the window before had any logs: without them, "new" means "unknown"."""
    return bool(summary.previous_events or summary.previous_traffic)


def explain(inc: Incident, summary, cfg: LintConfig, backend=None, trace_field="trace_id"):
    """Collect the related lines (when a backend is given) and diagnose the incident."""
    from .incidents import add_leadership, add_related

    if backend is not None:
        add_related(inc, backend, summary.window, trace_field)
        add_leadership(inc, backend, summary.window)
    inc.diagnosis = diagnose(
        inc, cfg, summary.traffic_ratio, had_data_before(summary), others=summary.groups
    )
    return inc.diagnosis


def elsewhere(inc: Incident, others) -> list[str]:
    """Other services where the incident's root cause started at about the same time: a
    shared dependency, not this service's change."""
    cause = inc.cause
    root = _short(cause.causes[0] if cause.causes else cause.exc_type)
    if not root or not inc.started:
        return []
    mine = set().union(*(g.services.keys() for g in inc.groups))
    found = []
    for g in others:
        if g in inc.groups or not g.first_seen or not g.services:
            continue
        theirs = {_short(c) for c in g.causes} | {_short(g.exc_type)}
        if root not in theirs:
            continue
        if abs((g.first_seen - inc.started).total_seconds()) > 15 * 60:
            continue
        found += [s for s in g.services if s not in mine and s not in found]
    return found


def diagnose(
    inc: Incident,
    cfg: LintConfig,
    traffic_ratio: float | None = None,
    had_before: bool = True,
    others=(),
) -> Diagnosis:
    cause = inc.cause
    found = [
        h
        for h in (_change(inc, cause, cfg, had_before),
                  _traffic(inc, cause, traffic_ratio, had_before),
                  _breaker(inc, cause, others) or _dependency(inc, cause), _capacity(inc, cause),
                  _deadlock(inc, cause), _code_or_data(inc, cause), _config(inc, cause),
                  _tenant(inc, cause), _breaker_effect(inc, cause, others),
                  _cycle(inc, cause), _orphans(inc, cause), _split_brain(inc, cause))
        if h is not None
    ]  # fmt: skip
    spread = elsewhere(inc, others)
    if spread:
        root = _short(cause.causes[0] if cause.causes else cause.exc_type)
        where = ", ".join(spread[:4])
        for h in found:
            if h.key == "change":
                h.add(-4, f"the same {root} started at the same time in {where}, which this "
                          "change didn't touch")  # fmt: skip
            elif h.key in ("dependency", "config", "capacity"):
                h.add(3, f"{where} fail the same way ({root}) at the same time: something "
                         "they share, not one service's code")  # fmt: skip
    found.sort(key=lambda h: (-h.score, h.key))
    gaps = []
    if traffic_ratio is None:
        gaps.append("traffic before and after isn't known, so rates couldn't be compared")
    note = order_note(inc)
    if note:
        gaps.append(note)
    return Diagnosis(found, gaps)


CLOSE_START = timedelta(seconds=60)


def order_note(inc: Incident) -> str | None:
    """When the order of the timestamps shouldn't be read as the order of events: the cause
    was picked against it, or services started within seconds of each other (clocks and
    log delivery drift by that much)."""
    cause = inc.cause
    if not cause.first_seen:
        return None
    earlier = [g for g in inc.effects if g.first_seen and g.first_seen < cause.first_seen]
    if earlier:
        svcs = ", ".join(sorted({s for g in earlier for s in g.services})) or "other groups"
        return (
            f"{svcs} logged errors before the cause did: callers time out before the call "
            "they wait on fails, so the timestamps' order isn't the order of events"
        )
    mine = set(cause.services)
    close = [g for g in inc.effects if g.first_seen and g.services.keys() - mine
             and abs(g.first_seen - cause.first_seen) < CLOSE_START]  # fmt: skip
    if close:
        return ("services started failing within a minute of each other: clock skew and log "
                "delivery can reorder that much, so which came first is uncertain")  # fmt: skip
    return None


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
        shared = shared_root(inc)
        if shared:
            entries = ", ".join(f"`{e}`" for e in shared[1])
            n_groups = sum(1 for g in inc.groups if g.causes and g.causes[0] == shared[0])
            out.append(f"- One root cause behind {n_groups} groups, reached from {entries}")
        trend = "new" if cause.is_new else (cause.trend or "")
        rate = f", rate {cause.rate_ratio:.1f}x" if cause.rate_ratio is not None else ""
        out.append(f"- {cause.count:,} error(s) ({trend}{rate})")
        for text in impact(inc):
            out.append(f"- Impact: {text}")
        top = cause.top_tenant
        if top and top[2] > 0:
            out.append(f"- Tenants: {top[1]:.0%} from `{top[0]}` ({top[2]} tenant(s) affected)")
        note = order_note(inc)
        if note:
            out.append(f"- Order: {note}")
        for r in inc.related:
            if r.level == "warning":
                out.append(f"- Warning in the same requests: `{r.message}`")
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
