"""The repro benchmark's free part: its scenarios are sound, and logsite alone (no AI)
reproduces each planted bug from its incident log."""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

RUN = Path(__file__).parent.parent / "bench" / "repro" / "run.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_repro_run", RUN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_repro_run"] = module
    spec.loader.exec_module(module)
    return module


def test_the_scenarios_are_sound(bench, capsys):
    assert bench.check() == 0, capsys.readouterr().out


@pytest.mark.parametrize(
    ("scenario", "fail_to_pass"),
    [
        # the test fakes a supplier that always times out: after the retry fix the last
        # attempt still does, so it needs "time out once, then answer" by hand
        ("supplier-timeout", False),
        ("isbn-spaces", True),
        ("shipping-config", True),
    ],
)
def test_logsite_alone_reproduces_each_bug(bench, tmp_path, monkeypatch, scenario, fail_to_pass):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = bench.workspace(tmp_path / "ws", scenario)
    assert bench.run_logsite(ws, None)["ok"]
    s = bench.score(ws, scenario, tmp_path / "score")
    assert s["logsite_verdict"] == "reproduced"
    assert s["fails_on_bug"] and s["fail_to_pass"] == fail_to_pass
    assert s["own_tests_pass"] and not s["fixed"]  # it writes the test; fixing is the next step
