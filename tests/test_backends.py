import json
import re
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from logsite.backends import (
    CloudWatchBackend,
    FileBackend,
    LokiBackend,
    OpenSearchBackend,
    TimeRange,
    from_config,
    opensearch,
)
from logsite.index import index_project

DEMO = Path(__file__).parent.parent / "examples" / "demo_app"


def test_file_backend_counts_demo_app():
    sites = index_project(DEMO)
    counts = FileBackend(DEMO / "sample.log").count_by_line(sites, TimeRange.last(24 * 365 * 10))
    assert counts == {
        "app/checkout.py:7": 2,
        "app/checkout.py:15": 3,
        "app/checkout.py:11": 1,
        "app/checkout.py:13": 1,
        "app/checkout.py:28": 1,
    }


def test_loki_parse_response_merges_labels():
    payload = {
        "data": {
            "result": [
                {
                    "stream": {"app": "checkout"},
                    "values": [
                        ["1760000000000000000", '{"msg": "hi", "lineno": 3}'],
                        ["1760000000000000001", "plain"],
                    ],
                }
            ]
        }
    }
    recs = LokiBackend.parse_response(payload)
    assert recs[0].message == "hi" and recs[0].fields["app"] == "checkout"
    assert recs[0].source_line == 3
    assert recs[1].message == "plain"


def test_loki_query_builder():
    b = LokiBackend("http://loki:3100/", '{app="x"}')
    assert b.build_query('say "hi"') == '{app="x"} |= "say \\"hi\\""'


def _hit(source):
    return {"_index": "logs", "_source": source}


OS_PAYLOAD = {
    "hits": {
        "hits": [
            # ECS (ecs-logging-python): nested objects, line info under log.origin
            _hit(
                {
                    "@timestamp": "2026-10-14T10:00:00Z",
                    "message": "charging order 41 for 1999 cents",
                    "log": {"origin": {"file": {"name": "checkout.py", "line": 7}}},
                    "service": {"name": "checkout"},
                }
            ),
            # The message itself is a JSON log line
            _hit({"@timestamp": "2026-10-14T10:00:01Z", "message": '{"msg": "hi", "lineno": 3}'}),
            _hit({"@timestamp": "2026-10-14T10:00:02Z"}),  # no message: skipped
        ]
    }
}


def test_opensearch_parse_response():
    recs = OpenSearchBackend.parse_response(OS_PAYLOAD)
    assert len(recs) == 2
    assert recs[0].source_file == "checkout.py" and recs[0].source_line == 7
    assert recs[0].fields["service.name"] == "checkout"
    assert recs[0].timestamp.year == 2026
    assert recs[1].message == "hi" and recs[1].source_line == 3


