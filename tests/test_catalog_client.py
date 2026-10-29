"""CatalogClient: caching, offline mode, error replies.  Fake transports only, no network."""

import http.server
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from transitory.catalog import client as C
from transitory.catalog.client import (CatalogClient, CatalogError, RecordedTransport,
                                       ResponseCache, adql_string, request_key)

RECORDED = Path(__file__).parent / "data" / "catalog" / "recorded.json"


class FakeTransport:
    """Answers every request with the next body in ``bodies`` (the last one repeats)."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, method, url, params):
        self.calls.append((method, url, params))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, Exception):
            raise body
        return body


ROWS = json.dumps([{"pl_name": "X b", "pl_orbper": 1.5}])


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(C, "RETRY_BACKOFF_SECONDS", 0.0)


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(tmp_path / "catalog")


def test_tap_parses_rows_and_caches_them(cache):
    transport = FakeTransport(ROWS)
    client = CatalogClient(transport=transport, cache=cache)
    first = client.tap("select pl_name from pscomppars")
    assert first.data == [{"pl_name": "X b", "pl_orbper": 1.5}]
    assert not first.from_cache
    assert first.retrieved_date == datetime.now(UTC).date().isoformat()

    second = client.tap("select   pl_name\n from pscomppars")  # same query, other spacing
    assert second.from_cache
    assert second.data == first.data
    assert second.retrieved == first.retrieved
    assert len(transport.calls) == 1
    method, url, params = transport.calls[0]
    assert (method, url) == ("GET", C.TAP_URL)
    assert params == {"query": "select pl_name from pscomppars", "format": "json"}


def test_a_new_client_reuses_the_disk_cache(cache):
    CatalogClient(transport=FakeTransport(ROWS), cache=cache).tap("select 1")
    transport = FakeTransport(CatalogError("must not be called"))
    assert CatalogClient(transport=transport, cache=cache).tap("select 1").from_cache
    assert transport.calls == []


def test_refresh_bypasses_the_cache(cache):
    transport = FakeTransport(ROWS)
    client = CatalogClient(transport=transport, cache=cache)
    client.tap("select 1")
    assert not client.tap("select 1", refresh=True).from_cache
    assert len(transport.calls) == 2


def test_stale_entries_are_fetched_again(cache):
    transport = FakeTransport(ROWS)
    client = CatalogClient(transport=transport, cache=cache)
    params = {"query": "select 1", "format": "json"}
    key = request_key("GET", C.TAP_URL, params)
    old = C.Response(data=[], retrieved=datetime.now(UTC) - timedelta(days=400))
    cache.put(key, {}, old)

    assert client.tap("select 1", max_age=None).data == []  # no age limit: old entry is fine
    fresh = client.tap("select 1")  # default archive limit is 30 days
    assert not fresh.from_cache and fresh.data == json.loads(ROWS)
    assert len(transport.calls) == 1


def test_offline_answers_only_from_cache(cache):
    CatalogClient(transport=FakeTransport(ROWS), cache=cache).tap("select 1")
    transport = FakeTransport(ROWS)
    offline = CatalogClient(transport=transport, cache=cache, offline=True)
    assert offline.tap("select 1").from_cache
    with pytest.raises(CatalogError, match="offline"):
        offline.tap("select 2")
    assert transport.calls == []


def test_archive_votable_error_becomes_catalog_error(cache):
    body = ('<?xml version="1.0"?><VOTABLE><RESOURCE type="results">'
            '<INFO name="QUERY_STATUS" value="ERROR">\nORA-00904: "PL_REFNAME": invalid identifier\n'
            '</INFO></RESOURCE></VOTABLE>')
    client = CatalogClient(transport=FakeTransport(body), cache=cache)
    with pytest.raises(CatalogError, match="ORA-00904"):
        client.tap("select pl_refname from pscomppars")
    assert list(cache.directory.iterdir()) == []  # errors are never cached


@pytest.mark.parametrize("body", ["not json", '{"rows": 1}'])
def test_archive_garbage_becomes_catalog_error(cache, body):
    with pytest.raises(CatalogError):
        CatalogClient(transport=FakeTransport(body), cache=cache).tap("select 1")


def test_mast_error_status_becomes_catalog_error(cache):
    body = json.dumps({"status": "ERROR", "msg": "bad service"})
    with pytest.raises(CatalogError, match="bad service"):
        CatalogClient(transport=FakeTransport(body), cache=cache).mast({"service": "Nope"})


def test_mast_executing_is_asked_again(cache, monkeypatch):
    monkeypatch.setattr(C, "MAST_RETRY_SECONDS", 0.0)
    transport = FakeTransport(json.dumps({"status": "EXECUTING", "data": []}),
                              json.dumps({"status": "COMPLETE", "data": [{"ID": 1}]}))
    reply = CatalogClient(transport=transport, cache=cache).mast({"service": "Mast.Catalogs.Tic.Cone"})
    assert reply.data["data"] == [{"ID": 1}]
    assert len(transport.calls) == 2
    method, url, params = transport.calls[0]
    assert (method, url) == ("POST", C.MAST_URL)
    assert json.loads(params["request"]) == {"service": "Mast.Catalogs.Tic.Cone"}


def test_mast_executing_forever_times_out(cache, monkeypatch):
    monkeypatch.setattr(C, "MAST_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(C, "MAST_MAX_WAIT_SECONDS", 0.0)
    transport = FakeTransport(json.dumps({"status": "EXECUTING"}))
    with pytest.raises(CatalogError, match="still running"):
        CatalogClient(transport=transport, cache=cache).mast({"service": "x"})


def test_unreadable_cache_entry_is_logged_and_refetched(cache, caplog):
    client = CatalogClient(transport=FakeTransport(ROWS), cache=cache)
    client.tap("select 1")
    (entry,) = cache.directory.iterdir()
    entry.write_text("{truncated", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="transitory.catalog.client"):
        assert not client.tap("select 1").from_cache
    assert "unreadable catalog cache entry" in caplog.text


def test_default_cache_follows_the_cache_dir_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSITORY_CACHE_DIR", str(tmp_path / "elsewhere"))
    assert ResponseCache().directory == tmp_path / "elsewhere" / "catalog"


def test_transport_failure_propagates(cache):
    client = CatalogClient(transport=FakeTransport(CatalogError("unreachable")), cache=cache)
    with pytest.raises(CatalogError, match="unreachable"):
        client.tap("select 1")


class _Reply:
    def __init__(self, status, text, reason="Bad Request"):
        self.status_code, self.text, self.reason = status, text, reason


def test_requests_transport_reports_archive_error_text(monkeypatch):
    import requests

    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return _Reply(400, '<VOTABLE><INFO name="QUERY_STATUS" value="ERROR">ORA-1: no</INFO></VOTABLE>')

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(CatalogError, match="HTTP 400: ORA-1: no"):
        C.requests_transport("GET", C.TAP_URL, {"query": "x"})
    assert seen["headers"]["User-Agent"].startswith("transitory/")


def test_requests_transport_wraps_connection_errors(monkeypatch):
    import requests

    def fake_post(url, data, headers, timeout):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(CatalogError, match="no route to host"):
        C.requests_transport("POST", C.MAST_URL, {"request": "{}"})


class SlowRequests:
    """Stands in for ``requests.get``: times out ``timeouts`` times, then answers ``replies``."""

    def __init__(self, timeouts, *replies):
        self.timeouts = timeouts
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, params, headers, timeout):
        import requests

        self.calls.append(timeout)
        if len(self.calls) <= self.timeouts:
            raise requests.ReadTimeout(f"Read timed out. (read timeout={timeout})")
        return self.replies.pop(0)


def test_requests_transport_tries_a_timeout_once_more(monkeypatch):
    import requests

    slow = SlowRequests(1, _Reply(200, ROWS))
    monkeypatch.setattr(requests, "get", slow)
    assert C.requests_transport("GET", C.TAP_URL, {"query": "x"}) == ROWS
    assert slow.calls == [C.REQUEST_TIMEOUT_SECONDS] * 2


def test_requests_transport_gives_up_after_two_timeouts(monkeypatch):
    import requests

    slow = SlowRequests(5)
    monkeypatch.setattr(requests, "get", slow)
    monkeypatch.setattr(C, "RETRY_BACKOFF_SECONDS", 3.0)
    sleeps = []
    monkeypatch.setattr(C.time, "sleep", sleeps.append)
    with pytest.raises(CatalogError, match=r"Read timed out.*\(tried twice\)"):
        C.requests_transport("GET", C.TAP_URL, {"query": "x"})
    assert len(slow.calls) == 2
    assert sleeps == [3.0]


def test_requests_transport_tries_a_server_error_once_more(monkeypatch):
    import requests

    slow = SlowRequests(0, _Reply(503, "busy", "Service Unavailable"), _Reply(200, ROWS))
    monkeypatch.setattr(requests, "get", slow)
    assert C.requests_transport("GET", C.TAP_URL, {"query": "x"}) == ROWS
    assert len(slow.calls) == 2


def test_requests_transport_does_not_retry_a_bad_query(monkeypatch):
    import requests

    slow = SlowRequests(0, _Reply(400, "nope"))
    monkeypatch.setattr(requests, "get", slow)
    with pytest.raises(CatalogError, match="HTTP 400: nope"):
        C.requests_transport("GET", C.TAP_URL, {"query": "x"})
    assert len(slow.calls) == 1


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(2.0)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_requests_transport_ends_a_slow_request(cache):
    """A real request to a local server slower than the timeout fails promptly, twice tried."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        start = time.monotonic()
        with pytest.raises(CatalogError, match="tried twice"):
            C.requests_transport("GET", url, {"query": "x"}, timeout=0.3)
        assert time.monotonic() - start < 2.0
    finally:
        server.shutdown()
        server.server_close()


def test_recorded_transport_replays_and_reports_missing():
    transport = RecordedTransport(RECORDED)
    entry = next(iter(transport.recordings.values()))
    assert transport(entry["method"], entry["url"], entry["params"]) == entry["body"]
    with pytest.raises(CatalogError, match="no recorded response"):
        transport("GET", C.TAP_URL, {"query": "never recorded", "format": "json"})
    assert len(transport.missing) == 1


def test_recording_transport_round_trips(tmp_path):
    recorder = C.RecordingTransport(inner=FakeTransport(ROWS), trim=lambda url, params, body: body.upper())
    assert recorder("GET", C.TAP_URL, {"query": "q"}) == ROWS.upper()
    path = tmp_path / "rec.json"
    recorder.save(path)
    assert RecordedTransport(path)("GET", C.TAP_URL, {"query": "q"}) == ROWS.upper()


def test_adql_string_escapes_quotes():
    assert adql_string("O'Brien's star") == "'O''Brien''s star'"
