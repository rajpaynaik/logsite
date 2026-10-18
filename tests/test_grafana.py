import json
from datetime import datetime, timezone

import pytest
from conftest import git
from test_backends import stub_server

from logsite.cli import main
from logsite.git import source_url
from logsite.grafana import LokiShape, Queries, build_dashboard, deployed_prefix, level_field
from logsite.index import index_source
from logsite.models import LogRecord

CODE = """import logging
logger = logging.getLogger(__name__)

def handle(req):
    logger.info("request %s %s", req.method, req.path)
"""
SITES = index_source(CODE, "app/api.py")


def rec(**fields):
    return LogRecord(None, "x", fields)


def test_deployed_prefix_and_level_field():
    records = [rec(pathname="/srv/app/api.py"), rec(pathname="/srv/app/api.py"), rec(pathname="x")]
    assert deployed_prefix(records, SITES) == "/srv/"
    assert deployed_prefix([rec(pathname="app/api.py")], SITES) == ""
    assert level_field([rec(levelname="INFO"), rec(levelname="ERROR")]) == "levelname"
    assert level_field([rec(**{"log.level": "info"})]) == "log_level"  # Loki's json naming
    assert level_field([]) is None  # plain text: the level is a word in the line


def test_queries():
    q = Queries(LokiShape('{app="shop"}', prefix="/srv/"))
    assert q.located == (
        '{app="shop"} | json | __error__=`` | pathname!=`` | lineno!=`` '
        '| label_format logsite_file=`{{ trimPrefix "/srv/" .pathname }}`, '
        "logsite_line=`{{ .lineno }}`, "
        'logsite_site=`{{ trimPrefix "/srv/" .pathname }}:{{ .lineno }}`'
    )
    assert q.by_site("count_over_time").startswith(
        "sum by (logsite_site, logsite_file, logsite_line) (count_over_time("
    )
    assert q.monthly_cost().endswith("* 2592000 / $__range_s / 1000000000 * $price_per_gb")
    assert '| level=~"(?i)(error|' in q.errors
    plain = Queries(LokiShape("{a}", parser="logfmt", file_field="f", line_field="l"))
    assert "| logfmt |" in plain.located and "{{ .f }}:{{ .l }}" in plain.located


def test_dashboard():
    d = build_dashboard(
        SITES,
        LokiShape('{app="shop"}'),
        title="shop",
        price_per_gb=0.25,
        source_url="https://github.com/acme/shop/blob/{ref}/{path}#L{line}",
        ref="abc123",
        uid="logsite-shop",
    )
    assert d["uid"] == "logsite-shop" and d["title"] == "shop"
    variables = {v["name"]: v for v in d["templating"]["list"]}
    assert variables["datasource"]["query"] == "loki"
    assert variables["price_per_gb"]["query"] == "0.25"
    panels = {p["title"]: p for p in d["panels"]}
    assert set(panels) == {
        "Log volume", "Log lines", "Estimated cost / month", "Error lines", "Cost by code line",
        "Errors by code line", "Loudest log calls", "Recent errors",
    }  # fmt: skip
    table = panels["Cost by code line"]
    assert all(t["datasource"]["uid"] == "${datasource}" for t in table["targets"])
    props = {p["id"]: p["value"] for p in table["fieldConfig"]["overrides"][0]["properties"]}
    assert props["mappings"][0]["options"]["app/api.py:5"] == {
        "text": "app/api.py:5  info('request %s %s')"
    }
    assert props["links"][0]["url"] == (
        "https://github.com/acme/shop/blob/abc123/${__data.fields.logsite_file}"
        "#L${__data.fields.logsite_line}"
    )
    assert panels["Error lines"]["fieldConfig"]["defaults"] == {"unit": "short"}  # no decimals
    before = panels["Errors by code line"]["targets"][1]["expr"]
    assert "[$__range] offset $__range" in before
    json.dumps(d)  # serializable


@pytest.mark.parametrize(
    "remote, url",
    [
        ("git@github.com:acme/shop.git", "https://github.com/acme/shop/blob/{ref}/{path}#L{line}"),
        ("https://tok@github.com/acme/shop", "https://github.com/acme/shop/blob/{ref}/{path}#L{line}"),
        ("ssh://git@gitlab.com/acme/shop.git", "https://gitlab.com/acme/shop/-/blob/{ref}/{path}#L{line}"),
        ("https://bitbucket.org/acme/shop.git", "https://bitbucket.org/acme/shop/src/{ref}/{path}#lines-{line}"),
    ],
)  # fmt: skip
def test_source_url_from_the_remote(repo, remote, url):
    git(repo, "remote", "add", "origin", remote)
    assert source_url(repo) == url


def test_source_url_without_a_remote(repo):
    assert source_url(repo) is None


def test_cli_grafana(tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text(CODE)
    line = json.dumps(
        {
            "message": "request GET /",
            "pathname": "/srv/app/api.py",
            "lineno": 5,
            "levelname": "INFO",
        }
    )
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1e9))
    payload = {"data": {"result": [{"stream": {}, "values": [[ts, line]]}]}}
    with stub_server(lambda path, body: payload) as (url, _):
        (tmp_path / "logsite.toml").write_text(
            f'[backend]\ntype = "loki"\nurl = "{url}"\nselector = \'{{app="shop"}}\'\n'
            "[cost]\nprice_per_gb = 0.3\n"
        )
        monkeypatch.chdir(tmp_path)
        assert (
            main(
                [
                    "grafana",
                    "--uid",
                    "x",
                    "-o",
                    "dash.json",
                    "--source-url",
                    "https://h/{path}#{line}",
                ]
            )
            == 0
        )
    err = capsys.readouterr().err
    assert "strips '/srv/' from deployed paths" in err and "level field 'levelname'" in err
    d = json.loads((tmp_path / "dash.json").read_text())
    assert d["uid"] == "x" and d["templating"]["list"][1]["query"] == "0.3"
    assert 'trimPrefix "/srv/"' in d["panels"][4]["targets"][0]["expr"]

    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "x.log"\n')
    with pytest.raises(SystemExit, match="reads Loki"):
        main(["grafana"])


