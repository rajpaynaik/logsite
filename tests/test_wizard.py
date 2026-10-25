"""logsite init: the terminal setup, answered by a script."""

import io
import json
import os

import pytest

from logsite.cli import main
from logsite.config import load_config
from logsite.webapp import App
from logsite.wizard import Wizard

LOG = json.dumps({"timestamp": "2026-10-20T08:00:00+00:00", "level": "ERROR",
                  "message": "boom", "exc_info": "Traceback (most recent call last):\n"
                  '  File "/srv/app/x.py", line 3, in f\nKeyError: \'id\''})  # fmt: skip


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("LOGSITE_SLACK_WEBHOOK", "LOGSITE_DATABASE_URL", "LOGSITE_LOKI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "svc"
    root.mkdir()
    (root / "app.log").write_text(LOG + "\n")
    return root


def run(root, answers, secrets, monkeypatch=None):
    asked: list[str] = []

    def script(queue):
        def answer(prompt):
            asked.append(prompt)
            if not queue:
                raise AssertionError(f"no answer left for {prompt!r}")
            return queue.pop(0)

        return answer

    out = io.StringIO()
    Wizard(root, out=out, ask=script(list(answers)), secret=script(list(secrets))).run()
    return out.getvalue(), asked


def test_a_log_file_sqlite_and_no_slack(project):
    # file backend; a wrong path first, then the right one; every step after keeps the default
    out, asked = run(project, ["5", "nope.log", "y", "app.log", ""], ["", ""])
    assert "no file at" in out and "connected: 1 error(s) in the file" in out
    assert "a SQLite file it is" in out and "Saved" in out
    assert "Path to the log file [nope.log]: " in "".join(asked)  # the retry offers the last answer
    cfg = load_config(project)
    assert cfg.backend == {"type": "file", "path": str(project / "app.log")}
    assert cfg.store.url is None and cfg.watch.every == "5m"
    assert "logsite watch               # keep checking, store every error\n" in out


def test_slack_and_a_database_url_go_to_the_secrets_file(project, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(App, "test_slack", lambda self, form: sent.append(form) or {"ok": True})
    db = f"sqlite:///{tmp_path / 'errors.db'}"
    hook = "https://hooks.slack.com/services/T/B/x"
    run(project, ["5", "app.log", "y", "10m"], [hook, db])
    assert sent == [{"webhook": hook}]
    text = (project / "logsite.toml").read_text()
    assert hook not in text and db not in text  # never in the config
    assert 'slack_webhook_env = "LOGSITE_SLACK_WEBHOOK"' in text
    assert 'url_env = "LOGSITE_DATABASE_URL"' in text and 'every = "10m"' in text
    secrets = json.loads((tmp_path / "cfg" / "logsite" / "secrets.json").read_text())
    assert secrets == {"LOGSITE_SLACK_WEBHOOK": hook, "LOGSITE_DATABASE_URL": db}
    assert oct(os.stat(tmp_path / "cfg" / "logsite" / "secrets.json").st_mode)[-3:] == "600"
    cfg = load_config(project)
    assert cfg.alerts.slack_webhook == hook and cfg.store.url == db


def test_running_it_again_keeps_what_was_saved(project, monkeypatch, tmp_path):
    monkeypatch.setattr(App, "test_slack", lambda self, form: {"ok": True})
    db = f"sqlite:///{tmp_path / 'errors.db'}"
    run(project, ["5", "app.log", "n", ""], ["https://hooks.slack.com/services/a", db])
    # Enter everywhere: the same store type, path, webhook, database and interval
    out, asked = run(project, ["", "", ""], ["", ""])
    prompts = "".join(asked)
    assert "Choose [5]" in prompts and "Path to the log file [app.log]" in prompts
    assert "Webhook URL (saved; Enter keeps it)" in prompts
    assert "Database URL (saved; Enter keeps it)" in prompts
    cfg = load_config(project)
    assert cfg.alerts.slack_webhook == "https://hooks.slack.com/services/a"
    assert cfg.store.url == db


def test_a_failing_log_store_saves_nothing_unless_asked(project):
    with pytest.raises(SystemExit, match="nothing saved"):
        run(project, ["5", "nope.log", "n", "n"], [])
    assert not (project / "logsite.toml").exists()


def test_a_secret_typed_once_survives_a_retry(project, monkeypatch):
    tried = []

    def fake(self, form):
        tried.append(form)
        return {"ok": len(tried) > 1, "error": "401", "lines": [], "errors": 0,
                "scope": "the last 24 hours"}  # fmt: skip

    monkeypatch.setattr(App, "test_backend", fake)
    # Loki: url, selector, username; password typed once; then the URL is fixed on the retry
    answers = ["1", "http://loki:3100", '{app="x"}', "", "", "y", "http://loki:3101", "", "",
               "", "", ""]  # fmt: skip
    run(project, answers, ["s3cret", "", "", ""])
    assert tried[1]["values"] == {"url": "http://loki:3101", "selector": '{app="x"}',
                                  "password": "s3cret"}  # fmt: skip
    text = (project / "logsite.toml").read_text()
    assert "s3cret" not in text and 'password_env = "LOGSITE_LOKI_PASSWORD"' in text


def test_not_in_a_terminal(project, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit, match="asks questions"):
        main(["--config-dir", str(project), "init"])
