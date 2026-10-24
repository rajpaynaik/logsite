import os
import shutil
import subprocess

import pytest

V1 = """\
import logging

logger = logging.getLogger(__name__)


def charge(order):
    total = order.total
    logger.info("charging order %s", order.id)
    return total


def refund(order):
    logger.info("refunding order %s", order.id)
"""

# Changes the body of charge() but not the log line itself.
V2 = V1.replace("total = order.total", "total = order.total * 2")


def git(repo, *args, date="2026-01-01T00:00:00+00:00"):
    env = {
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.com",
        "GIT_COMMITTER_NAME": "Ada",
        "GIT_COMMITTER_EMAIL": "ada@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "HOME": str(repo),
        "PATH": os.environ["PATH"],
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    """A git repo with app/pay.py committed twice (V1, then V2)."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    git(tmp_path, "init", "-q")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "pay.py").write_text(V1)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "add payments", date="2026-01-01T00:00:00+00:00")
    (tmp_path / "app" / "pay.py").write_text(V2)
    git(tmp_path, "commit", "-qam", "double the total", date="2026-02-01T00:00:00+00:00")
    return tmp_path


@pytest.fixture(autouse=True)
def _history_in_tmp(tmp_path_factory, monkeypatch):
    """Keep the history store (under XDG_DATA_HOME) out of the real home directory."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("data")))


# Throwaway databases to test the store against, besides SQLite: their URLs, when set
DATABASES = {
    "postgres": "LOGSITE_TEST_POSTGRES",  # postgresql://...
    "mysql": "LOGSITE_TEST_MYSQL",  # mysql://...
    "mssql": "LOGSITE_TEST_MSSQL",  # mssql://...?TrustServerCertificate=yes
    "clickhouse": "LOGSITE_TEST_CLICKHOUSE",  # clickhouse://...
}
TABLES = ("error_groups", "error_events", "line_counts", "alerts", "store_meta")


@pytest.fixture(params=["sqlite", *DATABASES])
def store_url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "store.db")
    url = os.environ.get(DATABASES[request.param])
    if not url:
        pytest.skip(f"set {DATABASES[request.param]} to a throwaway database to test it")
    from logsite.store import connect

    store = connect(url)  # start empty
    if request.param == "clickhouse":
        for table in TABLES:
            store.client.command(f"drop table if exists {table}")
    else:
        with store._tx() as tx:
            for table in TABLES:
                tx.ddl(f"drop table if exists {table}")
    store.close()
    return url
