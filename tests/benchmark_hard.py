"""Hard, worst-case failure logs for the diagnosis benchmark (tests/test_benchmark.py): each
build() writes a small project (code at the lines the traces name, a log, git history where
it matters) and each check() says what logsite got wrong. Realistic shapes, neutral domains."""
# ruff: noqa: E501  (log lines are as long as they are)

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ts(t, ms=True):
    return t.strftime("%Y-%m-%d %H:%M:%S") + (f".{t.microsecond // 1000:03d}" if ms else "")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def code(root, path, lines):
    """A source file with ``lines`` = {lineno: text}."""
    f = root / path
    f.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for n in range(1, max(lines) + 2):
        out.append(
            lines.get(n, "    // ..." if path.endswith((".java", ".go", ".js")) else "    pass")
        )
    f.write_text("\n".join(out) + "\n")


def git(root, *args, date=None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Sam",
        "GIT_AUTHOR_EMAIL": "sam@example.com",
        "GIT_COMMITTER_NAME": "Sam",
        "GIT_COMMITTER_EMAIL": "sam@example.com",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date.isoformat()
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def commit_all(root, msg="services", when=None):
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", msg, date=when or NOW - timedelta(days=30))


def config(root, window_hours=None, extra=""):
    top = f"window_hours = {window_hours}\n" if window_hours else ""
    (root / "logsite.toml").write_text(
        top + '[backend]\ntype = "file"\npath = "app.log"\n\n[history]\nenabled = false\n' + extra
    )


def traffic(service, hours=26, every=60, fmt="text"):
    lines = []
    t = NOW - timedelta(hours=hours)
    while t < NOW:
        if fmt == "json":
            lines.append(
                json.dumps({"ts": iso(t), "level": "info", "service": service, "msg": "request ok"})
            )
        else:
            lines.append(f"{ts(t)} INFO [{service}] c.e.Http - request ok")
        t += timedelta(seconds=every)
    return lines


def write_log(root, events):
    """events: (time, [lines])"""
    events.sort(key=lambda e: e[0])
    (root / "app.log").write_text("\n".join(line for _, ls in events for line in ls) + "\n")


# --- 1. a cascade across three services ----------------------------------------------------------


def cascade(root):
    code(
        root,
        "src/main/java/com/example/inventory/StockRepository.java",
        {54: "        return jdbc.query(STOCK_FOR_SKU, mapper, sku);"},
    )
    code(
        root,
        "src/main/java/com/example/inventory/StockController.java",
        {31: "        return repository.find(sku);"},
    )
    code(
        root,
        "checkout/inventory_client.py",
        {18: "    resp = client.get(f'{INVENTORY}/stock/{sku}', timeout=2.0)"},
    )
    code(root, "checkout/views.py", {40: "        stock = inventory_client.stock(item.sku)"})
    code(
        root,
        "gateway/proxy.go",
        {
            72: '\t\tlog.Error("upstream failed", zap.String("upstream", "checkout"), zap.Int("status", resp.StatusCode))'
        },
    )
    commit_all(root)
    start = NOW - timedelta(minutes=30)
    events = []
    for i, line in enumerate(traffic("inventory-service", every=40)):
        events.append((NOW - timedelta(hours=26) + timedelta(seconds=40 * i), [line]))
    for i in range(60):
        t0 = start + timedelta(seconds=20 * i)
        tid = f"7f3a{i:04d}c2"
        events.append(
            (
                t0,
                [
                    f"{ts(t0)} WARN  [inventory-service,{tid},a1] [http-nio-8080-exec-{i % 9}] c.e.i.StockRepository - slow query sku=bk-{i} took 8012ms",
                ],
            )
        )
        t1 = t0 + timedelta(seconds=8)
        events.append(
            (
                t1,
                [
                    f"{ts(t1)} ERROR [inventory-service,{tid},a1] [http-nio-8080-exec-{i % 9}] c.e.i.StockController - stock lookup failed sku=bk-{i}",
                    "org.springframework.dao.QueryTimeoutException: PreparedStatementCallback; SQL [select qty from stock where sku = ?]; ERROR: canceling statement due to statement timeout",
                    "\tat org.springframework.jdbc.support.SQLStateSQLExceptionTranslator.doTranslate(SQLStateSQLExceptionTranslator.java:99)",
                    "\tat org.springframework.jdbc.core.JdbcTemplate.query(JdbcTemplate.java:732)",
                    "\tat com.example.inventory.StockRepository.find(StockRepository.java:54)",
                    "\tat com.example.inventory.StockController.stock(StockController.java:31)",
                    "Caused by: org.postgresql.util.PSQLException: ERROR: canceling statement due to statement timeout",
                    "\tat org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(QueryExecutorImpl.java:2733)",
                ],
            )
        )
        t2 = t0 + timedelta(seconds=2, milliseconds=5)
        events.append(
            (
                t2,
                [
                    json.dumps(
                        {
                            "ts": iso(t2),
                            "level": "error",
                            "service": "checkout-service",
                            "trace_id": tid,
                            "logger": "checkout.views",
                            "msg": "inventory call failed",
                            "exc_info": "Traceback (most recent call last):\n"
                            '  File "/srv/app/checkout/views.py", line 40, in place_order\n'
                            "    stock = inventory_client.stock(item.sku)\n"
                            '  File "/srv/app/checkout/inventory_client.py", line 18, in stock\n'
                            "    resp = client.get(f'{INVENTORY}/stock/{sku}', timeout=2.0)\n"
                            '  File "/usr/local/lib/python3.12/site-packages/httpx/_client.py", line 1054, in get\n'
                            '    return self.request("GET", url)\n'
                            "httpx.ReadTimeout: timed out",
                        }
                    )
                ],
            )
        )
        t3 = t0 + timedelta(seconds=2, milliseconds=40)
        events.append(
            (
                t3,
                [
                    json.dumps(
                        {
                            "ts": iso(t3),
                            "level": "error",
                            "service": "gateway",
                            "trace_id": tid,
                            "caller": "gateway/proxy.go:72",
                            "msg": "upstream failed",
                            "upstream": "checkout",
                            "status": 502,
                        }
                    )
                ],
            )
        )
    write_log(root, events)
    config(root)


def check_cascade(inv, errors):
    p = []
    incs = inv["incidents"]
    if len(incs) != 1:
        p.append(f"expected 1 incident, got {len(incs)}")
    if not incs:
        return p
    inc = incs[0]
    svc = list(inc["cause"]["services"])
    if svc != ["inventory-service"]:
        p.append(
            f"cause should be inventory-service (the initiating failure), got {svc}: {inc['cause']['title'][:80]}"
        )
    eff = {s for e in inc["effects"] for s in e["services"]}
    if not {"checkout-service", "gateway"} <= eff:
        p.append(f"effects should include checkout-service and gateway, got {eff}")
    top = inc["diagnosis"]["hypotheses"][0]["cause"] if inc["diagnosis"]["hypotheses"] else None
    if top not in ("dependency", "capacity"):
        p.append(f"top cause should be the database (dependency/capacity), got {top}")
    if not any("slow query" in r["message"] for r in inc["related"]):
        p.append("the slow-query warning should be in the evidence")
    return p


# --- 2. a deploy that is a red herring -------------------------------------------------------------


def red_herring(root):
    path = "src/main/java/com/example/search/CatalogClient.java"
    code(
        root,
        path,
        {44: '        return rest.getForObject(catalogUrl + "/items/" + id, Item.class);'},
    )
    commit_all(root, when=NOW - timedelta(days=20))
    git(root, "tag", "-a", "v2.0", "-m", "v2.0", date=NOW - timedelta(days=20))
    start = NOW - timedelta(minutes=25)
    code(
        root,
        path,
        {44: '        return rest.getForObject(catalogUrl + "/v2/items/" + id, Item.class);'},
    )
    git(root, "commit", "-qam", "Use catalog v2 API", date=start - timedelta(minutes=40))
    git(root, "tag", "-a", "v2.1", "-m", "v2.1", date=start - timedelta(minutes=3))
    events = []
    for svc in ("search-service", "inventory-service", "cart-service"):
        for i, line in enumerate(traffic(svc, every=90)):
            events.append((NOW - timedelta(hours=26) + timedelta(seconds=90 * i), [line]))
    trace = [
        'org.springframework.web.client.ResourceAccessException: I/O error on GET request for "http://catalog-db.internal:8080/items/42": catalog-db.internal',
        "\tat org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:895)",
        "\tat com.example.search.CatalogClient.item(CatalogClient.java:44)",
        "Caused by: java.net.UnknownHostException: catalog-db.internal",
        "\tat java.base/java.net.InetAddress$CachedLookup.get(InetAddress.java:988)",
    ]
    other = [
        "java.net.UnknownHostException: catalog-db.internal: Name or service not known",
        "\tat java.base/java.net.Inet6AddressImpl.lookupAllHostAddr(Native Method)",
        "\tat java.base/java.net.InetAddress.getAllByName0(InetAddress.java:1529)",
    ]
    for i in range(40):
        t = start + timedelta(seconds=30 * i)
        events.append(
            (
                t,
                [
                    f"{ts(t)} ERROR [search-service] c.e.s.CatalogClient - catalog lookup failed",
                    *trace,
                ],
            )
        )
        for svc in ("inventory-service", "cart-service"):
            t2 = t + timedelta(seconds=3)
            events.append(
                (t2, [f"{ts(t2)} ERROR [{svc}] c.e.CatalogGateway - catalog lookup failed", *other])
            )
    write_log(root, events)
    config(root)


def check_red_herring(inv, errors):
    p = []
    for inc in inv["incidents"]:
        if "search-service" in inc["cause"]["services"]:
            hyps = inc["diagnosis"]["hypotheses"]
            top = hyps[0]["cause"] if hyps else None
            if top != "dependency":
                p.append(
                    f"search-service: top should be dependency (DNS for catalog-db.internal fails in services that weren't deployed), got {[(h['cause'], h['score']) for h in hyps]}"
                )
            change = next((h for h in hyps if h["cause"] == "change"), None)
            if change and not any(
                "inventory-service" in t or "other services" in t for t in change["against"]
            ):
                p.append(
                    "the change should have evidence against it: the same error in services that weren't deployed"
                )
            break
    else:
        p.append("no incident for search-service")
    return p


# --- 3. a log storm with a needle --------------------------------------------------------------------


def storm(root):
    code(
        root,
        "app/feed.py",
        {
            12: "    return cache.get(key)",
            30: "    total = sum(i.price for i in cart.items) / len(cart.items)",
        },
    )
    commit_all(root)
    events = []
    t = NOW - timedelta(hours=47)
    i = 0
    noise = (
        'Traceback (most recent call last):\n  File "/srv/app/app/feed.py", line 12, in item\n'
        "    return cache.get(key)\nKeyError: 'feed:{n}'"
    )
    while t < NOW:
        events.append(
            (
                t,
                [
                    json.dumps(
                        {
                            "ts": iso(t),
                            "level": "error",
                            "service": "feed",
                            "msg": f"cache miss for feed:{i}",
                            "exc_info": noise.format(n=i),
                        }
                    )
                ],
            )
        )
        t += timedelta(seconds=8)
        i += 1
    for k in range(3):
        t = NOW - timedelta(minutes=20 - k)
        events.append(
            (
                t,
                [
                    json.dumps(
                        {
                            "ts": iso(t),
                            "level": "error",
                            "service": "feed",
                            "msg": "average failed",
                            "exc_info": 'Traceback (most recent call last):\n  File "/srv/app/app/feed.py", line 30, in average\n'
                            "    total = sum(i.price for i in cart.items) / len(cart.items)\n"
                            "ZeroDivisionError: division by zero",
                        }
                    )
                ],
            )
        )
    write_log(root, events)
    config(root, extra="\n[lint]\nspike_ratio = 3\n")


def check_storm(inv, errors):
    p = []
    groups = errors["groups"]
    if not groups or "ZeroDivisionError" not in groups[0]["title"]:
        p.append(
            f"the new ZeroDivisionError should rank first, got {[g['title'][:50] for g in groups[:3]]}"
        )
    if len(groups) > 3:
        p.append(f"20k KeyErrors with different keys should be one group, got {len(groups)} groups")
    if not errors.get("truncated"):
        p.append(
            "21k errors with a 5000-record limit: the summary must say its counts are a lower bound"
        )
    return p


# --- 4. interleaved stack traces from two threads -----------------------------------------------------


def interleaved(root):
    code(root, "src/main/java/com/example/a/Exporter.java", {20: "        writer.write(row);"})
    code(root, "src/main/java/com/example/b/Mailer.java", {33: "        smtp.send(message);"})
    commit_all(root)
    t = NOW - timedelta(minutes=10)
    lines = [
        f"{ts(t)} ERROR [export-service] [pool-1-thread-1] c.e.a.Exporter - export failed",
        f"{ts(t)} ERROR [export-service] [pool-1-thread-2] c.e.b.Mailer - mail failed",
        "java.io.IOException: Broken pipe",
        "javax.mail.MessagingException: Could not connect to SMTP host: smtp.example.com, port: 587",
        "\tat com.example.a.Exporter.export(Exporter.java:20)",
        "\tat com.example.b.Mailer.send(Mailer.java:33)",
        "Caused by: java.net.ConnectException: Connection refused",
    ]
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_interleaved(inv, errors):
    p = []
    titles = [g["title"] for g in errors["groups"]]
    if not titles:
        p.append("nothing found at all")
    for g in errors["groups"]:
        if (
            "Broken pipe" in g["title"]
            and g.get("raised_at", "")
            and "Mailer" in (g.get("raised_at") or "")
        ):
            p.append("mixed up: the IOException attributed to Mailer")
        if "Broken pipe" in g["title"] and "MessagingException" in g["title"]:
            p.append(f"two exceptions merged into one title: {g['title'][:90]}")
    return p  # mostly: don't crash, don't invent; this is a known-hard shape


# --- 5. Python chained exceptions ---------------------------------------------------------------------


def py_chain(root):
    code(
        root,
        "shop/orders.py",
        {
            25: "        row = db.fetch_order(order_id)",
            31: "        raise OrderLookupError(order_id) from exc",
        },
    )
    code(root, "shop/db.py", {9: "    return conn.execute(SQL, (order_id,)).fetchone()"})
    commit_all(root)
    t = NOW - timedelta(minutes=5)
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/srv/shop/shop/orders.py", line 25, in get_order\n'
        "    row = db.fetch_order(order_id)\n"
        '  File "/srv/shop/shop/db.py", line 9, in fetch_order\n'
        "    return conn.execute(SQL, (order_id,)).fetchone()\n"
        "sqlite3.OperationalError: database is locked\n"
        "\n"
        "The above exception was the direct cause of the following exception:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/shop/shop/orders.py", line 31, in get_order\n'
        "    raise OrderLookupError(order_id) from exc\n"
        "shop.orders.OrderLookupError: order ord-77"
    )
    lines = []
    for i in range(5):
        lines.append(
            f"{ts(t + timedelta(seconds=i))},123 ERROR shop.orders: lookup failed for order ord-77"
        )
        lines.append(tb)
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_py_chain(inv, errors):
    p = []
    g = errors["groups"][0] if errors["groups"] else None
    if not g:
        return ["nothing found"]
    if "OrderLookupError" not in g["title"]:
        p.append(f"title should be the OrderLookupError, got {g['title'][:80]}")
    if not g["root_cause"] or "database is locked" not in g["root_cause"]:
        p.append(f"root cause should be 'database is locked', got {g['root_cause']}")
    if g["raised_at"] != "shop/db.py:9":
        p.append(
            f"raised at should be shop/db.py:9 (where the root cause was raised), got {g['raised_at']}"
        )
    top = (
        inv["incidents"][0]["diagnosis"]["hypotheses"][0]["cause"]
        if inv["incidents"] and inv["incidents"][0]["diagnosis"]["hypotheses"]
        else None
    )
    if top not in ("concurrency", "capacity", "dependency"):
        p.append(f"'database is locked' is lock contention: expected concurrency, got {top}")
    return p


