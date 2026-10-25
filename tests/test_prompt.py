"""logsite prompt: one stored error as a case file for an AI assistant."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import git

from logsite.cli import main
from logsite.errors import ErrorGroup
from logsite.prompt import fold_library_frames, shared_fields
from logsite.store import connect

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FIRST = NOW - timedelta(hours=30)

CODE = """\
import json


def charge(order):
    rate = RATES[order["currency"]]
    return order["amount"] * rate
"""

TRACEBACK = """\
Traceback (most recent call last):
  File "/srv/app/payments.py", line 5, in charge
    rate = RATES[order["currency"]]
  File "/usr/local/lib/python3.12/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
               ^^^^^^^^^^^^^^^
  File "/srv/venv/lib/python3.12/site-packages/requests/api.py", line 59, in get
    return request("get", url, params=params, **kwargs)
KeyError: 'JPY'"""


class Group(ErrorGroup):
    def __init__(self, fingerprint, first, last, title="KeyError: 'JPY'"):
        super().__init__(fingerprint, title, "KeyError", [title])
        self.first_seen, self.last_seen = first, last
        self.raised_at, self.raised_in = "payments.py:5", "charge"


def event(i, gid, at, **fields):
    return {"event_key": f"e{gid}{i}", "group_id": gid, "at": at.isoformat(),
            "service": "payments", "level": "error", "release": "v2.1", "trace_id": f"t{i}",
            "message": "charge failed", "traceback": TRACEBACK,
            "fields": json.dumps({"logger": "app", "lineno": 9, **fields})}  # fmt: skip


@pytest.fixture
def project(tmp_path):
    (tmp_path / "payments.py").write_text(CODE)
    (tmp_path / "logsite.toml").write_text(
        f'[backend]\ntype = "file"\npath = "app.log"\n\n'
        f'[store]\nurl = "sqlite:///{tmp_path / "store.db"}"\n\n'
        f'[history]\npath = "{tmp_path / "history.db"}"\n'
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "Payments", date=(FIRST - timedelta(days=10)).isoformat())
    edited = CODE.replace('["currency"]]', '["currency"]]  # all')
    (tmp_path / "payments.py").write_text(edited)
    git(tmp_path, "commit", "-qam", "Look rates up directly",
        date=(FIRST - timedelta(hours=5)).isoformat())  # fmt: skip
    store = connect(f"sqlite:///{tmp_path / 'store.db'}")
    g = Group("abcd1234ef", FIRST, NOW - timedelta(hours=1))
    rows = [event(i, "abcd1234", FIRST + timedelta(hours=i % 30), currency="JPY",
                  sku=f"BK-{i}", amount=10 + i) for i in range(12)]  # fmt: skip
    rows += [event(20 + i, "abcd1234", NOW - timedelta(hours=2, minutes=i), currency="JPY",
                   sku=f"BK-{i}", amount=5) for i in range(3)]  # fmt: skip
    other = Group("abff0000aa", FIRST, FIRST, "TimeoutError: timed out")
    store.write([g, other], rows + [event(0, "abff0000", FIRST)])
    store.close()
    return tmp_path


def test_the_case_file(project, capsys):
    assert main(["--config-dir", str(project), "prompt", "abcd"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Production error: KeyError: 'JPY'")
    # where, with the code around it
    assert "**Raised at:** `payments.py:5` in `charge()`" in out
    assert '> 5 |     rate = RATES[order["currency"]]  # all' in out
    # how often, by service and release
    assert "**15** times" in out and "`payments` (15)" in out and "`v2.1` (15)" in out
    assert "Last 24 hours: **" in out
    # when it started: the release and the commit just before
    assert "First release with it, from the logs: `v2.1`" in out
    assert "Look rates up directly" in out and "Payments" not in out.split("## Examples")[0]
    # what the failing requests share, and what varies
    assert "`currency` = `JPY` in 15 of 15" in out
    assert "`sku` (12 values)" in out
    assert "`logger`" not in out.split("## Examples")[0]  # noise isn't a finding
    # an example, with the library frames folded
    assert "... 2 library frame(s)" in out and "site-packages" not in out
    assert "`logsite repro abcd1234 --write-test`" in out
    assert out.rstrip().endswith("a retry of failed requests).")


def test_an_existing_reproduction_test_is_named(project, capsys):
    (project / "tests").mkdir()
    (project / "tests" / "test_repro_abcd1234.py").write_text("def test_it(): ...\n")
    main(["--config-dir", str(project), "prompt", "abcd1234"])
    assert "`tests/test_repro_abcd1234.py` reproduces it" in capsys.readouterr().out


def test_json(project, capsys):
    main(["--config-dir", str(project), "prompt", "abcd1234", "--format", "json"])
    case = json.loads(capsys.readouterr().out)
    assert case["group"]["id"] == "abcd1234" and case["sampled"] == 15
    assert case["started"]["commits"][0]["summary"] == "Look rates up directly"
    assert case["releases"] == [{"name": "v2.1", "n": 15, "first_at": case["releases"][0][
        "first_at"], "last_at": case["releases"][0]["last_at"]}]  # fmt: skip


def test_no_id_lists_the_recent_errors(project, capsys):
    assert main(["--config-dir", str(project), "prompt"]) == 2
    err = capsys.readouterr().err
    assert "abcd1234" in err and "abff0000" in err and "TimeoutError" in err


def test_an_ambiguous_or_unknown_id(project, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--config-dir", str(project), "prompt", "ab"])
    assert "2 stored errors start with 'ab'" in str(exc.value)
    assert "abff0000  TimeoutError" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--config-dir", str(project), "prompt", "zz"])
    assert "no stored error with id 'zz'" in str(exc.value)


def test_shared_fields():
    rows = [{"currency": "JPY", "user": {"plan": "free"}, "sku": f"s{i}", "msg": "x"}
            for i in range(9)]  # fmt: skip
    rows.append({"currency": "USD", "user": {"plan": "free"}, "sku": "s9"})
    rows.append("not json")
    shared = shared_fields(rows)
    assert shared["occurrences"] == 10
    assert {(f["field"], f["value"], f["count"]) for f in shared["same"]} == {
        ("currency", "JPY", 9), ("user.plan", "free", 10)}  # fmt: skip
    assert [f["field"] for f in shared["varies"]] == ["sku"]
    assert shared_fields([{"a": 1}, {"a": 1}])["same"] == []  # too few to say


def test_fold_library_frames():
    lines = TRACEBACK.splitlines()
    folded = fold_library_frames(lines)
    assert folded == lines[:3] + ["  ... 2 library frame(s)", "KeyError: 'JPY'"]
    assert fold_library_frames(["KeyError: 'x'"]) == ["KeyError: 'x'"]
