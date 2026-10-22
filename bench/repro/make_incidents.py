"""Write each scenario's incident.log by running examples/bookstore for real.

Requests go through ``bookstore.api.handle`` with the service's JSON logging, against a local
supplier server that's slow when asked to be, so the timeouts are real socket timeouts and
every stack trace is the one Python printed. Paths are rewritten to production's
(/srv/bookstore/..., /usr/local/lib/python3.12/...).

The easy scenarios are one short run each, a request at a time. The hard ones are a day of
traffic, assembled from real requests captured the same way: tens of thousands of them,
interleaved as concurrent requests are, with loud errors that aren't the bug, a bug that
starts mid-morning, one that needs a rare input, and one where production ran older code.
Everything is seeded, so the logs only change when the code does.

    python bench/repro/make_incidents.py            # every scenario
    python bench/repro/make_incidents.py --hard     # the hard ones only (not in git: ~50 MB)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent / "examples" / "bookstore"
EASY = ("supplier-timeout", "isbn-spaces", "shipping-config")

ISBNS = ["978-0-13-468599-1", "978-1-59327-584-6", "978-0-596-52068-7", "978-1-4919-5017-1",
         "978-0-262-03384-8", "978-1-098-10403-4"]  # fmt: skip
TITLES = ["Effective Python", "The Linux Command Line", "Programming Python", "Fluent Python",
          "Introduction to Algorithms", "Designing Data-Intensive Applications"]  # fmt: skip
PAGES = [480, 504, 1600, 792, 1312, 611]


# --- running the service (in a subprocess: --capture) -------------------------------------------


class Supplier(BaseHTTPRequestHandler):
    slow = threading.Event()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.slow.is_set():
            time.sleep(0.5)  # past the client's 0.2 s timeout
        reply = json.dumps({"qty": body["qty"]}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)
        except OSError:
            pass  # the client gave up

    def log_message(self, *args):
        pass


class Clock:
    def __init__(self, start: str):
        self.t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()

    def __call__(self) -> float:
        self.t += 0.37
        return self.t


def rewriter(app: Path):
    def rewrite(text: str) -> str:
        text = text.replace(str(app), "/srv/bookstore")
        return re.sub(r"[^\s\"']*/lib/python3\.\d+/", "/usr/local/lib/python3.12/", text)

    return rewrite


def feed(rng: random.Random, spaced: bool = False, zero_pages: bool = False) -> list[dict]:
    rows = []
    for i in rng.sample(range(len(ISBNS)), 3):
        rows.append({"isbn": ISBNS[i], "title": TITLES[i], "price": f"{rng.uniform(20, 60):.2f}",
                     "pages": PAGES[i]})  # fmt: skip
    if spaced:  # this supplier sends ISBNs with spaces
        rows[1]["isbn"] = rows[1]["isbn"].replace("-", " ")
    if zero_pages:  # ... and 0 for a page count it doesn't know yet
        rows[1]["pages"] = 0
    return rows


def request(api, kind: str, rng: random.Random, trace: str) -> None:
    """One request of a kind; the service logs it."""
    from bookstore.cache import CACHE

    CACHE.up = kind != "quote-cache-down"
    Supplier.slow.set() if kind == "reorder-slow" else Supplier.slow.clear()
    if kind in ("ok-feed", "feed-spaced", "feed-pages0"):
        body = {"rows": feed(rng, kind == "feed-spaced", kind == "feed-pages0")}
        api.handle("POST /feed", body, trace)
    elif kind in ("ok-reorder", "reorder-slow", "reorder-qty0"):
        qty = 0 if kind == "reorder-qty0" else rng.randint(1, 12)
        api.handle("POST /reorder", {"sku": f"BK-{rng.randint(1000, 1999)}", "qty": qty}, trace)
    else:  # quotes: small orders reach the rate lookup, large ones ship free
        zone = rng.choice(["domestic", "eu", "world"])
        small = kind == "quote-small"
        subtotal = round(rng.uniform(12, 45) if small else rng.uniform(55, 120), 2)
        body = {"zone": zone, "weight_kg": round(rng.uniform(0.3, 4.0), 1), "subtotal": subtotal}
        api.handle("POST /quote", body, trace)
    Supplier.slow.clear()
    CACHE.up = True


def easy_traffic(api, name: str, rng: random.Random, n: int) -> None:
    """The easy scenarios: one request at a time, one bug and a cache outage."""
    for i in range(n):
        trace = f"{rng.getrandbits(64):016x}"
        what = rng.choice(["feed", "reorder", "quote", "quote"])
        if what == "feed":
            bad = name == "isbn-spaces" and rng.random() < 0.5
            kind = "feed-spaced" if bad else "ok-feed"
        elif what == "reorder":
            kind = (
                "reorder-slow"
                if name == "supplier-timeout" and rng.random() < 0.6
                else "ok-reorder"
            )
        else:
            small = name == "shipping-config" and rng.random() < 0.7
            kind = "quote-small" if small else "ok-quote"
            if 40 <= i < 46:
                kind = "quote-small" if small else "quote-cache-down"
        request(api, kind, rng, trace)


TEMPLATE_KINDS = ("ok-feed", "ok-reorder", "ok-quote", "quote-cache-down", "reorder-qty0",
                  "reorder-slow", "feed-spaced", "feed-pages0")  # fmt: skip


def capture(profile: str, app: Path, out: Path, seed: int) -> None:
    """Run the service from ``app`` and write its JSON log lines to ``out``."""
    port = _free_port()
    settings = json.loads((app / "settings.json").read_text())
    settings["supplier"]["url"] = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp())
    (tmp / "settings.json").write_text(json.dumps(settings))
    os.environ["BOOKSTORE_SETTINGS"] = str(tmp / "settings.json")
    sys.path.insert(0, str(app))
    from bookstore import api
    from bookstore.logs import JsonFormatter

    server = ThreadingHTTPServer(("127.0.0.1", port), Supplier)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.FileHandler(out, mode="w", encoding="utf-8")
    handler.setFormatter(JsonFormatter(Clock(f"2026-10-2{seed}T08:00:00"), rewriter(app)))
    root.handlers = [handler]
    rng = random.Random(seed)
    try:
        if profile == "templates":  # 12 of each kind, each request's trace id says its kind
            for kind in TEMPLATE_KINDS:
                for i in range(12):
                    request(api, kind, rng, f"{kind}~{i}")
        else:
            easy_traffic(api, profile, rng, 160)
    finally:
        handler.close()
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_capture(profile: str, app: Path, out: Path, seed: int) -> None:
    """``capture`` in a fresh interpreter, so each run imports the service from scratch."""
    cmd = [sys.executable, __file__, "--capture", profile, "--app", str(app), "--out", str(out),
           "--seed", str(seed)]  # fmt: skip
    subprocess.run(cmd, check=True)


# --- assembling a day ---------------------------------------------------------------------------


def templates(path: Path) -> dict[str, list[list[dict]]]:
    """``{kind: [request, ...]}``, each request its log records in order."""
    by_trace: dict[str, list[dict]] = {}
    for line in path.read_text().splitlines():
        record = json.loads(line)
        by_trace.setdefault(record["trace_id"], []).append(record)
    out: dict[str, list[list[dict]]] = {}
    for trace, records in by_trace.items():
        out.setdefault(trace.split("~")[0], []).append(records)
    return out


def assemble(
    kinds: dict[str, list[list[dict]]],
    plan: list[tuple[str, int, list[tuple[float, float]]]],
    start: str,
    hours: float,
    rng: random.Random,
    after_error=None,
) -> list[dict]:
    """A day of traffic: ``count`` requests of each kind, at random times within its
    windows (fractions of the day), each with its own trace id and its lines a few to a few
    hundred milliseconds apart, so requests that overlap interleave as they do in
    production. ``after_error(records, rng)`` adds lines after a failed request's error."""
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    span = hours * 3600
    out = []
    for kind, count, windows in plan:
        for _ in range(count):
            lo, hi = rng.choice(windows)
            t = t0 + timedelta(seconds=rng.uniform(lo, hi) * span)
            trace = f"{rng.getrandbits(64):016x}"
            records = []
            for r in rng.choice(kinds[kind]):
                t += timedelta(milliseconds=rng.uniform(3, 250 if "slow" not in kind else 420))
                records.append({**r, "timestamp": t.isoformat(), "trace_id": trace})
            if after_error is not None and records[-1]["level"] == "ERROR":
                records += after_error(records, rng)
            out += records
    out.sort(key=lambda r: r["timestamp"])
    return out