@contextmanager
def stub_server(respond):
    """Local HTTP server; ``respond(path, body) -> dict`` builds each JSON reply.

    Yields ``(base_url, requests)``; requests collects ``(path, headers, body)``.
    """
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, body):
            requests.append((self.path, dict(self.headers), body))
            out = json.dumps(respond(self.path, body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self):
            self._reply(None)

        def do_POST(self):
            self._reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/", requests
    finally:
        httpd.shutdown()


def test_opensearch_query_against_a_live_http_server():
    with stub_server(lambda path, body: OS_PAYLOAD) as (url, requests):
        b = from_config(
            {
                "type": "elasticsearch",
                "url": url,
                "index": "logs-*",
                "query": "service.name:checkout",
                "api_key": "k",
            }
        )
        recs = b.query_logs(TimeRange.last(1), contains="charging", limit=10)
    assert len(recs) == 2
    ((path, headers, body),) = requests
    assert path == "/logs-*/_search" and headers["Authorization"] == "ApiKey k"
    assert body["size"] == 10 and body["sort"] == [{"@timestamp": {"order": "desc"}}]
    filters = body["query"]["bool"]["filter"]
    assert "range" in filters[0] and filters[1] == {
        "query_string": {"query": "service.name:checkout"}
    }
    assert filters[2] == {"match_phrase": {"message": "charging"}}


def test_opensearch_aggregate_pages_and_falls_back_for_unlocated_docs(monkeypatch):
    monkeypatch.setattr(opensearch, "AGG_PAGE", 2)
    pages = [
        [("/srv/app/checkout.py", 7, 7000), ("/srv/app/checkout.py", 15, 40)],
        [("checkout.py", 15, 2)],  # short page: the last one
    ]

    def respond(path, body):
        if "aggs" in body:
            buckets = [
                {"key": {"file": f, "line": n}, "doc_count": c}
                for f, n, c in pages[1 if body["aggs"]["by_line"]["composite"].get("after") else 0]
            ]
            return {"aggregations": {"by_line": {"buckets": buckets, "after_key": {"x": 1}}}}
        # unlocated documents: matched by template
        msg = "payment failed for order 1: declined"
        return {"hits": {"hits": [_hit({"@timestamp": "2026-10-14T10:00:00Z", "message": msg})]}}

    with stub_server(respond) as (url, requests):
        b = OpenSearchBackend(url, "logs", aggregate=True)
        counts = b.count_by_line(index_project(DEMO), TimeRange.last(1))
    assert counts == {"app/checkout.py:7": 7000, "app/checkout.py:15": 42, "app/checkout.py:11": 1}
    first, second, unlocated = (body for _, _, body in requests)
    assert first["size"] == 0 and "after" not in first["aggs"]["by_line"]["composite"]
    assert second["aggs"]["by_line"]["composite"]["after"] == {"x": 1}
    sources = first["aggs"]["by_line"]["composite"]["sources"]
    assert sources == [
        {"file": {"terms": {"field": "log.origin.file.name"}}},
        {"line": {"terms": {"field": "log.origin.file.line"}}},
    ]
    (not_located,) = unlocated["query"]["bool"]["must_not"]
    assert not_located == {
        "bool": {
            "filter": [
                {"exists": {"field": "log.origin.file.name"}},
                {"exists": {"field": "log.origin.file.line"}},
            ]
        }
    }


def test_loki_aggregate_queries():
    b = LokiBackend("http://loki:3100", '{app="x"}', aggregate=True)
    r = TimeRange.last(0.5)
    assert b.build_count_query(r) == (
        'sum by (pathname, lineno) (count_over_time({app="x"} | json | __error__="" '
        '| pathname!="" | lineno!="" [1800s]))'
    )
    assert b.build_unlocated_query() == (
        '{app="x"} | json | __error__!="" or pathname="" or lineno="" '
        "| drop __error__, __error_details__"
    )
    lf = LokiBackend("http://loki", "{a}", parser="logfmt", file_label="f", line_label="l")
    assert "| logfmt |" in lf.build_count_query(r) and "sum by (f, l)" in lf.build_count_query(r)
    with pytest.raises(ValueError, match="parser"):
        LokiBackend("http://loki", "{a}", parser="regex")


def test_loki_aggregate_counts_against_a_stub_server():
    def respond(path, body):
        if path.startswith("/loki/api/v1/query?"):
            return {
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"pathname": "/srv/app/checkout.py", "lineno": "7"},
                            "value": [1760000000, "7000"],
                        },
                        {
                            "metric": {"pathname": "/srv/app/checkout.py", "lineno": "nope"},
                            "value": [1760000000, "9"],
                        },
                    ],
                }
            }
        line = "2026-10-14 10:00:05 ERROR payment failed for order 42: card declined"
        return {"data": {"result": [{"stream": {}, "values": [["1760000000000000000", line]]}]}}

    with stub_server(respond) as (url, requests):
        b = from_config({"type": "loki", "url": url, "selector": '{app="x"}', "aggregate": True})
        counts = b.count_by_line(index_project(DEMO), TimeRange.last(24))
    assert counts == {"app/checkout.py:7": 7000, "app/checkout.py:11": 1}
    metric_req, logs_req = (urllib.parse.urlparse(p) for p, _, _ in requests)
    assert metric_req.path == "/loki/api/v1/query"
    assert "sum by" in urllib.parse.parse_qs(metric_req.query)["query"][0]
    assert logs_req.path == "/loki/api/v1/query_range"
    assert '__error__!=""' in urllib.parse.parse_qs(logs_req.query)["query"][0]


class FakeLogs:
    """Stands in for a boto3 ``logs`` client; serves events in pages of two."""

    def __init__(self, events):
        self.events, self.calls = events, []

    def filter_log_events(self, **params):
        self.calls.append(params)
        start = int(params.get("nextToken", 0))
        page = {"events": self.events[start : start + 2]}
        if start + 2 < len(self.events):
            page["nextToken"] = str(start + 2)
        return page


CW_EVENTS = [
    {
        "timestamp": 1760436000000 + i,
        "logStreamName": "web-1",
        "message": json.dumps({"message": f"reserving item SKU-{i}", "lineno": 15}),
    }
    for i in range(5)
]


def test_cloudwatch_paginates_and_parses():
    fake = FakeLogs(CW_EVENTS)
    b = CloudWatchBackend("/app/checkout", filter_pattern="ERROR", client=fake)
    recs = b.query_logs(TimeRange.last(24 * 365 * 10), limit=4)
    assert [r.message for r in recs] == [f"reserving item SKU-{i}" for i in range(4)]
    assert recs[0].fields["logStreamName"] == "web-1" and recs[0].source_line == 15
    assert len(fake.calls) == 2 and fake.calls[1]["nextToken"] == "2"
    assert fake.calls[0]["logGroupName"] == "/app/checkout"
    assert fake.calls[0]["filterPattern"] == "ERROR"


def test_cloudwatch_counts_demo_app():
    sites = index_project(DEMO)
    b = CloudWatchBackend("/app/checkout", client=FakeLogs(CW_EVENTS))
    assert b.count_by_line(sites, TimeRange.last(24 * 365 * 10)) == {"app/checkout.py:15": 5}


def test_cloudwatch_filter_patterns():
    def cw(pattern=None):
        return CloudWatchBackend("g", filter_pattern=pattern, client=FakeLogs([]))

    assert cw().build_pattern("a b") == '"a b"'
    assert cw("ERROR").build_pattern("x") == 'ERROR "x"'
    json_pattern = '{ $.service = "checkout" }'
    # JSON patterns can't take an extra text term; the backend filters those client-side
    assert cw(json_pattern).build_pattern("x") == json_pattern


