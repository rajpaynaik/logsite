"""Write each scenario's incident.log by running examples/bookstore for real.

Requests go through ``bookstore.api.handle`` with the service's JSON logging, against a local
supplier server that's slow when asked to be, so the timeouts are real socket timeouts and
every stack trace is the one Python printed. Paths are rewritten to production's
(/srv/bookstore/..., /usr/local/lib/python3.12/...); times run on a fixed clock, so the logs
only change when the code does.

    python bench/repro/make_incidents.py
"""

from __future__ import annotations

import json
import logging
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent / "examples" / "bookstore"
sys.path.insert(0, str(APP))

from bookstore import api, catalog, inventory  # noqa: E402
from bookstore.cache import CACHE  # noqa: E402
from bookstore.logs import JsonFormatter  # noqa: E402

SLOW = threading.Event()


class Supplier(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if SLOW.is_set():
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


def rewrite(text: str) -> str:
    text = text.replace(str(APP), "/srv/bookstore")
    return re.sub(r"[^\s\"']*/lib/python3\.\d+/", "/usr/local/lib/python3.12/", text)


ISBNS = ["978-0-13-468599-1", "978-1-59327-584-6", "978-0-596-52068-7", "978-1-4919-5017-1",
         "978-0-262-03384-8", "978-1-098-10403-4"]  # fmt: skip
TITLES = ["Effective Python", "The Linux Command Line", "Programming Python", "Fluent Python",
          "Introduction to Algorithms", "Designing Data-Intensive Applications"]  # fmt: skip


def feed(rng: random.Random, spaced: bool) -> list[dict]:
    rows = []
    for i in rng.sample(range(len(ISBNS)), 3):
        isbn = ISBNS[i]
        rows.append({"isbn": isbn, "title": TITLES[i], "price": f"{rng.uniform(20, 60):.2f}"})
    if spaced:  # this supplier sends ISBNs with spaces
        rows[1]["isbn"] = rows[1]["isbn"].replace("-", " ")
    return rows


def traffic(name: str, rng: random.Random, n: int) -> None:
    for i in range(n):
        trace = f"{rng.getrandbits(64):016x}"
        CACHE.up = not (40 <= i < 46)  # a short cache outage: warnings, nothing breaks
        kind = rng.choice(["feed", "reorder", "quote", "quote"])
        if kind == "feed":
            bad = name == "isbn-spaces" and rng.random() < 0.5
            api.handle("POST /feed", {"rows": feed(rng, bad)}, trace)
        elif kind == "reorder":
            slow = name == "supplier-timeout" and rng.random() < 0.6
            SLOW.set() if slow else SLOW.clear()
            sku = f"BK-{rng.randint(1000, 1999)}"
            api.handle("POST /reorder", {"sku": sku, "qty": rng.randint(1, 12)}, trace)
            SLOW.clear()
        else:
            zone = rng.choice(["domestic", "eu", "world"])
            small = name == "shipping-config" and rng.random() < 0.7
            subtotal = round(rng.uniform(12, 45) if small else rng.uniform(55, 120), 2)
            weight = round(rng.uniform(0.3, 4.0), 1)
            api.handle("POST /quote", {"zone": zone, "weight_kg": weight, "subtotal": subtotal},
                       trace)  # fmt: skip


SCENARIOS = {"supplier-timeout": 160, "isbn-spaces": 160, "shipping-config": 160}


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8099), Supplier)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        for n, (name, requests) in enumerate(SCENARIOS.items()):
            out = HERE / "scenarios" / name / "incident.log"
            out.parent.mkdir(parents=True, exist_ok=True)
            catalog.BOOKS.clear()
            inventory.STOCK.clear()
            CACHE.data.clear()
            handler = logging.FileHandler(out, mode="w", encoding="utf-8")
            handler.setFormatter(JsonFormatter(Clock(f"2026-10-2{n}T08:00:00"), rewrite))
            root.handlers = [handler]
            traffic(name, random.Random(n), requests)
            handler.close()
            lines = out.read_text().count("\n")
            errors = out.read_text().count('"level": "ERROR"')
            print(f"{out.relative_to(HERE)}: {lines} lines, {errors} errors")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
