"""Java and Go: indexing, lint, production record fields, stack traces and the error summary.

The fixtures' logs and traces are real output: a JDK 11 stack trace, and slog / zap /
zerolog records and a panic from Go 1.27 (paths rewritten to /srv/app)."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_java")
pytest.importorskip("tree_sitter_go")

from logsite.backends import FileBackend, TimeRange, parse_line  # noqa: E402
from logsite.cli import main  # noqa: E402
from logsite.errors import AppFiles, group_errors  # noqa: E402
from logsite.index import index_project, index_source  # noqa: E402
from logsite.lang import language_of  # noqa: E402
from logsite.lint import LintConfig, lint_source  # noqa: E402
from logsite.matching import Matcher  # noqa: E402
from logsite.models import LogRecord  # noqa: E402
from logsite.tracebacks import extract_error, join_multiline, parse_go_trace  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
JAVA = FIXTURES / "java"
GO = FIXTURES / "go"
JAVA_LINT = "src/main/java/com/acme/shop/Lint.java"
GO_LINT = "internal/checkout/lint.go"


def codes(path, root):
    source = (root / path).read_text()
    return [(d.line, d.code) for d in lint_source(source, path, LintConfig())]


def test_language_of():
    assert [language_of(p) for p in ("a.py", "A.java", "a.go", "x")] == [
        "python", "java", "go", "python",
    ]  # fmt: skip


# --- Java -----------------------------------------------------------------------------------


def test_java_index():
    sites = index_source((JAVA / JAVA_LINT).read_text(), JAVA_LINT)
    got = [(s.line, s.level, s.template, s.in_loop, s.in_except) for s in sites]
    assert got == [
        (10, "info", "charging order {} for {} cents", False, False),
        (14, "error", "payment failed for order {}: {}", False, True),
        (17, "debug", "card {} charged", False, False),  # "card " + cardNumber + " charged"
        (19, "info", "reserving item {}", True, False),
        (21, "warning", "legacy discount path used for order {}", False, False),
        (28, "info", "refund skipped for order {}", False, True),
        (30, "info", "refund failed for order {}", False, False),
        (31, "info", "refund requested", False, False),  # log.atInfo()...log("...")
        (35, "error", "settlement %s failed", False, False),  # String.format(...)
    ]
    fluent = sites[7]
    assert fluent.end_line == 34 and fluent.bound_fields == ("email", "order_id")
    assert all(s.library == "slf4j" for s in sites)


def test_java_lint():
    assert codes(JAVA_LINT, JAVA) == [
        (14, "LL003"),  # e.getMessage(): the stack trace is lost
        (17, "LL001"),  # string concatenation
        (17, "LL002"),  # cardNumber
        (19, "LL004"),  # in a loop
        (28, "LL007"),  # catch (Exception e) swallowed at info
        (30, "LL007"),  # "failed" at info
        (31, "LL002"),  # addKeyValue("email", ...)
        (35, "LL001"),  # String.format
    ]
    messages = {d.line: d.message for d in lint_source((JAVA / JAVA_LINT).read_text(), JAVA_LINT)}
    assert messages[14].startswith("e.getMessage() keeps the text but drops the stack trace")


@pytest.mark.parametrize(
    "code, expected",
    [
        ('log.error("failed", e);', []),  # the exception is attached
        ('log.error("failed {}", id, e);', []),
        ('log.error("failed");', ["LL003"]),
        ('LOGGER.warn("x {}", e.toString());', []),  # warn: no traceback rule
    ],
)
def test_java_catch_rules(code, expected):
    call = code.replace("log.", "LOGGER.")
    source = (
        "import org.slf4j.Logger;\nclass A {\n  static final Logger LOGGER = null;\n"
        f"  void f() {{ try {{ g(); }} catch (IOException e) {{ {call} throw e; }} }}\n}}\n"
    )
    assert [d.code for d in lint_source(source, "A.java")] == expected


def test_java_loggers_and_apis():
    source = """
