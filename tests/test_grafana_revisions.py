"""A mixed-version rollout: two releases log from the same file and line, but it's a
different log call in each. The cost dashboard must give each release's row the message and
the link of the call that release ran, and keep lines of unknown releases apart."""

import json
import re
import subprocess
from datetime import datetime, timezone

from conftest import git
from test_backends import stub_server

from logsite.cli import main

V1 = """import logging
logger = logging.getLogger(__name__)

def charge(order):
    logger.info("charged order %s", order)
"""
# v2 adds a warning above it: line 5 is now another log call
V2 = """import logging
logger = logging.getLogger(__name__)

def charge(order):
    logger.warning("refund failed for %s", order)
    logger.info("charged order %s", order)
"""
SOURCE_URL = "https://code.example/{ref}/{path}#L{line}"


def record(version, message, line=5):
    fields = {"message": message, "pathname": "/srv/app/api.py", "lineno": line,
              "levelname": "INFO"}  # fmt: skip
    if version is not None:
        fields["service"] = {"version": version}  # Loki's json parser: service_version
    return json.dumps(fields)


def dashboard(tmp_path, monkeypatch, capsys, lines):
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    payload = {"data": {"result": [{"stream": {}, "values": [[ts, x] for x in lines]}]}}
    with stub_server(lambda path, body: payload) as (url, _):
        (tmp_path / "logsite.toml").write_text(
            f'[backend]\ntype = "loki"\nurl = "{url}"\nselector = \'{{app="shop"}}\'\n'
        )
        monkeypatch.chdir(tmp_path)
        args = ["grafana", "-o", "dash.json", "--source-url", SOURCE_URL]
        assert main(args) == 0
    return json.loads((tmp_path / "dash.json").read_text()), capsys.readouterr().err


def project(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text(V1)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "Charge orders")
    git(tmp_path, "tag", "v1.0")
    (tmp_path / "app" / "api.py").write_text(V2)
    git(tmp_path, "commit", "-qam", "Warn on failed refunds")
    git(tmp_path, "tag", "v2.0")


def panel(d, title):
    return next(p for p in d["panels"] if p.get("title") == title)


def labels(expr: str) -> dict[str, str]:
    """The label_format templates of a LogQL query: name -> template."""
    stage = expr.split("| label_format ", 1)[1].split(" [", 1)[0]
    return dict(re.findall(r"(\w+)=`([^`]*)`", stage))


def evaluate(template: str, fields: dict[str, str]) -> str:
    """Loki's label_format for the shapes the dashboard writes: ``{{ .f }}``, ``trimPrefix``
    and an ``if eq .f "v" }}x{{ else if ... }}y{{ end }}`` chain."""
    chain = re.findall(r'\{\{ (?:if|else if) eq \.(\w+) "([^"]*)" \}\}([^{]*)', template)
    if chain:
        return next((out for f, v, out in chain if fields.get(f) == v), "")
    template = re.sub(r'\{\{ trimPrefix "([^"]*)" \.(\w+) \}\}',
                      lambda m: fields[m[2]].removeprefix(m[1]), template)  # fmt: skip
    return re.sub(r"\{\{ \.(\w+) \}\}", lambda m: fields.get(m[1], ""), template)


def click(d, record_fields: dict[str, str]) -> tuple[str, str]:
    """What Grafana shows in the cost table for a record, and where clicking it goes."""
    table = panel(d, "Cost by code line")
    row = {k: evaluate(t, record_fields) for k, t in labels(table["targets"][0]["expr"]).items()}
    call = next(o for o in table["fieldConfig"]["overrides"]
                if o["matcher"]["options"] == "logsite_key")  # fmt: skip
    props = {p["id"]: p["value"] for p in call["properties"]}
    shown = props["mappings"][0]["options"][row["logsite_key"]]["text"]
    url = props["links"][0]["url"]
    for name, value in row.items():
        url = url.replace(f"${{__data.fields.{name}}}", value)
    return shown, url


def code_at(tmp_path, url: str) -> str:
    """The source line a link opens."""
    ref, path, line = re.fullmatch(r"https://code\.example/(\w+)/(.+)#L(\d+)", url).groups()
    text = subprocess.run(["git", "-C", str(tmp_path), "show", f"{ref}:{path}"],
                          capture_output=True, text=True, check=True).stdout  # fmt: skip
    return text.splitlines()[int(line) - 1].strip()


def test_each_release_row_opens_the_call_that_wrote_it(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    lines = [record("1.0", "charged order 7")] * 3  # the replica still on 1.0
    lines += [record("2.0", "refund failed for 7")] * 2  # the replica already on 2.0
    lines += [record(None, "charged order 8"), record("3.0", "charged order 9")]
    d, err = dashboard(tmp_path, monkeypatch, capsys, lines)
    assert "releases from 'service_version': 1.0 (" in err and "2.0 (" in err

    base = {"pathname": "/srv/app/api.py", "lineno": "5"}
    shown, url = click(d, {**base, "service_version": "1.0"})
    assert shown == "app/api.py:5  info('charged order %s')"
    assert code_at(tmp_path, url) == 'logger.info("charged order %s", order)'

    shown, url = click(d, {**base, "service_version": "2.0"})
    assert shown == "app/api.py:5  warning('refund failed for %s')"
    assert code_at(tmp_path, url) == 'logger.warning("refund failed for %s", order)'

    # totals are per release first: the two calls at app/api.py:5 aren't added together
    table = panel(d, "Cost by code line")
    for target in table["targets"]:
        assert "sum by (logsite_rev, logsite_ref, logsite_key," in target["expr"]
        assert "| logsite_ref!=``" in target["expr"]  # releases with their own code only

    # no release, or one git doesn't know (3.0): apart, as logged, no message or link
    unknown = panel(d, "Cost by code line: unknown release")
    for target in unknown["targets"]:
        assert "| logsite_ref=``" in target["expr"]
    props = [p["id"] for o in unknown["fieldConfig"]["overrides"] for p in o["properties"]]
    assert "links" not in props
    assert "it knows 1.0, 2.0" in unknown["description"]
    for version in (None, "3.0"):
        fields = {**base, **({"service_version": version} if version else {})}
        assert evaluate(labels(unknown["targets"][0]["expr"])["logsite_ref"], fields) == ""


def test_without_a_release_field_it_says_links_are_todays(tmp_path, monkeypatch, capsys):
    project(tmp_path)
    d, err = dashboard(tmp_path, monkeypatch, capsys, [record(None, "charged order 7")])
    assert "no release field in the records" in err
    assert not any(p.get("title") == "Cost by code line: unknown release" for p in d["panels"])
    table = panel(d, "Cost by code line")
    assert "logsite_rev" not in table["targets"][0]["expr"]
    # what the dashboard did before releases: a 1.0 line at app/api.py:5 shown as 2.0's call
    call = next(o for o in table["fieldConfig"]["overrides"]
                if o["matcher"]["options"] == "logsite_site")  # fmt: skip
    mapping = next(p for p in call["properties"] if p["id"] == "mappings")["value"][0]
    assert mapping["options"]["app/api.py:5"]["text"].startswith("app/api.py:5  warning(")