def test_generic_dashboard_for_the_catalog(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no logsite.toml, no backend: not needed
    assert main(["grafana", "--generic", "-o", "generic.json"]) == 0
    d = json.loads((tmp_path / "generic.json").read_text())
    names = [v["name"] for v in d["templating"]["list"]]
    assert names == [
        "datasource", "selector", "parser", "file_field", "line_field", "level_field",
        "path_prefix", "repo_url", "price_per_gb",
    ]  # fmt: skip
    assert d["uid"] == "logsite-log-cost" and d["__requires"][1]["id"] == "loki"
    assert (
        d["panels"][0]["type"] == "text"
        and "github.com/rajpaynaik/logsite" in (d["panels"][0]["options"]["content"])
    )
    table = next(p for p in d["panels"] if p["title"] == "Cost by code line")
    expr = table["targets"][0]["expr"]
    assert expr.startswith("sum by (logsite_site, logsite_file, logsite_line) (bytes_over_time(")
    assert (
        "${selector} | ${parser} |" in expr and 'trimPrefix "${path_prefix}" .${file_field}' in expr
    )
    props = {p["id"]: p["value"] for p in table["fieldConfig"]["overrides"][0]["properties"]}
    assert props["mappings"] == []  # nothing from any one project
    assert props["links"][0]["url"] == (
        "${repo_url}/${__data.fields.logsite_file}#L${__data.fields.logsite_line}"
    )
    assert table["gridPos"]["y"] == 9  # below the intro and the totals


def test_committed_catalog_dashboard_is_current():
    """integrations/grafana/logsite-dashboard.json is what --generic writes."""
    from pathlib import Path

    from logsite.grafana import LokiShape

    committed = Path(__file__).parent.parent / "integrations" / "grafana" / "logsite-dashboard.json"
    fresh = build_dashboard(
        [], LokiShape(""), title="Log cost per code line (logsite)", uid="logsite-log-cost",
        generic=True,
    )  # fmt: skip
    assert json.loads(committed.read_text()) == json.loads(json.dumps(fresh)), (
        "regenerate: logsite grafana --generic -o integrations/grafana/logsite-dashboard.json"
    )


def test_error_groups_panel():
    plain = build_dashboard(SITES, LokiShape('{app="shop"}'), title="shop")
    d = build_dashboard(
        SITES, LokiShape('{app="shop"}'), title="shop", error_groups_url="http://logsite:8085/"
    )
    panel = d["panels"][4]
    assert panel["title"] == "Error groups" and panel["gridPos"]["y"] == 4
    (target,) = panel["targets"]
    assert target["datasource"] == {"type": "yesoreyeram-infinity-datasource", "uid": "${logsite}"}
    assert target["url"] == "${logsite_url}/errors" and target["root_selector"] == "groups"
    assert target["url_options"]["params"] == [
        {"key": "from", "value": "${__from}"},
        {"key": "to", "value": "${__to}"},
    ]  # follows the dashboard's time range
    order = panel["transformations"][0]["options"]["indexByName"]
    assert list(order)[:5] == ["Trend", "Count", "Error", "History", "Root cause"]
    assert {"First seen", "Fixed before by"} <= set(order)
    # first seen / gone / came back as annotations; the data source by type (see grafana.py)
    builtin, anno = d["annotations"]["list"]
    assert builtin["builtIn"] == 1 and anno["name"] == "logsite history"
    assert anno["datasource"] == {"type": "yesoreyeram-infinity-datasource"}
    assert anno["target"]["url"] == "${logsite_url}/history/events"
    assert anno["target"]["root_selector"] == "events"
    assert anno["target"]["columns"][0] == {
        "selector": "time",
        "text": "time",
        "type": "timestamp_epoch",
    }
    assert "annotations" not in plain
    links = [o for o in panel["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Raised at"]
    assert links[0]["properties"][0]["value"][0]["url"] == "${__data.fields.raised_url}"
    variables = {v["name"]: v for v in d["templating"]["list"]}
    assert variables["logsite_url"]["query"] == "http://logsite:8085"  # trailing / dropped
    assert variables["logsite"]["query"] == "yesoreyeram-infinity-datasource"
    # everything below the totals moved down to make room
    before = {p["title"]: p["gridPos"]["y"] for p in plain["panels"]}
    after = {p["title"]: p["gridPos"]["y"] for p in d["panels"] if p["title"] != "Error groups"}
    assert all(after[t] == y + (9 if y >= 4 else 0) for t, y in before.items())
    generic = build_dashboard(
        [], LokiShape(""), title="g", generic=True, error_groups_url="http://x"
    )
    assert any(r["id"] == "yesoreyeram-infinity-datasource" for r in generic["__requires"])


def test_plain_text_records_match_the_level_in_the_line():
    q = Queries(LokiShape('{app="orders"}', plain_text=True))
    assert q.errors == '{app="orders"} |~ "\\\\b(ERROR|CRITICAL|FATAL)\\\\b"'