# --- 6. Go fatal error (not a panic) --------------------------------------------------------------------


def go_fatal(root):
    code(root, "cache/cache.go", {27: "\tc.items[key] = value"})
    code(root, "server.go", {88: "\tcache.Set(r.URL.Path, body)"})
    commit_all(root)
    t = NOW - timedelta(minutes=3)
    lines = [
        json.dumps(
            {
                "ts": iso(t - timedelta(seconds=1)),
                "level": "info",
                "service": "api",
                "msg": "serving",
            }
        ),
        "fatal error: concurrent map writes",
        "",
        "goroutine 41 [running]:",
        "main.(*Cache).Set(0xc000120000, {0xc0001a2000, 0x9}, {0xc0001b4000, 0x200, 0x200})",
        "\t/app/cache/cache.go:27 +0x65",
        "main.handler({0x7f8a1c0, 0xc0001c0000}, 0xc0001d0000)",
        "\t/app/server.go:88 +0x1a5",
        "net/http.HandlerFunc.ServeHTTP(0x0?, {0x7f8a1c0?, 0xc0001c0000?}, 0x0?)",
        "\t/usr/local/go/src/net/http/server.go:2166 +0x29",
        "",
        "goroutine 1 [IO wait]:",
        "internal/poll.runtime_pollWait(0x7f2c, 0x72)",
        "\t/usr/local/go/src/runtime/netpoll.go:345 +0x85",
    ]
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_go_fatal(inv, errors):
    p = []
    g = errors["groups"][0] if errors["groups"] else None
    if not g:
        return ["the fatal error wasn't found at all (no timestamp, no level: a Go runtime crash)"]
    if "concurrent map writes" not in g["title"]:
        p.append(f"title should say concurrent map writes, got {g['title'][:60]}")
    if g["raised_at"] != "cache/cache.go:27":
        p.append(f"raised at should be cache/cache.go:27, got {g['raised_at']}")
    hyps = inv["incidents"][0]["diagnosis"]["hypotheses"] if inv["incidents"] else []
    if not hyps or hyps[0]["cause"] != "concurrency":
        p.append(f"expected concurrency (a data race), got {[h['cause'] for h in hyps]}")
    return p