import lombok.extern.slf4j.Slf4j;
import java.util.logging.Level;
@Slf4j
class A {
  private final java.util.logging.Logger jul = java.util.logging.Logger.getLogger("a");
  void f() {
    log.info("lombok {}", x);
    jul.log(Level.WARNING, "jul {0} warned", x);
    jul.fine("fine detail");
    LoggerFactory.getLogger(A.class).error("direct");
    other.info("not a logger");
    log.atError().setMessage("fluent message").log();
  }
}
"""
    got = [(s.line, s.level, s.template) for s in index_source(source, "A.java")]
    assert got == [
        (8, "info", "lombok {}"),
        (9, "warning", "jul {} warned"),  # {0} normalised to {}
        (10, "debug", "fine detail"),
        (11, "error", "direct"),
        (13, "error", "fluent message"),
    ]


# --- Go -------------------------------------------------------------------------------------


def test_go_index():
    sites = index_source((GO / GO_LINT).read_text(), GO_LINT)
    got = [(s.line, s.level, s.library, s.template, s.in_except) for s in sites]
    assert got == [
        (17, "info", "slog", "charging order", False),
        (19, "error", "slog", "payment failed", True),  # inside if err != nil
        (22, "debug", "slog", "card %s charged", False),  # fmt.Sprintf(...)
        (24, "info", "slog", "reserving item", False),
        (26, "warning", "zerolog", "legacy discount path used", False),
        (32, "info", "slog", "refund skipped", True),
        (34, "info", "slog", "refund failed for order {}", False),
        (35, "error", "zap", "settlement failed", False),  # s.zl is a *zap.Logger field
        (36, "error", "zerolog", "notify %s failed", False),  # Err(err)...Msgf
    ]
    assert sites[3].in_loop and sites[4].bound_fields == ("order_id",)
    assert sites[-1].end_line == 39
    # fmt.Println, zap.String(...) and zap.Error(err) aren't log calls
    assert len(sites) == 9


def test_go_lint():
    assert codes(GO_LINT, GO) == [
        (19, "LL003"),  # error logged without the error
        (22, "LL001"),  # fmt.Sprintf
        (22, "LL002"),  # cardNumber
        (24, "LL004"),
        (32, "LL007"),  # if err != nil { log at info } with no return
        (34, "LL001"),  # string concatenation
        (34, "LL007"),  # "failed" at info
        (36, "LL002"),  # Str("email", ...)
    ]


def test_go_libraries():
    source = """package x

import (
	"log"
	"github.com/sirupsen/logrus"
	"github.com/go-logr/logr"
	"k8s.io/klog/v2"
)

