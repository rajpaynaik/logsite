import json
from datetime import datetime, timedelta, timezone

from test_incidents import T0, group

from logsite.cli import main
from logsite.diagnose import diagnose, diagnosis_json
from logsite.git import Commit
from logsite.history import GroupHistory
from logsite.incidents import Incident
from logsite.lint import LintConfig

CFG = LintConfig(spike_ratio=2, spike_min_count=10)
TIMEOUT = "java.net.SocketTimeoutException: Read timed out"


def incident(cause, *, deploy_min=None, suspects=(), effects=()):
    inc = Incident([cause, *effects])
    if deploy_min is not None:
        inc.deploy = ("v1.4", T0 - timedelta(minutes=deploy_min))
    inc.suspects = list(suspects)
    return inc


COMMIT = Commit("0df4dac1aaaa", "Maya", T0 - timedelta(hours=2), "Lower carrier read timeout to 3s")


def test_a_change_right_before_and_touching_the_code_is_the_lead():
    cause = group("c", 0, previous=0, exc="IllegalStateException", raised="a/Carrier.java:58")
    cause.causes, cause.origin = [TIMEOUT], "java.net"
    d = diagnose(incident(cause, deploy_min=4, suspects=[COMMIT]), CFG)
    change, dependency = d.hypotheses
    assert change.key == "change" and dependency.key == "dependency"
    assert change.title == "The change in v1.4: 0df4dac1 Lower carrier read timeout to 3s"
    assert change.supporting == [
        "v1.4 was deployed 4 min before the errors started",
        "0df4dac1 changed the lines around a/Carrier.java:58, where it's raised",
        "the error wasn't there in the window before",
    ]
    assert change.next_check.startswith("read `git show 0df4dac1 -- a/Carrier.java`")
    assert dependency.against == ["but a recent commit changed the code that makes the call"]
    assert "slow for every caller" in dependency.next_check
    assert d.strength == "strong"
    assert d.unknown == ["traffic before and after isn't known, so rates couldn't be compared"]


def test_a_regression_says_the_change_may_undo_the_fix():
    cause = group("c", 0, previous=0, exc="ValueError", raised="a.py:7")
    cause.history = GroupHistory(known=True, regression=True, fixed_by=[{"sha": "41bdce40"}])
    (change,) = diagnose(incident(cause, deploy_min=4, suspects=[COMMIT]), CFG).hypotheses
    assert "it was fixed before (41bdce40) and came back: the change may undo that fix" in (
        change.supporting
    )


def test_same_rate_at_three_times_the_traffic_is_traffic():
    cause = group("c", 0, count=300, previous=100, exc="ValueError", raised="a.py:7")
    cause.traffic_ratio = 3.0
    d = diagnose(incident(cause, deploy_min=600, suspects=[COMMIT]), CFG, traffic_ratio=3.0)
    traffic, change = d.hypotheses
    assert traffic.key == "traffic" and traffic.score == 5
    assert "the error rate didn't change (1.0x): the errors grew with the traffic" in (
        traffic.supporting
    )
    assert "the error was already there at the same rate before (1.0x)" in change.against
    assert diagnosis_json(d)["hypotheses"][0]["cause"] == "traffic"


def test_nothing_to_go_on_is_unclear():
    cause = group("c", 0, previous=0, exc="ValueError", raised="a.py:7")
    d = diagnose(incident(cause), CFG)
    assert d.hypotheses == [] and d.strength == "unclear"


# --- end to end: traffic in the log -------------------------------------------------------------


def project(tmp_path, before_errors, now_errors, before_info=1000, now_info=3000):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lines = []
    for start, info, errors in ((now - timedelta(hours=30), before_info, before_errors),
                                (now - timedelta(hours=6), now_info, now_errors)):  # fmt: skip
        for i in range(info):
            t = start + timedelta(seconds=i)
            lines.append(f"{t:%Y-%m-%d %H:%M:%S} INFO [shop] c.e.Cart : added item {i}")
        for i in range(errors):
            t = start + timedelta(seconds=i)
            lines.append(f"{t:%Y-%m-%d %H:%M:%S} ERROR [shop] c.e.Cart : discount failed")
            lines.append("java.lang.IllegalArgumentException: unknown discount code")
            lines.append("\tat com.example.Cart.apply(Cart.java:3)")
    (tmp_path / "app.log").write_text("\n".join(lines) + "\n")
    (tmp_path / "logsite.toml").write_text(
        '[backend]\ntype = "file"\npath = "app.log"\n\n'
        "[lint]\nspike_ratio = 2\nspike_min_count = 10\n\n[history]\nenabled = false\n"
    )


def test_three_times_the_errors_at_three_times_the_traffic_isnt_a_spike(
    tmp_path, monkeypatch, capsys
):
    project(tmp_path, before_errors=20, now_errors=60)
    monkeypatch.chdir(tmp_path)
    assert main(["errors", "--format", "json"]) == 0
    (g,) = json.loads(capsys.readouterr().out)["groups"]
    assert g["trend"] == "3.0x" and g["traffic_ratio"] == 3.0 and g["rate_ratio"] == 1.0
    assert main(["errors", "--color", "never", "--fail-on-spike"]) == 0  # not a spike
    out = capsys.readouterr().out
    assert "traffic 3.0x" in out.splitlines()[0]
    assert "Trend        3.0x errors at 3.0x traffic: the rate is 1.0x, it grew with traffic" in out
    assert main(["investigate", "--report"]) == 0
    assert "No incident: nothing new or spiking" in capsys.readouterr().out


def test_report_when_the_rate_really_rose(tmp_path, monkeypatch, capsys):
    project(tmp_path, before_errors=20, now_errors=600)
    monkeypatch.chdir(tmp_path)
    assert main(["investigate", "--report", "-o", "report.md"]) == 0
    text = (tmp_path / "report.md").read_text()
    assert text.startswith("# Incident report: errors in the last 24h")
    assert "traffic 3.0x the window before" in text
    assert "- 600 errors (30.0x, rate 10.0x)" in text
    assert "the error rate is 10.0x: more than the traffic explains" in text
    assert "they aren't probabilities" in text
    assert main(["investigate", "--format", "json"]) == 0
    (inc,) = json.loads(capsys.readouterr().out)["incidents"]
    assert inc["diagnosis"]["hypotheses"][0]["cause"] == "traffic"
    assert inc["diagnosis"]["strength"] == "unclear"
    report = (tmp_path / "report.md").read_text()
    assert "### Possible causes (the evidence is unclear)" in report
    assert "**1. More traffic, not a new failure** (unlikely, score 0)" in report
    assert main(["investigate", "--color", "never"]) == 0
    assert "Likely       unclear: not enough evidence to rank a cause" in capsys.readouterr().out
