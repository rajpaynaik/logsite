"""Reproducing a production error as a failing test: the case built from the logs, the
pytest skeleton, and the check that a test fails the way production did."""

import ast
import json
import sys
import textwrap

import pytest

from logsite import repro
from logsite.agent import Failure, Run
from logsite.cli import main
from logsite.config import load_config
from logsite.index import index_source

INVENTORY = """\
import logging

from app.supplier import SupplierClient

log = logging.getLogger(__name__)

SUPPLIER = SupplierClient("http://supplier.internal")
SHELVES = {"main": 40, "annex": 12}


def reorder(sku, qty, warehouse):
    log.info("reorder %s qty=%s warehouse=%s", sku, qty, warehouse)
    capacity = SHELVES[warehouse]
    if qty > capacity:
        qty = capacity
    try:
        return SUPPLIER.restock(sku, qty)
    except Exception:
        log.exception("restock failed for %s", sku)
        raise
"""

SUPPLIER = """\
import urllib.request


class SupplierClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def restock(self, sku, qty):
        url = f"{self.base_url}/restock?sku={sku}&qty={qty}"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.read()
"""

TIMEOUT_TRACE = """\
Traceback (most recent call last):
  File "/srv/app/inventory.py", line 17, in reorder
    return SUPPLIER.restock(sku, qty)
  File "/srv/app/supplier.py", line 10, in restock
    with urllib.request.urlopen(url, timeout=2) as resp:
  File "/usr/lib/python3.12/urllib/request.py", line 215, in urlopen
    return opener.open(url, data, timeout)
  File "/usr/lib/python3.12/socket.py", line 720, in readinto
    return self._sock.recv_into(b)
TimeoutError: timed out"""

KEY_TRACE = """\
Traceback (most recent call last):
  File "/srv/app/api.py", line 9, in handle
    return inventory.reorder(body["sku"], body["qty"], body["warehouse"])
  File "/srv/app/inventory.py", line 13, in reorder
    capacity = SHELVES[warehouse]
KeyError: 'overflow'"""


def _line(ts, level, message, lineno, trace, exc=None):
    out = {"timestamp": f"2026-10-21T19:{ts}Z", "level": level, "message": message,
           "pathname": "/srv/app/inventory.py", "lineno": lineno, "trace_id": trace,
           "service": "inventory"}  # fmt: skip
    if exc:
        out["exc_info"] = exc
    return json.dumps(out)


def incident_log():
    lines = []
    for i in range(4):
        sku = f"BK-10{i}"
        lines.append(_line(f"2{i}:01", "INFO", f"reorder {sku} qty=5 warehouse=main", 12, f"t{i}"))
        lines.append(_line(f"2{i}:03", "ERROR", f"restock failed for {sku}", 19, f"t{i}",
                           TIMEOUT_TRACE))  # fmt: skip
    lines.append(_line("30:01", "INFO", "reorder BK-200 qty=2 warehouse=overflow", 12, "k1"))
    lines.append(_line("30:01", "ERROR", "reorder request failed", 9, "k1", KEY_TRACE))
    return "\n".join(lines) + "\n"


