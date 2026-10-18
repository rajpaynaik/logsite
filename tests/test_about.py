import json

import pytest
from test_incidents import project

from logsite.about import Span, parse_span, relation
from logsite.cli import main
from logsite.errors import ErrorGroup

JAVA = "src/main/java/com/example/shipping/CarrierApiClient.java"


def test_cli_about_a_line(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["about", f"{JAVA}:3"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == f"{JAVA}:1-5"  # a line outside Python widens to the lines near it
    assert "  errors in the last 24h:" in out
    assert "IllegalStateException: Shipping label request failed  (raised here, new)" in out
    assert "root cause: java.net.SocketTimeoutException: Read timed out" in out

    assert main(["about", f"{JAVA}:3", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (err,) = data["errors"]
    assert err["relation"] == "raised here" and err["count"] == 10
    assert data["unavailable"] == [] and data["lines"] == [1, 5]


def test_cli_about_without_a_backend(tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pay.py").write_text(
        "import logging\nlog = logging.getLogger(__name__)\n\n\n"
        "def pay(order):\n    log.info(f'paying {order}')\n    return order\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["about", "app/pay.py:6"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "app/pay.py:5-7  pay()"  # the function around the line
    assert "app/pay.py:6  info('paying {}')" in out
    assert "6: LL001" in out  # lint still works
    assert "(no production data: no [backend] configured)" in out
    with pytest.raises(SystemExit, match="no file app/nope.py"):
        main(["about", "app/nope.py"])


def test_parse_span(tmp_path):
    (tmp_path / "a.go").write_text("\n".join(f"// {i}" for i in range(1, 101)))
    assert parse_span(tmp_path, "a.go") == Span("a.go", 1, 100)
    assert parse_span(tmp_path, "a.go:50") == Span("a.go", 35, 65)
    assert parse_span(tmp_path, "a.go:20-10") == Span("a.go", 10, 20)
    assert parse_span(tmp_path, "./a.go:90-200") == Span("a.go", 90, 100)


def test_relation():
    span = Span("app/x.py", 10, 20)
    g = ErrorGroup("f", "t", "ValueError", [])
    g.raised_at, g.frames_at = "app/y.py:3", ["app/x.py:12", "app/y.py:3"]
    assert relation(g, span) == "passes through"
    g.raised_at = "app/x.py:15"
    assert relation(g, span) == "raised here"
    g.raised_at, g.frames_at = "app/y.py:3", []
    assert relation(g, span) is None


def test_mcp_production_context(tmp_path):
    pytest.importorskip("mcp")
    from test_mcp import call

    project(tmp_path)
    err, out = call(tmp_path, "production_context", {"path": JAVA, "line": 3})
    assert not err, out
    assert out["errors"][0]["relation"] == "raised here"
    assert out["errors"][0]["root_cause"] == "java.net.SocketTimeoutException: Read timed out"
    err, out = call(tmp_path, "production_context", {"path": "nope.py"})
    assert err