# --- 7. truncated traces ----------------------------------------------------------------------------------


def truncated(root):
    code(
        root,
        "src/main/java/com/example/billing/InvoiceService.java",
        {140: "        return pdf.render(invoice);"},
    )
    code(
        root,
        "src/main/java/com/example/billing/InvoiceController.java",
        {52: "        return service.render(id);"},
    )
    commit_all(root)
    t = NOW - timedelta(minutes=8)
    lines = []
    for i in range(6):
        tt = t + timedelta(seconds=10 * i)
        lines += [
            f"{ts(tt)} ERROR [billing] [http-nio-8080-exec-{i}] c.e.b.InvoiceController - render failed invoiceId=inv-{i}",
            "org.springframework.web.util.NestedServletException: Request processing failed; nested exception is java.lang.IllegalStateException: renderer pool closed",
            "\tat org.springframework.web.servlet.FrameworkServlet.processRequest(FrameworkServlet.java:1014)",
            "\t... 87 more",
            "Caused by: java.lang.IllegalStateException: renderer pool closed",
            "\tat com.example.billing.InvoiceService.render(InvoiceService.java:140)",
            "\tat com.example.billing.InvoiceController.render(InvoiceController.java:52)",
            "\tSuppressed: java.io.IOException: stream closed",
            "\t\tat com.example.billing.InvoiceService.render(InvoiceService.java:141)",
            "\t... 42 common frames omitted",
            "Caused by: java.util.concurrent.RejectedExecutionException: Task rejected from java.util.concurrent.ThreadPoolExecutor@6d3a[Terminated, pool size = 0, active threads = 0, queued tasks = 0, completed tasks = 1882]",
            "\tat java.base/java.util.concurrent.ThreadPoolExecutor$AbortPolicy.rejectedExecution(ThreadPoolExecutor.java:2081)",
            "\tat java.base/java.util.concurrent.ThreadPoolExe",  # cut by the collector's line limit
        ]
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_truncated(inv, errors):
    p = []
    if len(errors["groups"]) != 1:
        p.append(f"6 identical failures should be 1 group, got {len(errors['groups'])}")
    g = errors["groups"][0] if errors["groups"] else None
    if g and (not g["root_cause"] or "RejectedExecutionException" not in g["root_cause"]):
        p.append(f"root cause should be the RejectedExecutionException, got {g['root_cause']}")
    if g and g["raised_at"] != "src/main/java/com/example/billing/InvoiceService.java:140":
        p.append(f"raised at should be InvoiceService.java:140, got {g['raised_at']}")
    hyps = inv["incidents"][0]["diagnosis"]["hypotheses"] if inv["incidents"] else []
    if not hyps or hyps[0]["cause"] != "capacity":
        p.append(
            f"a terminated executor rejecting tasks: expected capacity (threads), got {[h['cause'] for h in hyps]}"
        )
    return p


