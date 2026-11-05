"""The editor's code lens and hover show how long a log call's operation took."""

from types import SimpleNamespace

import pytest

pytest.importorskip("pygls")

from lsprotocol import types  # noqa: E402

from logsite.config import load_config  # noqa: E402
from logsite.latency import Timing  # noqa: E402
from logsite.server import Project, code_lens, hover  # noqa: E402

SOURCE = 'import logging\nlog = logging.getLogger()\nlog.info("charged %s", order)\n'


class FakeServer:
    def __init__(self, project, path):
        self._found = (project, path)
        doc = SimpleNamespace(source=SOURCE, lines=SOURCE.splitlines(keepends=True))
        self.workspace = SimpleNamespace(get_text_document=lambda uri: doc)

    def project_for(self, uri):
        return self._found


@pytest.fixture
def server(tmp_path):
    project = Project(None, load_config(tmp_path))
    project.counts, project.previous_counts = {"app.py:3": 1200}, {"app.py:3": 1100}
    project.timings = {"app.py:3": Timing(1200, 0.04, 0.18, 0.31, 0.9)}
    project.counts_ready = True
    project.history = lambda path, line: []
    return FakeServer(project, "app.py")


def test_code_lens_shows_p95(server):
    params = types.CodeLensParams(text_document=types.TextDocumentIdentifier(uri="file:///app.py"))
    (lens,) = code_lens(server, params)
    assert lens.command.title == "$(graph) 1.2k in 24h · p95 180ms"


def test_hover_shows_the_percentiles(server):
    params = types.HoverParams(text_document=types.TextDocumentIdentifier(uri="file:///app.py"),
                               position=types.Position(line=2, character=4))  # fmt: skip
    text = hover(server, params).contents.value
    assert "Took p50 40ms · p95 180ms · p99 310ms · max 900ms (1,200 timed in 24h)" in text


def test_no_timings_no_p95(server):
    server._found[0].timings = {}
    params = types.CodeLensParams(text_document=types.TextDocumentIdentifier(uri="file:///app.py"))
    (lens,) = code_lens(server, params)
    assert "p95" not in lens.command.title
