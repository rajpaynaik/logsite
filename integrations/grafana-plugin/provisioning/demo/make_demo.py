"""A demo project for trying the plugin: a slow database query that spreads through three
services (inventory -> checkout -> gateway) in the last 30 minutes, with the code the stack
traces point at. Run again to move the errors to "now".

    python make_demo.py /demo && logsite --config-dir /demo alert-server
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOW = datetime.now(timezone.utc).replace(microsecond=0)

CODE = {
    "src/main/java/com/example/inventory/StockRepository.java": {
        54: "        return jdbc.query(STOCK_FOR_SKU, mapper, sku);"
    },
    "src/main/java/com/example/inventory/StockController.java": {31: "        return repository.find(sku);"},
    "checkout/inventory_client.py": {18: "    resp = client.get(f'{INVENTORY}/stock/{sku}', timeout=2.0)"},
    "checkout/views.py": {40: "        stock = inventory_client.stock(item.sku)"},
    "gateway/proxy.go": {
        72: '\t\tlog.Error("upstream failed", zap.String("upstream", "checkout"), '
        'zap.Int("status", resp.StatusCode))'
    },
}

CONFIG = """\
[backend]
type = "file"
path = "app.log"

[history]
enabled = false
"""


def ts(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") + f".{t.microsecond // 1000:03d}"


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def write_code(root):
    for path, lines in CODE.items():
        f = root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        filler = "    // ..." if path.endswith((".java", ".go")) else "    pass"
        f.write_text("\n".join(lines.get(n, filler) for n in range(1, max(lines) + 2)) + "\n")


def events():
    out = []
    begin = NOW - timedelta(hours=26)
    for i in range(26 * 90):  # normal traffic, so the error rate has a baseline
        t = begin + timedelta(seconds=40 * i)
        out.append((t, [f"{ts(t)} INFO  [inventory-service] c.e.Http - request ok"]))
    start = NOW - timedelta(minutes=30)
    for i in range(60):
        t0 = start + timedelta(seconds=20 * i)
        tid = f"7f3a{i:04d}c2"
        thread = f"http-nio-8080-exec-{i % 9}"
        out.append((t0, [
            f"{ts(t0)} WARN  [inventory-service,{tid},a1] [{thread}] c.e.i.StockRepository - "
            f"slow query sku=bk-{i} took 8012ms",
        ]))
        t1 = t0 + timedelta(seconds=8)
        out.append((t1, [
            f"{ts(t1)} ERROR [inventory-service,{tid},a1] [{thread}] c.e.i.StockController - "
            f"stock lookup failed sku=bk-{i}",
            "org.springframework.dao.QueryTimeoutException: PreparedStatementCallback; SQL [select qty "
            "from stock where sku = ?]; ERROR: canceling statement due to statement timeout",
            "\tat org.springframework.jdbc.support.SQLStateSQLExceptionTranslator.doTranslate"
            "(SQLStateSQLExceptionTranslator.java:99)",
            "\tat org.springframework.jdbc.core.JdbcTemplate.query(JdbcTemplate.java:732)",
            "\tat com.example.inventory.StockRepository.find(StockRepository.java:54)",
            "\tat com.example.inventory.StockController.stock(StockController.java:31)",
            "Caused by: org.postgresql.util.PSQLException: ERROR: canceling statement due to statement timeout",
            "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(QueryExecutorImpl.java:2733)",
        ]))
        t2 = t0 + timedelta(seconds=2, milliseconds=5)
        out.append((t2, [json.dumps({
            "ts": iso(t2), "level": "error", "service": "checkout-service", "trace_id": tid,
            "logger": "checkout.views", "msg": "inventory call failed",
            "exc_info": "Traceback (most recent call last):\n"
            '  File "/srv/app/checkout/views.py", line 40, in place_order\n'
            "    stock = inventory_client.stock(item.sku)\n"
            '  File "/srv/app/checkout/inventory_client.py", line 18, in stock\n'
            "    resp = client.get(f'{INVENTORY}/stock/{sku}', timeout=2.0)\n"
            '  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1054, in get\n'
            '    return self.request("GET", url)\n'
            "httpx.ReadTimeout: timed out",
        })]))
        t3 = t0 + timedelta(seconds=2, milliseconds=40)
        out.append((t3, [json.dumps({
            "ts": iso(t3), "level": "error", "service": "gateway", "trace_id": tid,
            "caller": "gateway/proxy.go:72", "msg": "upstream failed", "upstream": "checkout", "status": 502,
        })]))
    out.sort(key=lambda e: e[0])
    return out


def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_code(root)
    (root / "logsite.toml").write_text(CONFIG)
    log = root / "app.log.new"
    log.write_text("\n".join(line for _, lines in events() for line in lines) + "\n")
    os.replace(log, root / "app.log")  # the server never reads a half-written log


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "demo"))