# --- 8. a retry storm -------------------------------------------------------------------------------------


def retries(root):
    code(
        root,
        "src/main/java/com/example/notify/SmsClient.java",
        {61: "        return http.post(gatewayUrl, body);"},
    )
    commit_all(root)
    t = NOW - timedelta(minutes=15)
    lines = []
    exc = [
        "java.net.ConnectException: Connection refused",
        "\tat java.base/sun.nio.ch.Net.connect0(Native Method)",
        "\tat com.example.notify.SmsClient.send(SmsClient.java:61)",
    ]
    for m in range(20):
        base = t + timedelta(seconds=30 * m)
        for a in range(1, 5):
            tt = base + timedelta(seconds=a)
            lines += [
                f"{ts(tt)} WARN  [notify] [worker-{m}] c.e.n.SmsClient - send failed, retry attempt={a} of 4 messageId=msg-{m}",
                *exc,
            ]
        tt = base + timedelta(seconds=6)
        lines += [
            f"{ts(tt)} ERROR [notify] [worker-{m}] c.e.n.SmsClient - giving up after 4 attempts messageId=msg-{m}",
            *exc,
        ]
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_retries(inv, errors):
    p = []
    n = len(errors["groups"])
    if n > 2:
        p.append(
            f"retries of one failure should be at most 2 groups (retrying, gave up), got {n}: {[g['title'][:40] for g in errors['groups']]}"
        )
    hyps = inv["incidents"][0]["diagnosis"]["hypotheses"] if inv["incidents"] else []
    if not hyps or hyps[0]["cause"] != "dependency":
        p.append(
            f"connection refused by the SMS gateway: expected dependency, got {[h['cause'] for h in hyps]}"
        )
    return p