def gateway_retries(records: list[dict], rng: random.Random) -> list[dict]:
    """The API gateway in front of the service retries a 500 twice, logging each failure:
    the loudest errors in the log, and none of them in this codebase."""
    t = datetime.fromisoformat(records[-1]["timestamp"])
    route = records[0]["message"].removeprefix("request ")
    out = []
    for attempt in (1, 2, 3):
        t += timedelta(milliseconds=rng.uniform(250, 900))
        out.append({
            "timestamp": t.isoformat(), "level": "ERROR", "logger": "gateway.proxy",
            "message": f"upstream bookstore {route} failed: 500 Internal Server Error "
                       f"(attempt {attempt}/3)",
            "pathname": "/srv/gateway/gateway/proxy.py", "lineno": 88,
            "trace_id": records[-1]["trace_id"], "service": "gateway",
        })  # fmt: skip
    return out


ALL_DAY = [(0.0, 1.0)]
HARD = {
    # A day of logs (~25 MB). A cache outage and client errors are louder; the bug only
    # starts at 09:00, when a supplier switched its feed to ISBNs with spaces.
    "hard-noisy-day": dict(
        start="2026-10-21T08:00:00", hours=6, plan=[
            ("ok-quote", 11000, ALL_DAY), ("ok-reorder", 5000, ALL_DAY),
            ("ok-feed", 3500, ALL_DAY),
            ("quote-cache-down", 1500, [(0.15, 0.22), (0.60, 0.66)]),
            ("reorder-qty0", 700, ALL_DAY), ("reorder-slow", 60, ALL_DAY),
            ("feed-spaced", 400, [(1 / 6, 1.0)]),
        ],
    ),
    # One feed row in a hundred has 0 pages; its page count is on a line of its own,
    # among the lines of every other request in flight.
    "hard-rare-value": dict(
        start="2026-10-22T08:00:00", hours=4, plan=[
            ("ok-feed", 2500, ALL_DAY), ("feed-pages0", 45, ALL_DAY),
            ("ok-quote", 4000, ALL_DAY), ("ok-reorder", 2000, ALL_DAY),
            ("quote-cache-down", 300, [(0.4, 0.45)]), ("reorder-qty0", 200, ALL_DAY),
        ],
    ),
    # Production runs last week's build (the supplier client without its docstring: every
    # line after it is 5 earlier than here), and the gateway's retries are the loudest
    # errors in the log.
    "hard-drift": dict(
        start="2026-10-23T08:00:00", hours=3, old_code=True, after_error=gateway_retries, plan=[
            ("ok-reorder", 2000, ALL_DAY), ("reorder-slow", 250, ALL_DAY),
            ("ok-quote", 2500, ALL_DAY), ("ok-feed", 1000, ALL_DAY),
        ],
    ),
}  # fmt: skip


