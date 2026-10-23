"""TypeScript and JavaScript: log calls found with tree-sitter, matched, linted and grouped."""

import json

from logsite.backends.base import parse_line
from logsite.cli import main
from logsite.index import index_project, index_source
from logsite.lint import LintConfig, lint_source
from logsite.matching import Matcher

CHARGE_TS = """\
import pino from "pino";

const logger = pino();

export function charge(order: Order): void {
  logger.info({ orderId: order.id }, "charging order %s", order.id);
  try {
    gateway.charge(order);
  } catch (err) {
    logger.error({ err }, "payment failed");
    logger.error(`payment failed for ${order.id}: ${err.message}`);
  }
  for (const item of order.items) {
    logger.debug("reserving %s", item.sku);
  }
  logger.child({ orderId: order.id }).warn("slow charge");
  console.log(`done ${order.id}`);
}
"""

ORDERS_TS = """\
import { Injectable, Logger } from "@nestjs/common";

@Injectable()
export class OrderService {
  private readonly logger = new Logger(OrderService.name);

  refund(id: string): void {
    try {
      this.payments.refund(id);
    } catch (e) {
      this.logger.log("refund failed for " + id);
    }
  }
}
"""

SIGNIN_JS = """\
const winston = require("winston");
const logger = winston.createLogger({ level: "info" });

function signIn(req, userId) {
  logger.log("warn", "slow request %s", req.path);
  logger.info("user signed in", { userId });
  logger.error(new Error("boom"));
}
"""


def sites(source, path):
    return [(s.line, s.level, s.template, s.library) for s in index_source(source, path)]


def test_pino_calls():
    assert sites(CHARGE_TS, "src/charge.ts") == [
        (6, "info", "charging order %s", "pino"),
        (10, "error", "payment failed", "pino"),
        (11, "error", "payment failed for {}: {}", "pino"),
        (14, "debug", "reserving %s", "pino"),
        (16, "warning", "slow charge", "pino"),
        (17, "info", "done {}", "console"),
    ]
    (first,) = [s for s in index_source(CHARGE_TS, "src/charge.ts") if s.line == 6]
    assert first.bound_fields == ("orderId",)


def test_nestjs_and_winston_calls():
    assert sites(ORDERS_TS, "src/orders.service.ts") == [
        (11, "info", "refund failed for {}", "nestjs")
    ]
    assert sites(SIGNIN_JS, "src/signin.js") == [
        (5, "warning", "slow request %s", "winston"),
        (6, "info", "user signed in", "winston"),
        (7, "error", None, "winston"),
    ]


def test_lint():
    cfg = LintConfig()
    found = {(d.line, d.code) for d in lint_source(CHARGE_TS, "src/charge.ts", cfg)}
    assert (11, "LL003") in found  # err.message: the stack trace is lost
    assert (11, "LL001") in found  # a template literal, built even when the level is off
    assert (10, "LL003") not in found  # { err }: the error itself is logged
    assert (14, "LL004") in found  # debug inside a loop
    assert (17, "LL001") not in found  # template literals are idiomatic with console
    nest = {(d.line, d.code) for d in lint_source(ORDERS_TS, "src/orders.service.ts", cfg)}
    assert (11, "LL007") in nest  # "failed" at info level
    js = {(d.line, d.code) for d in lint_source(SIGNIN_JS, "src/signin.js", cfg)}
    assert (7, "LL005") not in js  # logger.error(new Error(...)): the error, not a message


def test_test_files_and_built_files_are_skipped(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "charge.ts").write_text(CHARGE_TS)
    (tmp_path / "src" / "charge.test.ts").write_text(CHARGE_TS)
    (tmp_path / "src" / "vendor.min.js").write_text(SIGNIN_JS)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "index.js").write_text(SIGNIN_JS)
    assert {s.path for s in index_project(tmp_path)} == {"src/charge.ts"}


def test_a_pino_line_matches_its_call():
    matcher = Matcher(index_source(CHARGE_TS, "src/charge.ts"))
    line = '{"level":30,"time":1792656000000,"msg":"charging order ORD-1","orderId":"ORD-1"}'
    site = matcher.match(parse_line(line))
    assert site is not None and site.line == 6


def test_pino_times_and_levels():
    from logsite.tracebacks import is_error, level_of

    warn = parse_line('{"level":40,"time":1792656000000,"msg":"slow"}')
    error = parse_line('{"level":50,"time":1792656000000,"msg":"payment failed"}')
    assert warn.timestamp.isoformat().startswith("2026-10-22T08:00:00")  # milliseconds
    assert (level_of(warn), is_error(warn)) == ("warn", False)
    assert (level_of(error), is_error(error)) == ("error", True)


def test_a_node_error_is_grouped_at_the_ts_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "charge.ts").write_text(CHARGE_TS)
    stack = "\n".join([
        "Error: card declined",
        "    at Gateway.charge (/srv/app/src/gateway.ts:3:11)",
        "    at charge (/srv/app/src/charge.ts:8:13)",
        "    at processTicksAndRejections (node:internal/process/task_queues:95:5)",
    ])  # fmt: skip
    lines = [
        json.dumps({"level": 50, "time": 1792656000000 + i, "msg": "payment failed",
                    "err": {"type": "Error", "message": "card declined", "stack": stack}})
        for i in range(3)
    ]  # fmt: skip
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    assert main(["errors", "--from", "app.log", "--format", "json", "--no-history"]) == 0
    (group,) = json.loads(capsys.readouterr().out)["groups"]
    assert group["count"] == 3 and group["title"] == "Error: card declined"
    assert group["raised_at"] == "src/charge.ts:8" and group["logged_at"] == "src/charge.ts:10"