API = """\
import logging

from app import inventory

log = logging.getLogger(__name__)


def handle(body):
    return inventory.reorder(body["sku"], body["qty"], body["warehouse"])
"""


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A small inventory service and the log of an incident in it."""
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "inventory.py").write_text(INVENTORY)
    (tmp_path / "app" / "supplier.py").write_text(SUPPLIER)
    (tmp_path / "app" / "api.py").write_text(API)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "shop"\nversion = "0"\n')
    (tmp_path / "incident.log").write_text(incident_log())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    return tmp_path


def case_of(shop, group=None):
    case, _ = repro.find_case(load_config(shop), group, log_file=str(shop / "incident.log"))
    return case


# --- values back to the code -------------------------------------------------------------------


def test_logged_values_map_back_to_the_code_that_filled_the_log_call():
    src = textwrap.dedent("""\
        import logging
        log = logging.getLogger(__name__)
        def f(shipment, carrier, qty, n, total):
            log.info("shipping %s to %s", shipment.id, carrier)
            log.error(f"stock low for {shipment.sku} qty={qty}")
            log.warning("retry {} of {}".format(n, total))
            log.info("no values")
    """)
    s1, s2, s3, s4 = index_source(src, "a.py")
    assert s1.args == ("shipment.id", "carrier")
    assert s1.values("2026-10-21 INFO shipping S-1042 to ups") == {
        "shipment.id": "S-1042",
        "carrier": "ups",
    }
    assert s2.values("stock low for BK-1 qty=0") == {"shipment.sku": "BK-1", "qty": "0"}
    assert s3.values("retry 2 of 5") == {"n": "2", "total": "5"}
    assert s4.args == () and s4.values("no values") == {}
    assert s1.values("something else entirely") == {}


# --- the case ------------------------------------------------------------------------------------


def test_case_from_a_downloaded_log(shop):
    case = case_of(shop)
    assert (case.exc_type, case.raised_at, case.raised_in) == (
        "TimeoutError",
        "app/supplier.py:10",
        "restock",
    )
    assert case.count == 4
    assert case.entry == {
        "at": "app/inventory.py:11",
        "module": "app.inventory",
        "function": "reorder",
        "class": None,
        "params": ["sku", "qty", "warehouse"],
        "init_params": [],
        "is_async": False,
        "args": {},
    }
    assert case.fake["call"] == "urllib.request.urlopen"
    assert case.fake["target"] == "urllib.request.urlopen"
    assert case.fake["raise"] == 'TimeoutError("timed out")'
    # what the failing request logged, by the code that filled the log call; no trace ids
    assert [(i.name, i.value, i.source) for i in case.inputs] == [
        ("sku", "BK-103", "app/inventory.py:12"),
        ("qty", "5", "app/inventory.py:12"),
        ("warehouse", "main", "app/inventory.py:12"),
    ]
    assert case.request["field"] == "trace_id" and len(case.request["lines"]) == 2
    assert case.missing == []


def test_the_group_by_number_or_id(shop):
    key = case_of(shop, "2")
    assert key.title == "KeyError: 'overflow'" and key.raised_at == "app/inventory.py:13"
    assert case_of(shop, key.id[:5]).id == key.id
    # handle(body) is outermost, but body wasn't logged: reorder's arguments were
    assert key.entry["function"] == "reorder" and key.entry["args"] == {
        "sku": "body['sku']",
        "qty": "body['qty']",
        "warehouse": "body['warehouse']",
    }
    with pytest.raises(ValueError, match="no error group ffff"):
        case_of(shop, "ffff")


def test_a_pasted_trace_is_the_same_group_as_the_log(shop):
    path = repro.save_pasted(shop, KEY_TRACE + "\n")
    case, _ = repro.find_case(load_config(shop), None, log_file=str(path))
    assert case.id == case_of(shop, "2").id
    assert any("no trace or request id" in m for m in case.missing)


def test_a_pasted_trace_says_how_the_project_was_called(shop):
    trace = KEY_TRACE.replace(
        "Traceback (most recent call last):\n",
        "Traceback (most recent call last):\n...\n"  # frames left out by whoever pasted it
        '  File "<ipython-input-3-ab12>", line 1, in <module>\n'
        '    handle({"sku": "BK-1", "qty": 5, "warehouse": "overflow"})\n',
    )
    path = repro.save_pasted(shop, trace + "\n")
    case, _ = repro.find_case(load_config(shop), None, log_file=str(path))
    assert case.id == case_of(shop, "2").id
    assert case.caller == {
        "at": "<ipython-input-3-ab12>:1",
        "code": 'handle({"sku": "BK-1", "qty": 5, "warehouse": "overflow"})',
    }
    assert "#   handle({" in repro.skeleton(case)
    # a library's frame says nothing about how the project was called
    assert case_of(shop, "2").caller is None


def test_an_error_logged_without_a_stack_trace_says_so(shop):
    (shop / "plain.log").write_text(
        _line("40:00", "ERROR", "restock failed for BK-1", 19, "") + "\n"
    )
    case, _ = repro.find_case(load_config(shop), None, log_file=str(shop / "plain.log"))
    assert case.exc_type is None
    assert any("no stack trace was logged" in m for m in case.missing)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("import requests\ndef f():\n    requests.post('u')\n", "requests.post"),
        ("from urllib.request import urlopen\ndef f():\n    urlopen('u')\n", "app.x.urlopen"),
        ("import httpx\nclass C:\n    def __init__(self):\n        self.client = httpx.Client()\n"
         "    def f(self):\n        self.client.post('u')\n", "httpx.Client.post"),
        ("import urllib.request as r\nclass C:\n    def __init__(self):\n"
         "        self.o = r.build_opener()\n    def f(self):\n        self.o.open('u')\n", None),
        ("def f(client):\n    client.post('u')\n", None),
    ],
)  # fmt: skip
def test_what_to_patch(source, target):
    tree = ast.parse(source)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func).endswith(("post", "urlopen", "open")))  # fmt: skip
    assert repro._patch_target(tree, call.func, "app.x") == target


# --- the skeleton and the check -------------------------------------------------------------------


def test_the_skeleton_uses_what_production_logged(shop):
    text = repro.skeleton(case_of(shop))
    ast.parse(text)
    assert "from app.inventory import reorder" in text
    assert 'mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out"))' in text
    assert 'sku="BK-103",  # logged at app/inventory.py:12' in text
    assert "qty=5," in text and 'warehouse="main",' in text


def _run(*failures, exit_code=1):
    from logsite.agent import group

    return Run("pytest", ["pytest"], exit_code, 0.1, group(failures), {"failed": len(failures)})


def _failure(exc, raised_at, locations, test_file="tests/test_x.py"):
    return Failure("tests/test_x.py::test_x", "test", exc, "m", raised_at, "f", test_file,
                   locations=locations)  # fmt: skip


def test_check_verdicts(shop):
    data = repro.case_json(case_of(shop))
    through = ["tests/test_x.py:3", "app/inventory.py:17", "app/supplier.py:10"]
    ok = repro.check(data, _run(_failure("TimeoutError", "app/supplier.py:10", through)))
    assert ok.ok and ok.text.startswith("reproduced: TimeoutError through app/supplier.py:10")

    itself = repro.check(data, _run(_failure("TimeoutError", "tests/test_x.py:3",
                                             ["tests/test_x.py:3"])))  # fmt: skip
    assert itself.status == "wrong_failure" and "in the test itself" in itself.text

    before = ["tests/test_x.py:3", "app/inventory.py:17"]
    early = repro.check(data, _run(_failure("TimeoutError", "app/inventory.py:17", before)))
    assert early.status == "wrong_failure" and "something there is faked" in early.text

    other = repro.check(data, _run(_failure("TypeError", "app/inventory.py:13", through)))
    assert other.status == "wrong_failure" and "raised TypeError" in other.text

    moved = repro.check(data, _run(_failure("TimeoutError", "app/supplier.py:12",
                                            ["app/supplier.py:12"])))  # fmt: skip
    assert moved.ok and "near app/supplier.py:10" in moved.notes[0]

    assert repro.check(data, _run(exit_code=0)).status == "passed"
    assert repro.check(data, _run(exit_code=2)).status == "no_result"


def test_repro_and_expect_end_to_end(shop, capsys):
    assert main(["repro", "--from", "incident.log", "--write-test", "--color", "never"]) == 0
    out = capsys.readouterr()
    case_id = case_of(shop).id
    assert f"Reproduce {case_id} · TimeoutError: timed out · 4 error(s)" in out.out
    assert "1 other error group(s)" in out.err
    test = shop / "tests" / f"test_repro_{case_id}.py"
    assert test.is_file()
    pytest_cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]

    # the skeleton fails the way production did, unedited
    assert main(["agent", "--expect", case_id[:6], "--color", "never", "--", *pytest_cmd,
                 str(test)]) == 0  # fmt: skip
    assert (
        f"✓ {case_id} reproduced: TimeoutError through app/supplier.py:10"
        in capsys.readouterr().out
    )

    # a test that raises the error itself doesn't count
    (shop / "tests" / "test_fake.py").write_text("def test_it():\n    raise TimeoutError('x')\n")
    assert main(["agent", "--expect", case_id, "--color", "never", "--", *pytest_cmd,
                 "tests/test_fake.py"]) == 1  # fmt: skip
    assert "wrong failure" in capsys.readouterr().out

    # --format json carries the verdict
    assert main(["agent", "--expect", case_id, "--format", "json", "--", *pytest_cmd,
                 str(test)]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["expect"]["status"] == "reproduced"

    # the test exists: no overwrite without --force
    with pytest.raises(SystemExit, match="exists"):
        main(["repro", "--from", "incident.log", "--write-test"])


def test_expect_needs_a_saved_case(shop):
    with pytest.raises(SystemExit, match="run logsite repro first"):
        main(["agent", "--expect", "abcdef", "--", "true"])


def test_code_that_moved_since_production_ran(shop, capsys):
    # two lines added above the call: production's line 10 is line 12 here
    (shop / "app" / "supplier.py").write_text("# local edit\n# another\n" + SUPPLIER)
    case = case_of(shop)
    assert (case.raised_at, case.local_at) == ("app/supplier.py:10", "app/supplier.py:12")
    assert (
        case.fake["at"] == "app/supplier.py:12" and case.fake["target"] == "urllib.request.urlopen"
    )
    data = repro.case_json(case)
    through = ["tests/test_x.py:3", "app/inventory.py:17", "app/supplier.py:12"]
    v = repro.check(data, _run(_failure("TimeoutError", "app/supplier.py:12", through)))
    assert v.ok and not v.notes


def _group(title, first, last, count, levels=("error",), raised_at="app/x.py:1"):
    from collections import Counter
    from datetime import datetime, timezone

    from logsite.errors import ErrorGroup

    t = datetime(2026, 10, 21, tzinfo=timezone.utc)
    g = ErrorGroup(title[:8].ljust(8, "0"), title, title.split(":")[0], [], count=count)
    g.first_seen, g.last_seen = t.replace(hour=first), t.replace(hour=last)
    g.levels, g.raised_at = Counter(levels), raised_at
    return g


def test_errors_that_started_during_a_file_are_new():
    from datetime import datetime, timezone

    from logsite.errors import mark_started

    start = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)
    end = start.replace(hour=14)
    started = _group("ValueError: invalid ISBN", 9, 14, 400)  # from 09:00 to the end
    all_day = _group("ValueError: qty", 8, 14, 700)
    came_and_went = _group("ConnectionRefusedError: cache", 9, 10, 1500)  # an outage, over
    rare = _group("TimeoutError: timed out", 12, 14, 3)  # too few to tell
    groups = [started, all_day, came_and_went, rare]
    mark_started(groups, start, end)
    assert [g.is_new for g in groups] == [True, False, False, False]


def test_picking_a_group():
    groups = [
        _group("upstream failed: 500", 8, 9, 750, raised_at=None),  # another service's line
        _group("ConnectionRefusedError: cache", 8, 9, 300, levels=("warning",)),
        _group("TimeoutError: timed out", 8, 9, 250),
        _group("ZeroDivisionError: division by zero", 8, 9, 45),
    ]
    assert repro.pick_group(groups, None).title == "TimeoutError: timed out"
    assert repro.pick_group(groups, "zerodivision").title.startswith("ZeroDivisionError")
    assert repro.pick_group(groups, "1").title.startswith("upstream")


def test_a_file_is_read_whole_whatever_its_dates(shop):
    future = incident_log().replace("2026-10-21T", "2099-01-01T")
    (shop / "future.log").write_text(future)
    case, _ = repro.find_case(load_config(shop), None, log_file=str(shop / "future.log"))
    assert case.count == 4