def last_weeks_build(dest: Path) -> Path:
    """The service as production runs it in hard-drift: an older build."""
    shutil.copytree(APP, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    path = dest / "bookstore" / "supplier.py"
    text = path.read_text()
    doc = re.search(r'\n    """Orders stock.*?"""\n', text, re.S)
    assert doc, "the supplier client's docstring moved: update last_weeks_build"
    path.write_text(text[: doc.start()] + "\n" + text[doc.end() :])
    return dest


def write(out: Path, records: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def describe(out: Path) -> str:
    text = out.read_text()
    size = len(text.encode()) / 1e6
    lines, errors = text.count("\n"), text.count('"level": "ERROR"')
    return f"{out.relative_to(HERE)}: {lines:,} lines, {size:.1f} MB, {errors:,} errors"


def make_hard(names: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        run_capture("templates", APP, tmp / "now.jsonl", 7)
        kinds = {"now": templates(tmp / "now.jsonl")}
        for n, name in enumerate(names):
            spec = HARD[name]
            if spec.get("old_code") and "old" not in kinds:
                run_capture("templates", last_weeks_build(tmp / "old"), tmp / "old.jsonl", 8)
                kinds["old"] = templates(tmp / "old.jsonl")
            records = assemble(
                kinds["old" if spec.get("old_code") else "now"], spec["plan"], spec["start"],
                spec["hours"], random.Random(100 + n), spec.get("after_error"),
            )  # fmt: skip
            out = HERE / "scenarios" / name / "incident.log"
            write(out, records)
            print(describe(out))


def make_easy() -> None:
    for n, name in enumerate(EASY):
        out = HERE / "scenarios" / name / "incident.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        run_capture(name, APP, out, n)
        print(describe(out))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hard", action="store_true", help="only the hard scenarios")
    p.add_argument("--scenario", action="append", choices=list(HARD), help="these hard ones")
    p.add_argument("--capture", help=argparse.SUPPRESS)
    p.add_argument("--app", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--seed", type=int, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.capture:
        capture(args.capture, args.app, args.out, args.seed)
        return
    if not (args.hard or args.scenario):
        make_easy()
    make_hard(args.scenario or list(HARD))


if __name__ == "__main__":
    main()
