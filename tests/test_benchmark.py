"""The diagnosis benchmark: incidents with a known cause, and what logsite must conclude.

Each case builds a small project (code, a log, git history when it matters), runs
``logsite investigate --format json`` on it and checks the top-ranked cause and how strong
the evidence is. The traps matter as much as the hits: a busy day isn't an incident, a
commit near the failing line isn't the cause without a deploy that ties it to the start, and
too little evidence must come out as "unclear". Add a case for every misdiagnosis found.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import git

from logsite.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "incidents"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def stamp(t: datetime) -> str:
    return f"{t:%Y-%m-%d %H:%M:%S}"


def java(root: Path, path: str, line: int, code: str) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    body = ["package x;", "", "class C {", *["  // ..." for _ in range(line - 4)], code, "}"]
    file.write_text("\n".join(body) + "\n")


def config(root: Path, **top: str) -> None:
    settings = "".join(f"{k} = {v}\n" for k, v in top.items())
    (root / "logsite.toml").write_text(
        settings + '[backend]\ntype = "file"\npath = "app.log"\n\n[history]\nenabled = false\n'
    )


# --- the cases -----------------------------------------------------------------------------------


def db_pool(root: Path) -> None:
    """A real-world shape: Postgres out of connections behind Spring wrappers, from an API
    and a Kafka consumer, with Hikari's pool-exhausted warning in the same request. One old
    commit near the code and no deploy: capacity, not the commit."""
    base = "src/main/java/com/example/orders"
    java(root, f"{base}/api/OrderController.java", 87, "    return orderService.createOrder(r);")
    java(root, f"{base}/service/OrderService.java", 214, "    return orderRepository.save(o);")
    java(root, f"{base}/repository/OrderRepository.java", 142, "    return jdbc.execute(sql);")
    java(root, f"{base}/events/OrderEventConsumer.java", 116, "    repo.save(toOrder(event));")
    (root / "app.log").write_text((FIXTURES / "db-pool.log").read_text())
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "orders service", date="2026-10-18T09:00:00+00:00")
    config(root, window_hours="87600")


SHIPPING = [
    "java.lang.IllegalStateException: Shipping label request failed",
    "\tat com.example.shipping.CarrierApiClient.requestLabel(CarrierApiClient.java:58)",
    "Caused by: java.net.SocketTimeoutException: Read timed out",
    "\tat java.base/sun.nio.ch.NioSocketImpl.timedRead(NioSocketImpl.java:288)",
]


def _shipping(root: Path, *, deploy: bool, touch: bool) -> None:
    path = "src/main/java/com/example/shipping/CarrierApiClient.java"
    java(root, path, 58, "    return rest.postForObject(url, request, LabelResponse.class);")
    git(root, "init", "-q")
    git(root, "add", ".")
    old = (NOW - timedelta(days=60)).isoformat()
    git(root, "commit", "-qm", "carrier client", date=old)
    git(root, "tag", "-a", "v1.3", "-m", "v1.3", date=old)
    start = NOW - timedelta(minutes=40)
    if touch:
        java(
            root,
            path,
            58,
            "    return rest.withReadTimeout(3).postForObject(url, request, L.class);",
        )
        when = (start - timedelta(minutes=30)).isoformat()
        git(root, "commit", "-qam", "Lower carrier read timeout to 3s", date=when)
    if deploy:
        git(
            root, "tag", "-a", "v1.4", "-m", "v1.4", date=(start - timedelta(minutes=4)).isoformat()
        )
    lines = []
    for t in (NOW - timedelta(hours=30) + timedelta(seconds=30 * i) for i in range(3000)):
        lines.append(f"{stamp(t)} INFO [shipping] c.e.s.Listener : shipment processed")
    for i in range(40):
        t = start + timedelta(seconds=30 * i)
        lines += [f"{stamp(t)} ERROR [shipping] [traceId=tr-{i:04d}] c.e.s.Listener : failed",
                  *SHIPPING]  # fmt: skip
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def regression_after_deploy(root: Path) -> None:
    """A deploy 4 minutes before, whose commit changed the line that raises: the change."""
    _shipping(root, deploy=True, touch=True)


def dependency_without_a_change(root: Path) -> None:
    """The same timeouts with no deploy and no commit near the code: the carrier."""
    _shipping(root, deploy=False, touch=False)


def busy_day(root: Path) -> None:
    """Three times the errors at three times the traffic: not an incident."""
    java(root, "src/Cart.java", 3, "    apply(code);")
    lines = []
    for start, info, errors in ((NOW - timedelta(hours=30), 1000, 20),
                                (NOW - timedelta(hours=6), 3000, 60)):  # fmt: skip
        lines += [f"{stamp(start + timedelta(seconds=i))} INFO [shop] c.e.Cart : ok"
                  for i in range(info)]  # fmt: skip
        for i in range(errors):
            lines += [f"{stamp(start + timedelta(seconds=i))} ERROR [shop] c.e.Cart : failed",
                      "java.lang.IllegalArgumentException: unknown discount code",
                      "\tat com.example.Cart.apply(Cart.java:3)"]  # fmt: skip
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)
    with open(root / "logsite.toml", "a") as f:
        f.write("\n[lint]\nspike_ratio = 2\nspike_min_count = 10\n")


def too_little_evidence(root: Path) -> None:
    """A new error with nothing to tie it to: no deploy, no history, no traffic before."""
    java(root, "src/Cart.java", 3, "    apply(code);")
    t = NOW - timedelta(minutes=30)
    lines = [f"{stamp(t)} ERROR [shop] c.e.Cart : failed",
             "java.lang.IllegalArgumentException: unknown discount code",
             "\tat com.example.Cart.apply(Cart.java:3)"]  # fmt: skip
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


@dataclass
class Case:
    build: object
    cause: str | None  # the top-ranked cause, None for "no incident"
    strength: tuple[str, ...] = ()  # acceptable evidence strengths
    mentions: tuple[str, ...] = ()  # in the report's evidence


CASES = {
    "db-pool": Case(db_pool, "capacity", ("strong",),
                    ("total=40, active=40, idle=0, waiting=126",)),
    "regression-after-deploy": Case(regression_after_deploy, "change", ("strong",),
                                    ("deployed 4 min before",)),
    "dependency-without-a-change": Case(dependency_without_a_change, "dependency",
                                        ("moderate", "weak", "strong")),
    "busy-day": Case(busy_day, None),
    "too-little-evidence": Case(too_little_evidence, "unclear"),
}  # fmt: skip


@pytest.mark.parametrize("name", CASES)
def test_benchmark(name, tmp_path, monkeypatch, capsys):
    case = CASES[name]
    case.build(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["investigate", "--format", "json"]) == 0
    incidents = json.loads(capsys.readouterr().out)["incidents"]
    if case.cause is None:
        assert incidents == [], f"{name}: expected no incident"
        return
    (inc,) = incidents
    d = inc["diagnosis"]
    if case.cause == "unclear":
        assert d["strength"] == "unclear", d
        return
    top = d["hypotheses"][0]
    assert top["cause"] == case.cause, f"{name}: ranked {[h['cause'] for h in d['hypotheses']]}"
    assert d["strength"] in case.strength, f"{name}: {d['strength']} evidence"
    evidence = " ".join(top["supporting"])
    for text in case.mentions:
        assert text in evidence, f"{name}: {text!r} not in the evidence"


# --- six unrelated failures in six services, within 22 minutes ----------------------------------

SIX = {  # service -> (top cause, what the evidence must name)
    "inventory-service": ("concurrency", "deadlock detected"),
    "order-service": ("dependency", "carrier.example.com"),
    "shipment-consumer": ("data", '"UNKNOWN"'),
    "cart-service": ("code", "com.example.cart.Cart.getDiscount()"),
    "reporting-service": ("capacity", "CsvExporter.export()"),
    "checkout-service": ("dependency", "inventoryService"),
}


def six_failures(root: Path) -> None:
    """Each failure is its own incident (no chaining by time across services), and each
    signature gets its own cause: a deadlock, a timeout, bad input, a null, memory, an open
    circuit breaker. A shape from real-world logs, renamed to a neutral domain."""
    text = (FIXTURES / "six-failures.log").read_text()
    import re

    for cls, file, line in set(
        re.findall(r"at (com\.example\.[\w.$]+)\.\w+\((\w+\.java):(\d+)\)", text)
    ):
        package = cls.rsplit(".", 1)[0].replace(".", "/")
        java(root, f"src/main/java/{package}/{file}", int(line), "    call();")
    (root / "app.log").write_text(text)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "services", date="2026-10-18T09:00:00+00:00")
    config(root, window_hours="87600")


def test_benchmark_six_failures(tmp_path, monkeypatch, capsys):
    six_failures(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["investigate", "--format", "json"]) == 0
    incidents = json.loads(capsys.readouterr().out)["incidents"]
    by_service = {next(iter(i["cause"]["services"])): i for i in incidents}
    assert set(by_service) == set(SIX), "one incident per service"
    assert all(len(i["effects"]) == 0 for s, i in by_service.items() if s != "reporting-service")
    for service, (cause, names) in SIX.items():
        d = by_service[service]["diagnosis"]
        top = d["hypotheses"][0]
        assert top["cause"] == cause, f"{service}: ranked {[h['cause'] for h in d['hypotheses']]}"
        assert d["strength"] in ("strong", "moderate"), f"{service}: {d['strength']}"
        said = " ".join([top["title"], *top["supporting"], top["next_check"]])
        assert names in said, f"{service}: {names!r} not in {said!r}"


# --- worst cases: cascades, red herrings, storms, crashes without timestamps, other runtimes -----


@pytest.mark.parametrize("name", list(__import__("benchmark_hard").CASES))
def test_benchmark_hard(name, tmp_path, monkeypatch, capsys):
    import benchmark_hard

    build, check = benchmark_hard.CASES[name]
    build(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--format", "json", "--top", "20"]) == 0
    errors = json.loads(capsys.readouterr().out)
    assert main(["investigate", "--format", "json"]) == 0
    inv = json.loads(capsys.readouterr().out)
    problems = check(inv, errors)
    assert not problems, f"{name}: " + "; ".join(problems)


def test_benchmark_alert_storm(tmp_path, monkeypatch):
    """One incident fires eight alerts (latency, errors, 5xx... in three services): one
    report, then one line per alert, and a full report again once the window is over."""
    from datetime import timedelta

    import benchmark_hard

    from logsite.alerts import AlertsConfig, Enricher
    from logsite.backends import TimeRange, from_config
    from logsite.cli import _with_incidents
    from logsite.config import load_config
    from logsite.errors import AppFiles, summarize_errors
    from logsite.index import index_project

    benchmark_hard.cascade(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    backend = from_config(cfg.backend)

    def summarize(start, end, labels):
        window = TimeRange.last(24)
        s = summarize_errors(backend, index_project(tmp_path), AppFiles.scan(tmp_path, set()),
                             window, window.previous(), cfg.lint)  # fmt: skip
        return _with_incidents(s, cfg, backend)

    sent = []
    clock = [benchmark_hard.NOW]
    enricher = Enricher(
        AlertsConfig(slack_webhook="https://hooks.slack.test/x"), summarize,
        send=lambda url, body: sent.append(body["text"]), now=lambda: clock[0],
    )  # fmt: skip

    def payload(*names):
        return {"alerts": [{"status": "firing", "labels": {"alertname": n}, "annotations": {},
                            "startsAt": (clock[0] - timedelta(minutes=5)).isoformat()}
                           for n in names]}  # fmt: skip

    enricher.handle(payload("inventory latency", "inventory errors", "checkout errors",
                            "checkout 5xx", "gateway 502s", "gateway latency"))  # fmt: skip
    clock[0] += timedelta(minutes=10)
    enricher.handle(payload("checkout SLO burn"))
    clock[0] += timedelta(minutes=5)
    enricher.handle(payload("gateway SLO burn"))
    assert len(sent) == 8
    full, *lines = sent
    assert "What failed" in full and "Likely cause" in full
    assert all(
        line.count("\n") == 0 and "same cause as inventory latency" in line for line in lines
    )
    assert lines[-1].endswith("8 alerts for this incident so far")
    clock[0] += timedelta(minutes=40)  # past dedupe_minutes: a full report again
    enricher.handle(payload("inventory errors"))
    assert "What failed" in sent[-1]
