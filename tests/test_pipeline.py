import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from logsite.cli import main
from logsite.cost import CostConfig
from logsite.index import index_source
from logsite.models import Usage
from logsite.pipeline import (
    MULTILINE_FIRSTLINE,
    PipelineConfig,
    location_regex,
    make_plan,
    re2_escape,
    template_regex,
    to_alloy,
)

CODE = """import logging
logger = logging.getLogger(__name__)

def handle(req):
    logger.info("request %s %s", req.method, req.path)

def health():
    logger.debug("health ok")

def charge(order):
    logger.error("payment failed for order %s", order.id)

def tick():
    logger.info("t %s", 1)
"""
SITES = index_source(CODE, "app/api.py")
USAGE = {
    "app/api.py:5": Usage(2000, 300_000),
    "app/api.py:8": Usage(500, 50_000),
    "app/api.py:11": Usage(30, 4_000),
}
PRICED = CostConfig(price_per_gb=0.5)


def plan(**cfg):
    return make_plan(SITES, USAGE, PipelineConfig(**cfg), PRICED, window_hours=24)


def alloy(p, cfg=None, **kw):
    args = {
        "match": "location",
        "format": "json",
        "file_field": "pathname",
        "line_field": "lineno",
        "selector": '{app="shop"}',
        "generated_by": "test",
    }
    return to_alloy(p, cfg or PipelineConfig(), PRICED, **{**args, **kw})


def test_config_is_validated():
    assert PipelineConfig.from_dict({"sample": {"app/api.py:5": 0.1}}).sample == {
        "app/api.py:5": 0.1
    }
    with pytest.raises(ValueError, match="unknown"):
        PipelineConfig.from_dict({"dorp": []})
    with pytest.raises(ValueError, match="between 0 and 1"):
        PipelineConfig.from_dict({"sample": {"app/api.py:5": 2}})
    with pytest.raises(ValueError, match="match"):
        PipelineConfig.from_dict({"match": "magic"})


def test_plan_from_config_and_volume():
    p = plan(drop_levels=["debug"], max_per_window=200)
    got = [(r.site.id, r.action, r.rate, r.reason) for r in p.rules]
    assert got == [
        ("app/api.py:5", "sample", 0.1, "2,000 per 24h, over 200"),
        ("app/api.py:8", "drop", None, "debug level"),
    ]
    sample = p.rules[0]
    # 300 kB a day, 30 days, 90% gone
    assert sample.saved_bytes_per_month == pytest.approx(300_000 * 30 * 0.9)
    assert sample.saved_per_month == pytest.approx(300_000 * 30 * 0.9 / 1e9 * 0.5)


def test_errors_and_unknown_lines_never_get_a_rule():
    p = plan(drop=["app/api.py:11", "app/nope.py:1"], sample={"app/api.py:5": 0.25})
    assert [(r.site.id, r.rate) for r in p.rules] == [("app/api.py:5", 0.25)]
    assert p.skipped == [
        ("app/api.py:11", "error lines are never dropped or sampled"),
        ("app/nope.py:1", "not a log call in this project"),
    ]


def test_rates_are_rounded():
    usage = {"app/api.py:5": Usage(30_000, 1)}
    p = make_plan(SITES, usage, PipelineConfig(max_per_window=1000), PRICED, 24)
    assert p.rules[0].rate == 0.033


def test_re2_escape_never_escapes_spaces():
    assert re2_escape("order (id) 3.5 ok") == "order \\(id\\) 3[.]5 ok"
    site = SITES[0]
    assert location_regex(site) == "(.*/)?app/api[.]py:5"
    assert template_regex(site) == "request .*? .*?"
    assert template_regex(SITES[3]) is None  # "t %s": too little fixed text


def test_alloy_by_location():
    text = alloy(plan(drop_levels=["debug"], max_per_window=200))
    assert 'loki.process "logsite" {' in text
    assert 'logsite_file = "pathname",' in text and "stage.json {" in text
    assert 'values = { logsite_site = "" }' in text
    assert (
        'selector = "{logsite_site=~\\"(.*/)?app/api[.]py:5\\"} '
        '!~ \\"(?i)(error|exception|traceback|critical|fatal)\\""'
    ) in text  # the error guard here too: the line can still be an error at runtime
    assert "rate                = 0.1" in text
    assert 'action              = "drop"' in text
    assert 'values = ["logsite_site"]' in text  # the helper label never reaches Loki
    assert "// keep 1 in 10 (2,000 per 24h, over 200)" in text
    assert "logfmt" not in text
    assert "stage.logfmt {\n    mapping = {" in alloy(plan(max_per_window=200), format="logfmt")


def test_alloy_by_template_guards_error_lines():
    text = alloy(plan(sample={"app/api.py:5": 0.5, "app/api.py:14": 0.5}), match="template")
    assert (
        'selector = "{app=\\"shop\\"} |~ \\"request .*? .*?\\" '
        '!~ \\"(?i)(error|exception|traceback|critical|fatal)\\""'
    ) in text
    assert "app/api.py:14: skipped, its message has too little fixed text" in text
    assert "stage.labels" not in text
    with pytest.raises(ValueError, match="selector"):
        alloy(plan(max_per_window=1), match="template", selector=None)