func f(l logr.Logger, err error) {
	log.Printf("std %s", x)
	logrus.WithField("user_id", u).WithError(err).Error("logrus failed")
	l.Error(err, "logr failed", "key", v)
	klog.InfoS("klog structured", "pod", p)
	t.Errorf("test failure")
}
"""
    got = [(s.line, s.level, s.library, s.template) for s in index_source(source, "x.go")]
    assert got == [
        (11, "info", "log", "std %s"),
        (12, "error", "logrus", "logrus failed"),
        (13, "error", "logr", "logr failed"),
        (14, "info", "klog", "klog structured"),
    ]
    assert [d.code for d in lint_source(source, "x.go")] == []  # errors are attached


# --- production records, stack traces, errors -------------------------------------------------


def test_record_locations_from_java_and_go_loggers():
    java = parse_line((JAVA / "app.log").read_text().splitlines()[0])
    assert (java.source_file, java.source_line) == ("com/acme/shop/Checkout.java", 47)
    assert java.timestamp is not None  # "@timestamp"
    slog, _, zap, zerolog = (parse_line(x) for x in (GO / "app.log").read_text().splitlines())
    assert (slog.source_file, slog.source_line) == ("/srv/app/internal/checkout/checkout.go", 20)
    assert (zap.source_file, zap.source_line) == ("checkout/checkout.go", 23)
    assert (zerolog.source_file, zerolog.source_line) == (
        "/srv/app/internal/checkout/checkout.go",
        24,
    )
    assert zap.timestamp == datetime.fromtimestamp(1792253323.179117, tz=timezone.utc)
    logrus = LogRecord(None, "x", {"file": "/srv/app/x.go:42"})
    assert (logrus.source_file, logrus.source_line) == ("/srv/app/x.go", 42)


def test_go_counts_from_real_records(tmp_path):
    sites = index_project(GO)
    records = [parse_line(x) for x in (GO / "app.log").read_text().splitlines()]
    matched = [Matcher(sites).match(r).id for r in records]
    assert matched == [
        "internal/checkout/checkout.go:20",
        "internal/checkout/checkout.go:22",
        "internal/checkout/checkout.go:23",
        "internal/checkout/checkout.go:24",
    ]


def test_matching_accepts_any_line_of_a_multi_line_call():
    source = (
        'package x\nimport "github.com/rs/zerolog/log"\nfunc f() {\n'
        '\tlog.Info().\n\t\tStr("k", v).\n\t\tMsg("m")\n}\n'
    )
    m = Matcher(index_source(source, "x.go"))

    def at(line):
        site = m.match_location("x.go", line)
        return site.id if site else None

    # the call spans lines 4-6; Go reports the line of .Msg(
    assert [at(n) for n in (3, 4, 6, 7)] == [None, "x.go:4", "x.go:4", None]


def test_java_stack_trace_error_groups():
    records = [parse_line(x) for x in (JAVA / "app.log").read_text().splitlines()]
    info = extract_error(records[0])
    assert [e.type for e in info.chain] == [
        "java.net.SocketTimeoutException",
        "java.lang.IllegalStateException",
    ]  # root cause first
    assert [(f.file, f.line, f.function) for f in info.root.frames] == [
        ("com/acme/shop/Checkout.java", 12, "charge"),
        ("com/acme/shop/Inventory.java", 5, "reserve"),
    ]
    app = AppFiles.scan(JAVA)
    groups = sorted(group_errors(records, [], app).values(), key=lambda g: g.title)
    assert [(g.title, g.raised_at, g.path) for g in groups] == [
        (
            "java.lang.IllegalStateException: could not reserve stock for order 0",
            "src/main/java/com/acme/shop/Inventory.java:5",
            ["Checkout.charge", "Inventory.reserve"],
        ),
        (
            "java.lang.NullPointerException",
            "src/main/java/com/acme/shop/Checkout.java:20",
            ["Checkout.main", "Checkout.price"],
        ),
    ]
    assert groups[0].causes[0].startswith("java.net.SocketTimeoutException: inventory read")


def test_go_panic_and_zap_stack():
    (exc,) = parse_go_trace((GO / "panic.txt").read_text())
    assert exc.title == "runtime error: index out of range [5] with length 3"
    assert [(f.file, f.line, f.function) for f in exc.frames] == [
        ("/srv/app/main.go", 24, "main"),
        ("/srv/app/internal/checkout/checkout.go", 36, "Price"),
    ]
    zap = parse_line((GO / "app.log").read_text().splitlines()[2])
    info = extract_error(zap)
    assert info.final.title == "inventory timed out" and info.final.logged_only
    assert info.final.frames[0].is_library  # runtime/proc.go in GOROOT
    (g,) = group_errors([zap], index_project(GO), AppFiles.scan(GO)).values()
    assert g.raised_at is None  # the stack shows where it was logged, not raised
    assert g.logged_at.id == "internal/checkout/checkout.go:23"


def test_plain_text_java_and_go_traces_are_joined(tmp_path):
    java_trace = json.loads((JAVA / "app.log").read_text().splitlines()[0])["stack_trace"]
    lines = [
        "2026-10-17 12:00:00.000 INFO  c.a.s.Checkout - started",
        "2026-10-17 12:00:01.000 ERROR c.a.s.Checkout - payment failed",
        *java_trace.splitlines(),
        "2026-10-17 12:00:02.000 INFO  c.a.s.Checkout - next",
        *(GO / "panic.txt").read_text().splitlines(),
    ]
    events = list(join_multiline(lines))
    assert len(events) == 4
    assert events[1].count("\n") == len(java_trace.splitlines())
    assert events[3].startswith("panic: runtime error") and "main.go:24" in events[3]
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    errors = FileBackend(tmp_path / "app.log").query_errors(TimeRange.last(24 * 365 * 10))
    assert [extract_error(r).final.type for r in errors] == [
        "java.lang.IllegalStateException",
        "runtime error",
    ]


def test_cli_on_a_go_project(tmp_path, monkeypatch, capsys):
    import shutil

    shutil.copytree(GO, tmp_path / "svc")
    (tmp_path / "svc" / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "app.log"\n')
    monkeypatch.chdir(tmp_path / "svc")
    assert main(["index"]) == 0
    out = capsys.readouterr().out
    assert "internal/checkout/checkout.go:24" in out and "lint.go:36" in out
    assert main(["counts", "--hours", str(24 * 365 * 10)]) == 0
    counts = capsys.readouterr().out
    assert re.search(r"1\s+internal/checkout/checkout.go:23", counts)


SPRING_CONTROLLER = """package com.example.orders;
import org.slf4j.Logger;
class OrderController {
    private static final Logger log = LoggerFactory.getLogger(OrderController.class);
    void submit(Order o) { try { service.submitOrder(o); } catch (OrderProcessingException e) {
        log.error("Failed to process order", e);
    } }
}
"""


def test_spring_boot_trace_in_plain_text(tmp_path):
    """A Spring Boot error with three exceptions, proxies, `... N common frames omitted`
    and a PostgreSQL root cause with indented Detail / SQLState lines."""
    trace = (JAVA / "spring-trace.txt").read_text()
    lines = [
        "2026-10-17 12:18:42.391 ERROR [order-service,traceId=8fa23bc1,spanId=91ac42] "
        "[http-nio-8080-exec-42] com.example.orders.OrderController : Failed to process order",
        *trace.splitlines(),
        "2026-10-17 12:18:42.398 WARN [order-service] c.e.o.OrderService : Retrying attempt=2",
        "2026-10-17 12:18:47.402 ERROR [order-service] com.example.orders.service.OrderService "
        ": Retry failed for order ORD-78291",
    ]
    events = list(join_multiline(lines))
    assert len(events) == 3 and events[0].endswith("... 41 common frames omitted")
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    records = FileBackend(tmp_path / "app.log").query_errors(TimeRange.last(24 * 365 * 10))
    info = extract_error(records[0])
    assert [e.type.rsplit(".", 1)[-1] for e in info.chain] == [
        "PSQLException", "QueryTimeoutException", "OrderProcessingException",
    ]  # fmt: skip
    assert info.root.message == "ERROR: canceling statement due to statement timeout"

    app = AppFiles(
        [
            "src/main/java/com/example/orders/OrderController.java",
            "src/main/java/com/example/orders/service/OrderService.java",
            "src/main/java/com/example/orders/repository/OrderRepository.java",
        ]
    )
    sites = index_source(SPRING_CONTROLLER, "src/main/java/com/example/orders/OrderController.java")
    groups = sorted(group_errors(records, sites, app).values(), key=lambda g: -len(g.causes))
    order, retry = groups
    assert order.causes == [
        "org.postgresql.util.PSQLException: ERROR: canceling statement due to statement timeout",
        "org.springframework.dao.QueryTimeoutException: JDBC statement timed out",
    ]
    # the root cause is all driver code: raised at the nearest frame of the project's own
    assert order.raised_at == "src/main/java/com/example/orders/repository/OrderRepository.java:87"
    assert order.origin == "org.postgresql"
    assert order.logged_at.id == "src/main/java/com/example/orders/OrderController.java:6"
    assert order.sample["ids"] == {"trace_id": "8fa23bc1", "span_id": "91ac42"}
    assert retry.title == "Retry failed for order ORD-78291"  # Spring's prefix is gone


def test_kafka_listener_failure_is_headlined_by_the_real_exception():
    """Spring Kafka wraps the listener's exception; the headline is what the listener threw,
    the wrapper is noted, and a JDK socket timeout is attributed to java.net, not sun.nio."""
    trace = (JAVA / "kafka-trace.txt").read_text()
    record = LogRecord(
        None,
        "2026-10-17 16:20:31.481 ERROR [shipping-consumer] [traceId=tr-91ac72] "
        "[container-3-C-1] o.s.k.l.KafkaMessageListenerContainer : Error handler threw\n" + trace,
    )
    app = AppFiles(
        [
            f"src/main/java/com/example/shipping/{name}.java"
            for name in ("ShipmentEventListener", "ShipmentProcessor", "CarrierService",
                         "CarrierApiClient")
        ]
    )  # fmt: skip
    (g,) = group_errors([record], [], app).values()
    assert g.title == "java.lang.IllegalStateException: Shipping label request failed"
    assert g.wrapped_in == ["ListenerExecutionFailedException"]
    assert g.causes[0] == "java.net.SocketTimeoutException: Read timed out"  # root first
    assert g.raised_at == "src/main/java/com/example/shipping/CarrierApiClient.java:58"
    assert g.origin == "java.net"
    assert g.path == [
        "ShipmentEventListener.consume",
        "ShipmentProcessor.process",
        "CarrierService.createLabel",
        "CarrierApiClient.requestLabel",
        "java.net",
    ]
    assert g.sample["ids"] == {"trace_id": "tr-91ac72"}
