"""Agent mode, on real output: pytest 9, go test (Go 1.27), Maven surefire 3 with JUnit 5,
tsc and javac (paths rewritten to /src)."""

import json
import sys
from pathlib import Path

import pytest

from logsite import agent
from logsite.agent import analyse, build_pattern, delta, detect
from logsite.cli import main
from logsite.errors import AppFiles

FIXTURES = Path(__file__).parent / "fixtures" / "agent"


def run_of(name, command, files):
    return analyse(command, (FIXTURES / name).read_text(), 1, 0.1, AppFiles(files))


def summary(run):
    return [(len(c.failures), c.title, c.first.raised_at, c.first.raised_in) for c in run.causes]


PY_FILES = ["app/orders/repository.py", "app/pricing/tax.py", "tests/test_orders.py",
            "tests/test_tax.py"]  # fmt: skip
GO_FILES = ["pricing/tax.go", "pricing/tax_test.go", "catalog/catalog.go",
            "catalog/catalog_test.go"]  # fmt: skip


def test_pytest_30_failures_are_2_causes():
    run = run_of("pytest1.txt", ["pytest"], PY_FILES)
    assert run.tool == "pytest" and run.counts == {"failed": 30, "passed": 12}
    assert summary(run) == [
        (27, "sqlite3.OperationalError: no such column: discount_code",
         "app/orders/repository.py:10", "get"),
        (3, "AssertionError: assert total(<n>) == <n>", "tests/test_tax.py:4", "test_tax_0"),
    ]  # fmt: skip
    assert run.causes[0].tests[0] == "tests/test_orders.py::test_order_0"
    assert run.causes[1].first.message.splitlines()[1:] == [
        "assert 2199 == 2099",
        "+  where 2199 = total(1999)",
    ]


def test_go_test_assertions_and_a_panic():
    run = run_of("gotest1.txt", ["go", "test", "./..."], GO_FILES)
    assert run.tool == "go" and run.counts == {"failed": 4}
    assert summary(run) == [
        (3, "Total(<n>) = <n>, want <n>", "pricing/tax_test.go:7", "TestTotal1999"),
        (1, "runtime error: invalid memory address or nil pointer dereference",
         "catalog/catalog.go:8", "Title"),
    ]  # fmt: skip
    panic = run.causes[1].first
    assert panic.test_file == "catalog/catalog_test.go" and panic.origin is None


def test_maven_junit():
    run = run_of("mvn1.txt", ["mvn", "test"], [
        "src/main/java/com/example/books/Pricing.java",
        "src/test/java/com/example/books/PricingTest.java",
    ])  # fmt: skip
    assert run.tool == "junit"
    assert run.counts == {"passed": 1, "failed": 2, "errors": 2, "skipped": 0}
    (assertion, npe) = run.causes
    assert assertion.title == "AssertionFailedError: expected: <<n>> but was: <<n>>"
    assert assertion.first.raised_at == "src/test/java/com/example/books/PricingTest.java:8"
    assert assertion.first.origin == "org.junit"  # not JUnit's own frame as "raised"
    assert npe.title == "NullPointerException"
    assert npe.first.raised_at == "src/main/java/com/example/books/Pricing.java:9"
    assert npe.tests == [
        "com.example.books.PricingTest.title1",
        "com.example.books.PricingTest.title2",
    ]


def test_tsc_errors_group_by_the_missing_name():
    run = run_of("tsc1.txt", ["tsc"], ["src/cart.ts", "src/checkout.ts", "src/pricing.ts"])
    assert run.tool == "compile" and run.counts == {"errors": 38}
    assert [(len(c.failures), c.title) for c in run.causes] == [
        (31, "TS2304 Cannot find name 'totl'."),
        (6, "TS2345 Argument of type 'string' is not assignable to parameter of type 'number'."),
        (1, "TS2304 Cannot find name 'shippng'."),  # a different typo: its own cause
    ]
    assert run.causes[0].first.raised_at == "src/cart.ts:2"