ALLOY = os.environ.get("LOGSITE_ALLOY") or shutil.which("alloy")


@pytest.mark.skipif(not ALLOY, reason="set LOGSITE_ALLOY to an alloy binary to check syntax")
def test_alloy_accepts_the_config(tmp_path):
    for match in ("location", "template"):
        path = tmp_path / f"{match}.alloy"
        p = plan(drop_levels=["debug"], max_per_window=200)
        path.write_text(alloy(p, PipelineConfig(multiline=True), match=match))
        proc = subprocess.run([ALLOY, "fmt", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_cli_pipeline(tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text(CODE)
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        {"timestamp": now, "message": "request GET /", "pathname": "/srv/app/api.py", "lineno": 5}
    ] * 5
    lines += [
        {"timestamp": now, "message": "health ok", "pathname": "/srv/app/api.py", "lineno": 8}
    ]
    (tmp_path / "prod.log").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "prod.log"\n'
        '[pipeline]\ndrop_levels = ["debug"]\nmax_per_window = 2\n'
    )
    monkeypatch.chdir(tmp_path)
    assert main(["pipeline", "-o", "rules.alloy"]) == 0
    err = capsys.readouterr().err
    assert "keep 0.4" in err and "drop" in err and "written to rules.alloy" in err
    text = (tmp_path / "rules.alloy").read_text()
    assert "matches lines by location" in text  # every record has file + line
    assert main(["pipeline", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(r["id"], r["action"]) for r in data["rules"]] == [
        ("app/api.py:5", "sample"),
        ("app/api.py:8", "drop"),
    ]
    assert data["match"] == "location"


def test_only_known_low_levels_get_a_rule():
    # a level set at runtime could be an error: fail closed, even when asked explicitly
    code = (
        "import logging\nlog = logging.getLogger(__name__)\n\n\n"
        "def f(lvl):\n    log.log(lvl, 'payment retry failed for %s', 1)\n"
    )
    sites = index_source(code, "app/retry.py")
    (site,) = sites
    assert site.level == "log"
    busy = {site.id: Usage(50_000, 5_000_000)}
    for cfg in (PipelineConfig(max_per_window=1000), PipelineConfig(drop=[site.id])):
        p = make_plan(sites, busy, cfg, PRICED, 24)
        assert p.rules == []
        assert p.skipped == [
            (site.id, "its level is set at runtime (it could be an error), so it's never touched")
        ]
    # drop_levels can't make warning or error lines eligible either
    p = plan(drop_levels=["warning", "error"])
    assert all(r.site.level in ("debug", "info") for r in p.rules)


def test_multiline_joins_stack_traces_before_the_rules():
    text = alloy(plan(drop_levels=["debug"]), PipelineConfig(multiline=True))
    assert "stage.multiline {" in text and "max_lines     = 500" in text
    assert text.index("stage.multiline") < text.index("stage.json")  # first: before parsing
    assert "stack traces joined" in text
    assert "stage.multiline" not in alloy(plan())  # opt-in


def test_multiline_firstline_starts_records_and_continues_traces():
    import re

    first = re.compile(MULTILINE_FIRSTLINE)
    starts = [
        "2026-10-18 14:22:31.812 ERROR [stock-service] StockRepository - failed",
        "2026-10-18T14:22:31.812Z  INFO 1 --- [main] c.e.App : started",
        '{"ts": "2026-10-18T14:22:31Z", "level": "error"}',
        "Oct 18 14:22:31 host app[1]: started",
        "level=error msg=failed",
        "E1018 14:22:31.123456 1 main.go:10] failed",
        "panic: runtime error: index out of range",
    ]
    continues = [
        "\tat com.example.stock.StockRepository.find(StockRepository.java:54)",
        "Caused by: org.postgresql.util.PSQLException: ERROR: deadlock detected",
        "\t... 12 more",
        "org.springframework.dao.QueryTimeoutException: PreparedStatementCallback",
        "  Detail: Process 421 waits for ShareLock on transaction 812",
        "Traceback (most recent call last):",
        '  File "/srv/app/views.py", line 40, in place_order',
        "    at Object.<anonymous> (/srv/app/index.js:3:9)",
        "goroutine 1 [running]:",
    ]
    assert [x for x in starts if not first.match(x)] == []
    assert [x for x in continues if first.match(x)] == []


def test_cli_pipeline_multiline_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text(CODE)
    (tmp_path / "prod.log").write_text("")
    (tmp_path / "logsite.toml").write_text('[backend]\ntype = "file"\npath = "prod.log"\n')
    monkeypatch.chdir(tmp_path)
    # no drop or sample rules, and no stream selector: joining traces needs neither
    assert main(["pipeline", "--multiline", "-o", "join.alloy"]) == 0
    assert "joins stack traces only" in capsys.readouterr().err
    assert "stage.multiline {" in (tmp_path / "join.alloy").read_text()