# --- 9. configuration: an expired certificate, a rotated password ------------------------------------


def config_failures(root):
    code(
        root,
        "src/main/java/com/example/labels/LabelClient.java",
        {77: "        return rest.postForObject(gatewayUrl, req, Resp.class);"},
    )
    code(root, "worker/db.py", {14: "    return psycopg.connect(DSN)"})
    commit_all(root)
    t = NOW - timedelta(minutes=12)
    events = []
    for i in range(10):
        tt = t + timedelta(seconds=20 * i)
        events.append(
            (
                tt,
                [
                    f"{ts(tt)} ERROR [shipping-service] c.e.l.LabelClient - label call failed",
                    'org.springframework.web.client.ResourceAccessException: I/O error on POST request for "https://labels.example.com/v1": PKIX path validation failed: java.security.cert.CertPathValidatorException: validity check failed',
                    "\tat org.springframework.web.client.RestTemplate.doExecute(RestTemplate.java:895)",
                    "\tat com.example.labels.LabelClient.label(LabelClient.java:77)",
                    "Caused by: javax.net.ssl.SSLHandshakeException: PKIX path validation failed: java.security.cert.CertPathValidatorException: validity check failed",
                    "\tat java.base/sun.security.ssl.Alert.createSSLException(Alert.java:130)",
                    "Caused by: java.security.cert.CertificateExpiredException: NotAfter: Sat Oct 18 00:00:00 UTC 2026",
                    "\tat java.base/sun.security.x509.CertificateValidity.valid(CertificateValidity.java:277)",
                ],
            )
        )
        tt2 = tt + timedelta(seconds=5)
        events.append(
            (
                tt2,
                [
                    json.dumps(
                        {
                            "ts": iso(tt2),
                            "level": "error",
                            "service": "report-worker",
                            "msg": "db connect failed",
                            "exc_info": 'Traceback (most recent call last):\n  File "/srv/worker/worker/db.py", line 14, in connect\n'
                            "    return psycopg.connect(DSN)\n"
                            '  File "/usr/local/lib/python3.12/site-packages/psycopg/connection.py", line 119, in connect\n'
                            "    raise last_ex.with_traceback(None)\n"
                            'psycopg.OperationalError: connection failed: FATAL:  password authentication failed for user "reports"',
                        }
                    )
                ],
            )
        )
    write_log(root, events)
    config(root)