def test_from_config_errors():
    assert from_config({}) is None
    with pytest.raises(ValueError, match="supported: file, loki, opensearch"):
        from_config({"type": "splunk"})
    with pytest.raises(ValueError, match="'loki' needs 'url'"):
        from_config({"type": "loki"})


def test_count_total_loki_and_opensearch_against_stub_servers(tmp_path):
    def loki(path, body):
        return {"data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "4321"]}]}}

    with stub_server(loki) as (url, requests):
        b = from_config({"type": "loki", "url": url, "selector": '{app="x"}'})
        assert b.count_total(TimeRange.last(1)) == 4321
    (req,) = requests
    query = urllib.parse.parse_qs(urllib.parse.urlparse(req[0]).query)["query"][0]
    assert query == 'sum(count_over_time({app="x"} [3600s]))'

    def opensearch(path, body):
        return {"count": 987}

    with stub_server(opensearch) as (url, requests):
        b = from_config({"type": "opensearch", "url": url, "index": "logs-*"})
        assert b.count_total(TimeRange.last(1)) == 987
    assert urllib.parse.urlparse(requests[0][0]).path == "/logs-*/_count"


def test_count_total_file(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    lines = [f"{(now - timedelta(minutes=m)):%Y-%m-%d %H:%M:%S} INFO ok" for m in range(10)]
    lines += [f"{(now - timedelta(hours=5)):%Y-%m-%d %H:%M:%S} INFO old", "no time here"]
    (tmp_path / "a.log").write_text("\n".join(lines) + "\n")
    b = from_config({"type": "file", "path": str(tmp_path / "a.log")})
    assert b.count_total(TimeRange.last(1)) == 10  # untimed lines don't count anywhere


def _split_loki(monkeypatch):
    """A Loki that stores a Java trace one line per entry; records the queries it gets."""
    from logsite.backends import loki as lk

    t0 = 1_760_000_000 * 10**9
    trace = [
        "2026-10-18 14:22:31.812 INFO  [stock] c.e.s.StockController - lookup sku=bk-1",
        "2026-10-18 14:22:31.813 ERROR [stock] c.e.s.StockController - stock lookup failed",
        "org.springframework.dao.QueryTimeoutException: statement timeout",
        "\tat com.example.stock.StockRepository.find(StockRepository.java:54)",
        "Caused by: org.postgresql.util.PSQLException: ERROR: canceling statement",
        "\tat org.postgresql.core.v3.QueryExecutorImpl.execute(QueryExecutorImpl.java:27)",
        "2026-10-18 14:22:32.100 INFO  [stock] c.e.s.StockController - lookup sku=bk-2",
    ]
    entries = [(str(t0 + i), line) for i, line in enumerate(trace)]
    labels = {"app": "stock", "pod": "stock-1"}
    queries = []

    def get(self, endpoint, params):
        queries.append(params["query"])
        if "|~" in params["query"]:  # the error filter: frame lines don't match it
            hit = [(ts, line) for ts, line in entries if re.search(lk.ERROR_FILTER, line)]
            return {"data": {"result": [{"stream": labels, "values": hit[::-1]}]}}
        other = {"stream": {**labels, "container": "sidecar"}, "values": [(str(t0), "noise")]}
        return {"data": {"result": [{"stream": labels, "values": entries}, other]}}

    monkeypatch.setattr(lk.LokiBackend, "_get", get)
    return lk, queries, t0


def test_loki_joins_stack_traces_split_into_entries(monkeypatch):
    lk, queries, t0 = _split_loki(monkeypatch)
    b = lk.LokiBackend("http://loki", '{app="stock"}')
    window = TimeRange.last(1)
    records = b.query_errors(TimeRange(window.start.replace(year=2025), window.end))
    assert len(records) == 1  # one error, its trace joined; INFO lines and the sidecar dropped
    r = records[0]
    assert "Caused by: org.postgresql.util.PSQLException" in r.message
    assert "StockRepository.java:54" in r.message
    assert r.timestamp == datetime.fromtimestamp((t0 + 1) / 1e9, tz=timezone.utc)
    assert r.fields["pod"] == "stock-1"
    # unfiltered, with the configured selector (labels Loki returns can be structured
    # metadata, not selectable); the error's stream is picked from the result, not the sidecar
    assert queries[1] == '{app="stock"}' and "noise" not in r.message


def test_loki_keeps_split_errors_when_joining_takes_too_many_queries(monkeypatch):
    lk, queries, _ = _split_loki(monkeypatch)
    monkeypatch.setattr(lk, "MAX_STITCH_QUERIES", 0)
    b = lk.LokiBackend("http://loki", '{app="stock"}')
    window = TimeRange.last(1)
    records = b.query_errors(TimeRange(window.start.replace(year=2025), window.end))
    # the ERROR line, the exception line and a "Caused by:" piece, on their own; `errors` warns
    assert len(queries) == 1 and len(records) == 3
    assert any(r.message.startswith("Caused by:") for r in records)
