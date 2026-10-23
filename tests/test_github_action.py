"""The GitHub Action's script: a draft pull request with a reproduction test per new error."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "integrations" / "github-action" / "repro_prs.py"
INCIDENT = ROOT / "bench" / "repro" / "scenarios" / "supplier-timeout" / "incident.log"

spec = importlib.util.spec_from_file_location("repro_prs", SCRIPT)
repro_prs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro_prs)

FAKE_GH = """#!/bin/sh
printf '%s ' "$@" | tr '\n' ' ' >> "$GH_LOG"; echo >> "$GH_LOG"  # one line per call
case "$1 $2" in
  "pr list") if grep -q -- "$4" "$GH_EXISTING" 2>/dev/null; then echo '[{"number": 1}]'; else echo '[]'; fi ;;
  "pr create") echo "https://github.com/acme/bookstore/pull/7" ;;
esac
"""  # noqa: E501


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# The demo log's error runs through the whole file, so it isn't new: these use --all.


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """The bookstore demo as a git repo with a remote, and a fake gh that records its calls."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    repo = tmp_path / "bookstore"
    shutil.copytree(ROOT / "examples" / "bookstore", repo)
    shutil.copy(INCIDENT, repo / "incident.log")
    git(tmp_path, "init", "-q", "--bare", "-b", "main", "remote.git")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    git(repo, "push", "-q", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    logsite = shutil.which("logsite") or str(Path(sys.executable).with_name("logsite"))
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), str(Path(logsite).parent), os.environ["PATH"]])
    )
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("GH_EXISTING", str(tmp_path / "existing"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".data"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(repo)
    return tmp_path


def test_new_groups_are_the_new_ones_raised_in_the_code():
    data = {"groups": [
        {"id": "a", "new": True, "raised_at": "app/x.py:1"},
        {"id": "b", "new": False, "raised_at": "app/x.py:2"},
        {"id": "c", "new": True, "raised_at": None},  # another service's error line
    ]}  # fmt: skip
    assert [g["id"] for g in repro_prs.new_groups(data)] == ["a"]
    assert [g["id"] for g in repro_prs.new_groups(data, include_all=True)] == ["a", "b"]


def test_opens_a_draft_pull_request_with_the_test_and_the_verdict(shop, capsys):
    code = repro_prs.main([
        "--from", "incident.log", "--all", "--max-prs", "1",
        "--test-command", f"{sys.executable} -m pytest",
    ])  # fmt: skip
    assert code == 0
    out = capsys.readouterr().out
    assert "opened https://github.com/acme/bookstore/pull/7" in out
    calls = (shop / "gh.log").read_text().splitlines()
    create = next(c for c in calls if c.startswith("pr create"))
    assert "--draft --base main --head logsite/repro-" in create
    assert "Reproduce: TimeoutError: timed out" in create
    assert "✓" in create and "as in production" in create  # the --expect verdict
    # the branch was pushed with just the test, and the checkout is back on main
    branch = create.split("--head ")[1].split()[0]
    remote = shop / "remote.git"
    files = subprocess.run(["git", "diff", "--name-only", "main", branch], cwd=remote,
                           capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert len(files) == 1 and files[0].startswith("tests/test_repro_")
    assert subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True,
                          check=True).stdout.strip() == "main"  # fmt: skip


def test_never_opens_the_same_pull_request_twice(shop, capsys):
    data = json.loads(subprocess.run(
        ["logsite", "errors", "--from", "incident.log", "--format", "json", "--no-history"],
        capture_output=True, text=True,
    ).stdout)  # fmt: skip
    first = repro_prs.new_groups(data, include_all=True)[0]["id"]
    (shop / "existing").write_text(f"logsite/repro-{first}\n")
    repro_prs.main(["--from", "incident.log", "--all", "--max-prs", "1"])
    assert f"skip {first}: a pull request exists already" in capsys.readouterr().out
    assert not any(c.startswith("pr create") for c in (shop / "gh.log").read_text().splitlines())


def test_dry_run_opens_nothing(shop, capsys):
    repro_prs.main(["--from", "incident.log", "--all", "--dry-run"])
    assert "would open logsite/repro-" in capsys.readouterr().out
    assert not any(c.startswith("pr create") for c in (shop / "gh.log").read_text().splitlines())