def check_config(inv, errors):
    p = []
    by_svc = {next(iter(i["cause"]["services"]), "?"): i for i in inv["incidents"]}
    for svc, want in (("shipping-service", "certificate"), ("report-worker", "password")):
        inc = by_svc.get(svc)
        if not inc:
            p.append(f"no incident for {svc}")
            continue
        hyps = inc["diagnosis"]["hypotheses"]
        if not hyps or hyps[0]["cause"] != "config":
            p.append(
                f"{svc}: expected config ({want}), got {[(h['cause'], h['title'][:50]) for h in hyps]}"
            )
    if len(inv["incidents"]) != 2:
        p.append(f"two services, two incidents; got {len(inv['incidents'])}")
    return p


# --- 10. disk full and file descriptors, no stack traces --------------------------------------------


def no_trace_capacity(root):
    code(
        root,
        "uploader/store.go",
        {41: '\t\tlog.Error().Err(err).Str("path", p).Msg("write failed")'},
    )
    commit_all(root)
    t = NOW - timedelta(minutes=9)
    lines = []
    for i in range(30):
        tt = t + timedelta(seconds=10 * i)
        lines.append(
            json.dumps(
                {
                    "time": iso(tt),
                    "level": "error",
                    "service": "uploader",
                    "caller": "uploader/store.go:41",
                    "error": f"write /data/uploads/{i:05d}.part: no space left on device",
                    "message": "write failed",
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "time": iso(tt + timedelta(seconds=1)),
                    "level": "error",
                    "service": "uploader",
                    "caller": "uploader/store.go:41",
                    "error": "open /data/uploads/index: too many open files",
                    "message": "write failed",
                }
            )
        )
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_no_trace(inv, errors):
    p = []
    kinds = []
    for inc in inv["incidents"]:
        hyps = inc["diagnosis"]["hypotheses"]
        kinds.append(hyps[0]["cause"] if hyps else None)
        if (
            hyps
            and hyps[0]["cause"] == "capacity"
            and not any(w in hyps[0]["title"] for w in ("disk", "file"))
        ):
            p.append(f"capacity, but of what? {hyps[0]['title']}")
    if not kinds or any(k != "capacity" for k in kinds):
        p.append(f"disk full / too many open files: expected capacity, got {kinds}")
    if len(errors["groups"]) != 2:
        p.append(
            f"expected 2 groups (disk, file descriptors), got {[g['title'][:50] for g in errors['groups']]}"
        )
    return p


