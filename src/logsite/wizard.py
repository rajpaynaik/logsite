"""``logsite init``: set logsite up from the terminal, one question at a time.

The same steps as the browser setup (``logsite app``) and the same code behind them: where
the logs are (tested before it's saved), Slack (a test message), and where to store the
errors (tested and its tables created). ``logsite.toml`` gets the settings; passwords,
tokens and webhooks go to the secrets file, never into the config.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path

from .term import Palette
from .webapp import BACKENDS, App, secrets_path

Ask = Callable[[str], str]


class Wizard:
    def __init__(self, root: Path, out=None, ask: Ask = input, secret: Ask = getpass.getpass,
                 color: Palette | None = None) -> None:  # fmt: skip
        self.app = App(root)
        self.out = out or sys.stdout
        self.ask, self.secret = ask, secret
        self.c = color or Palette(False)

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def question(self, label: str, default: str = "", hint: str = "") -> str:
        shown = f" [{default}]" if default else (f" ({hint})" if hint else "")
        answer = self.ask(f"  {label}{shown}: ").strip()
        return answer or default

    def yes(self, label: str, default: bool = True) -> bool:
        answer = self.ask(f"  {label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not answer else answer.startswith("y")

    def choose(self, label: str, options: list[tuple[str, str]], default: str | None) -> str:
        for i, (_key, text) in enumerate(options, 1):
            self.say(f"    {i}) {text}")
        keys = [k for k, _ in options]
        pick = str(keys.index(default) + 1) if default in keys else ""
        while True:
            answer = self.question(label, pick)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return keys[int(answer) - 1]
            if answer in keys:
                return answer
            self.say(f"  {self.c.yellow('Pick a number from 1 to')} {len(options)}")

    def ok(self, text: str) -> None:
        self.say(f"  {self.c.green('✓')} {text}")

    def fail(self, text: str) -> None:
        self.say(f"  {self.c.red('✗')} {text}")

    # --- the steps ----------------------------------------------------------------------

    def run(self) -> dict:
        settings = self.app.settings()
        status = self.app.status()
        self.say(self.c.bold(f"Setting up logsite for {status['project_name']}"))
        self.say(self.c.dim(f"  Settings go to {status['config_file']}; passwords and tokens to "
                            f"{secrets_path()}, never into the config."))  # fmt: skip
        form: dict = {}
        form["backend"] = self.backend(settings)
        slack = self.slack(settings)
        if slack:
            form["slack"] = {"webhook": slack}
        form["store"] = self.store(settings)
        every = self.question("How often should `logsite watch` check", settings["watch"]["every"])
        form["watch"] = {"every": every}
        self.app.save(form)
        self.done(form)
        return form

    def backend(self, settings: dict) -> dict:
        self.say()
        self.say(self.c.bold("1/3  Where are your logs?"))
        current = settings["backend"]
        options = [(k, v["label"]) for k, v in BACKENDS.items()]
        kind = self.choose("Choose", options, current["type"])
        same = kind == current["type"]
        defaults = dict(current["values"]) if same else {}
        typed: dict[str, str] = {}  # secrets typed in this run: a retry keeps them
        while True:
            values: dict[str, str] = {}
            for key, label, field_kind, required, placeholder in BACKENDS[kind]["fields"]:
                if field_kind == "secret":
                    kept = typed.get(key) or (same and current["secrets"].get(key))
                    note = " (Enter keeps it)" if kept else ("" if required else " (Enter to skip)")
                    value = self.secret(f"  {label}{note}: ").strip() or typed.get(key, "")
                    if value:
                        typed[key] = value
                else:
                    hint = placeholder if required else "optional"
                    value = self.question(label, str(defaults.get(key, "")), hint)
                if value:
                    values[key] = value
            form = {"type": kind, "values": values}
            self.say("  Testing the connection…")
            result = self.app.test_backend(form)
            if result["ok"]:
                capped = "+" if result.get("errors_capped") else ""
                self.ok(f"connected: {result['errors']}{capped} error(s) in {result['scope']}")
                for line in result["lines"][:3]:
                    self.say(self.c.dim(f"      {(line['time'] or '')[:19]}  {line['message']}"))
                if not result["lines"]:
                    empty = (f"      (no log lines in {result['scope']}: check the selector, "
                             "index or log group if that's unexpected)")  # fmt: skip
                    self.say(self.c.dim(empty))
                return form
            self.fail(result["error"])
            if not self.yes("Change the answers and try again?"):
                if self.yes("Save it anyway?", default=False):
                    return form
                raise SystemExit("logsite init: nothing saved")
            defaults = {k: v for k, v in values.items() if k not in typed}

    def slack(self, settings: dict) -> str | None:
        self.say()
        self.say(self.c.bold("2/3  Slack alerts") + self.c.dim(" (optional)"))
        self.say(self.c.dim("  An incoming webhook for the channel: in Slack, Apps → Incoming "
                            "Webhooks → Add to Slack."))  # fmt: skip
        note = " (saved; Enter keeps it)" if settings["slack"]["saved"] else " (Enter to skip)"
        while True:
            url = self.secret(f"  Webhook URL{note}: ").strip()
            if not url:
                return None
            if not self.yes("Send a test message to the channel?"):
                return url
            result = self.app.test_slack({"webhook": url})
            if result["ok"]:
                self.ok("sent: check the channel")
                return url
            self.fail(result["error"])
            if not self.yes("Try another URL?"):
                return None

    def store(self, settings: dict) -> dict:
        self.say()
        self.say(self.c.bold("3/3  Where should errors be stored?"))
        self.say(self.c.dim("  Enter for SQLite (a file on this machine), or a database URL: "
                            "postgresql://, mysql://, mssql://, clickhouse://"))  # fmt: skip
        saved = settings["store"]["postgres"]
        note = " (saved; Enter keeps it)" if saved else ""
        while True:
            url = self.secret(f"  Database URL{note}: ").strip()
            if not url and saved:
                return {"postgres": True}
            result = self.app.test_store({"url": url})
            if result["ok"]:
                self.ok("connected; the tables are ready" if url else "a SQLite file it is")
                return {"postgres": bool(url), "url": url}
            self.fail(result["error"])
            if not self.yes("Try again?"):
                self.say("  Using SQLite for now.")
                return {"postgres": False}

    def done(self, form: dict) -> None:
        status = self.app.status()
        self.say()
        self.ok(self.c.bold(f"Saved {status['config_file']}"))
        self.say()
        self.say("  Next:")
        self.say("    logsite errors              # the errors in your logs now, grouped by cause")
        if form.get("slack"):
            self.say("    logsite watch --dry-run     # what it would post to Slack")
        self.say("    logsite watch               # keep checking, store every error"
                 + (", post to Slack" if form.get("slack") else ""))  # fmt: skip
        self.say("    logsite db backfill --since 7d   # load the past week into the store")
        self.say("    logsite app                 # the same, in your browser")