def test_javac_symbols():
    run = run_of("javac1.txt", ["javac", "src/Shelf.java"], ["src/Shelf.java"])
    assert [(len(c.failures), c.title) for c in run.causes] == [
        (2, "cannot find symbol: variable books"),
        (1, "cannot find symbol: variable booksTitle"),
    ]


def test_build_pattern_and_detect():
    assert (
        build_pattern("TS2304 Cannot find name 'x' at 12") == "TS2304 Cannot find name 'x' at <n>"
    )
    assert build_pattern("undefined: rate") == "undefined: rate"
    assert detect(["python", "-m", "pytest"], []) == "pytest"
    assert detect(["go", "test", "./..."], []) == "go"
    assert detect(["./gradlew", "test"], []) == "junit"
    assert detect(["make"], ["ok  \texample.com/x\t0.1s"]) == "go"
    assert detect(["npx", "tsc"], []) == "compile"


def test_delta():
    run = run_of("pytest1.txt", ["pytest"], ["app/orders/repository.py", "tests/test_tax.py"])
    first, second = run.causes
    previous = {
        "causes": {
            first.id: {"title": first.title, "count": 27, "tests": first.tests},
            "gone1234": {"title": "KeyError: 'x'", "count": 4, "tests": ["t::a", "t::b"]},
        }
    }
    d = delta(run, previous)
    assert [cid for cid, _ in d.fixed] == ["gone1234"] and d.fixed_tests == 2
    assert d.new == [second] and d.still == [first]
    assert delta(run, None) is None


# --- end to end: a real pytest run ---------------------------------------------------------------

REPO = """import sqlite3


def get(order_id):
    db = sqlite3.connect(":memory:")
    db.execute("create table orders (id integer)")
    return db.execute("select id, missing from orders where id = ?", (order_id,)).fetchone()
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    pytest.importorskip("pytest")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "repo.py").write_text(REPO)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    tests = "from app.repo import get\n" + "".join(
        f"\n\ndef test_{i}():\n    assert get({i}) is None\n" for i in range(8)
    )
    (tmp_path / "tests" / "test_repo.py").write_text(tests)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path


PYTEST = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]


def test_cli_agent_first_run_fix_and_show(project, capsys):
    assert main(["agent", "--", *PYTEST]) == 1  # pytest's exit code
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("pytest: 8 failed · 1 cause(s) · exit 1")
    assert lines[2] == "1. 8 test(s)  sqlite3.OperationalError: no such column: missing"
    assert "   raised   app/repo.py:7 in get()" in out
    assert 'return db.execute("select id, missing from orders where id = ?"' in out
    assert "tests    tests/test_repo.py ×8 (test_0, test_1, test_2, …)" in out
    assert "full output: " in out and "logsite agent --show N" in out
    raw = Path(out.split("full output: ")[1].split(" · ")[0])
    assert "test session starts" in raw.read_text()  # the whole output, kept

    assert main(["agent", "--show", "1"]) == 0
    assert "E       sqlite3.OperationalError: no such column: missing" in capsys.readouterr().out

    (project / "app" / "repo.py").write_text(REPO.replace("id, missing", "id"))
    assert main(["agent", "--", *PYTEST]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("pytest: 8 passed · exit 0")
    assert "✓ fixed   sqlite3.OperationalError: no such column: missing  (8 test(s))" in out

    assert main(["agent", "--format", "json", "--", *PYTEST]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["causes"] == [] and data["delta"]["fixed"] == []


def test_cli_agent_never_hides_or_inflates(project, capsys):
    # unrecognised failing output: its last lines
    assert (
        main(["agent", "--", sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"]) == 3
    )
    assert "boom" in capsys.readouterr().out
    # a tiny output is printed as is, never replaced by a longer summary
    assert main(["agent", "--", sys.executable, "-c", "print('ok')"]) == 0
    assert capsys.readouterr().out == "ok\n"
    assert main(["agent", "--", "no-such-command-xyz"]) == 127
    assert "command not found" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="give the command"):
        main(["agent"])


def test_state_dir_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert agent.state_dir() == tmp_path / "logsite" / "agent"
    assert agent.state_key(tmp_path, ["pytest"]) != agent.state_key(tmp_path, ["pytest", "-x"])