# --- 11. Node.js ---------------------------------------------------------------------------------------


def node(root):
    code(root, "src/cache/redis.js", {44: "    await this.client.connect();"})
    code(root, "src/routes/cart.js", {19: "  const cart = await cache.get(`cart:${id}`);"})
    commit_all(root)
    t = NOW - timedelta(minutes=6)
    lines = []
    for i in range(12):
        tt = t + timedelta(seconds=15 * i)
        lines += [
            f"{iso(tt)} ERROR [cart-api] Unhandled promise rejection",
            "Error: connect ECONNREFUSED 10.0.3.12:6379",
            "    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1607:16)",
            "    at RedisClient.connect (/app/src/cache/redis.js:44:11)",
            "    at async getCart (/app/src/routes/cart.js:19:16)",
        ]
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_node(inv, errors):
    p = []
    g = errors["groups"][0] if errors["groups"] else None
    if not g:
        return ["nothing found"]
    if "ECONNREFUSED" not in g["title"]:
        p.append(f"title should carry ECONNREFUSED, got {g['title'][:60]}")
    if g["raised_at"] != "src/cache/redis.js:44":
        p.append(f"raised at should be src/cache/redis.js:44, got {g['raised_at']}")
    hyps = inv["incidents"][0]["diagnosis"]["hypotheses"] if inv["incidents"] else []
    if not hyps or hyps[0]["cause"] != "dependency":
        p.append(f"ECONNREFUSED to redis: expected dependency, got {[h['cause'] for h in hyps]}")
    return p


