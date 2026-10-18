import io
import json

import pytest

from logsite.cli import main
from logsite.hook import claude, rewrite


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest -q", "logsite agent -- pytest -q"),
        ("python -m pytest tests/", "logsite agent -- python -m pytest tests/"),
        (
            "rtk pytest -q",
            "logsite agent -- pytest -q",
        ),  # RTK's rewrite: agent mode needs raw output
        ("cd app && go test ./...", "cd app && logsite agent -- go test ./..."),
        ("mvn -q test", "logsite agent -- mvn -q test"),
        ("./gradlew build", "logsite agent -- ./gradlew build"),
        ("npx tsc --noEmit", "logsite agent -- npx tsc --noEmit"),
        ("mvn dependency:tree", None),  # not a build or test
        ("pytest -q | tail -5", None),  # the assistant asked for a pipe: leave it
        ("pytest > out.txt", None),
        ("pytest && git commit -m x", None),
        ("FOO=1 pytest", None),
        ("logsite agent -- pytest", None),
        ("git status", None),
        ("pytest 'unclosed", None),
    ],
)
def test_rewrite(command, expected):
    assert rewrite(command) == expected


def run_hook(event, monkeypatch=None):
    out = io.StringIO()
    assert claude(io.StringIO(json.dumps(event)), out) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_hook_output_keeps_the_other_input_fields():
    out = run_hook({"tool_name": "Bash", "tool_input": {"command": "pytest", "timeout": 600}})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["updatedInput"] == {"command": "logsite agent -- pytest", "timeout": 600}
    assert "permissionDecision" not in hso  # the usual permission rules still apply


def test_hook_stays_out_of_the_way(monkeypatch):
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None
    assert claude(io.StringIO("not json"), io.StringIO()) == 0
    monkeypatch.setenv("LOGSITE_HOOK", "off")
    assert run_hook({"tool_name": "Bash", "tool_input": {"command": "pytest"}}) is None


def test_cli_hook_ignores_a_broken_config(tmp_path, monkeypatch, capsys):
    (tmp_path / "logsite.toml").write_text("this is [not toml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"tool_name":"Bash","tool_input":{"command":"pytest"}}')
    )
    assert main(["hook", "claude"]) == 0
    assert "logsite agent -- pytest" in capsys.readouterr().out