# --- 12. JSON logs from the Logstash encoder -----------------------------------------------------------


def logstash(root):
    code(
        root,
        "src/main/java/com/example/catalog/PriceService.java",
        {93: "        return BigDecimal.valueOf(rate).multiply(price);"},
    )
    code(
        root,
        "src/main/java/com/example/catalog/PriceController.java",
        {40: "        return service.price(sku, currency);"},
    )
    commit_all(root)
    t = NOW - timedelta(minutes=7)
    lines = []
    for i in range(8):
        tt = t + timedelta(seconds=20 * i)
        lines.append(
            json.dumps(
                {
                    "@timestamp": iso(tt),
                    "@version": "1",
                    "message": f"price failed sku=bk-{i} currency=XTS",
                    "logger_name": "com.example.catalog.PriceController",
                    "thread_name": f"http-nio-8080-exec-{i}",
                    "level": "ERROR",
                    "level_value": 40000,
                    "app": "catalog",
                    "traceId": f"c0ffee{i:02d}",
                    "stack_trace": "java.lang.IllegalArgumentException: no exchange rate for XTS\n"
                    "\tat com.example.catalog.PriceService.price(PriceService.java:93)\n"
                    "\tat com.example.catalog.PriceController.price(PriceController.java:40)\n"
                    "\tat java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103)\n"
                    "Caused by: java.util.NoSuchElementException: XTS\n"
                    "\tat java.base/java.util.HashMap.get(HashMap.java:556)\n"
                    "\tat com.example.catalog.RateTable.rate(RateTable.java:21)\n",
                }
            )
        )
    (root / "app.log").write_text("\n".join(lines) + "\n")
    config(root)


def check_logstash(inv, errors):
    p = []
    g = errors["groups"][0] if errors["groups"] else None
    if not g:
        return ["nothing found"]
    if len(errors["groups"]) != 1:
        p.append(f"expected 1 group, got {len(errors['groups'])}")
    if not g["root_cause"] or "NoSuchElementException" not in g["root_cause"]:
        p.append(f"root cause should be NoSuchElementException, got {g['root_cause']}")
    if g["services"] != {"catalog": 8}:
        p.append(f"service should be catalog (the 'app' field), got {g['services']}")
    hyps = inv["incidents"][0]["diagnosis"]["hypotheses"] if inv["incidents"] else []
    if not hyps or hyps[0]["cause"] not in ("code", "data"):
        p.append(f"a missing exchange rate: expected code/data, got {[h['cause'] for h in hyps]}")
    return p


CASES = {
    "cascade": (cascade, check_cascade),
    "red-herring": (red_herring, check_red_herring),
    "storm": (storm, check_storm),
    "interleaved": (interleaved, check_interleaved),
    "py-chain": (py_chain, check_py_chain),
    "go-fatal": (go_fatal, check_go_fatal),
    "truncated": (truncated, check_truncated),
    "retries": (retries, check_retries),
    "config": (config_failures, check_config),
    "no-trace-capacity": (no_trace_capacity, check_no_trace),
    "node": (node, check_node),
    "logstash": (logstash, check_logstash),
}
