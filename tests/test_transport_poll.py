import json
from unittest import mock

import pytest

from syncroprintd import transport_poll as tpoll
from syncroprintd.config import Account
from syncroprintd.pipeline import job_from_payload
from syncroprintd.store import Store


class FakeJsonResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise tpoll.requests.HTTPError(str(self.status_code))


def account():
    return Account(host="syncromsp.com", subdomain="exampleshop", api_token="tok")


def make_poller(tmp_path, on_job=None, sources=None):
    store = Store(str(tmp_path / "jobs.db"))
    store.set_meta(tpoll.CURSOR_KEY, "2026-08-12T00:00:00Z")
    p = tpoll.PollerTransport(account, store, on_job or (lambda j: None),
                              sources=sources if sources is not None else tpoll.DEFAULT_SOURCES)
    return p, store


def test_source_emits_payloads_with_poll_ids():
    src = tpoll.RestSweepSource("tickets", "Ticket")
    body = {"tickets": [
        {"id": 101, "pdf_url": "https://exampleshop.syncromsp.com/t/101.pdf", "location_id": 5},
        {"id": 102},                       # no pdf field -> skipped
        "garbage",                          # not a dict -> skipped
    ]}
    with mock.patch.object(tpoll.requests, "get", return_value=FakeJsonResponse(body)) as get:
        payloads = src.fetch(account(), "2026-08-12T00:00:00Z")
    assert len(payloads) == 1
    p = payloads[0]
    assert p["_poll_id"] == "tickets-101"
    assert p["document"] == "Ticket"
    assert p["autoprinted"] is True
    assert p["location"] == 5
    params = get.call_args.kwargs["params"]
    assert params["api_key"] == "tok" and params["since_updated_at"] == "2026-08-12T00:00:00Z"


def test_poll_job_id_is_resource_stable():
    p = {"document": "Ticket", "file": "https://x.syncromsp.com/a.pdf", "_poll_id": "tickets-101"}
    assert job_from_payload(p).id == "tickets-101"
    # even if the pre-signed URL changes between sweeps, id is stable
    p2 = dict(p, file="https://x.syncromsp.com/a.pdf?sig=other")
    assert job_from_payload(p2).id == "tickets-101"


def test_source_auth_error_raises():
    with mock.patch.object(tpoll.requests, "get", return_value=FakeJsonResponse({}, status=401)):
        with pytest.raises(PermissionError):
            tpoll.RestSweepSource("tickets", "Ticket").fetch(account(), "x")


def test_sweep_advances_cursor_on_success(tmp_path):
    jobs = []
    p, store = make_poller(tmp_path, on_job=jobs.append,
                           sources=[tpoll.RestSweepSource("tickets", "Ticket")])
    body = {"tickets": [{"id": 1, "pdf_url": "https://x/t.pdf"}]}
    with mock.patch.object(tpoll.requests, "get", return_value=FakeJsonResponse(body)):
        assert p.sweep_once() == 1
    assert jobs and jobs[0]["_poll_id"] == "tickets-1"
    assert store.get_meta(tpoll.CURSOR_KEY) != "2026-08-12T00:00:00Z"
    store.close()


def test_sweep_keeps_cursor_on_failure(tmp_path):
    p, store = make_poller(tmp_path, sources=[tpoll.RestSweepSource("tickets", "Ticket")])
    with mock.patch.object(tpoll.requests, "get",
                           side_effect=tpoll.requests.ConnectionError("down")):
        assert p.sweep_once() == 0
    assert store.get_meta(tpoll.CURSOR_KEY) == "2026-08-12T00:00:00Z"  # window not lost
    store.close()


def test_mark_current_moves_cursor(tmp_path):
    p, store = make_poller(tmp_path)
    p.mark_current()
    assert store.get_meta(tpoll.CURSOR_KEY) != "2026-08-12T00:00:00Z"
    store.close()


def test_first_run_initializes_cursor_to_now(tmp_path):
    store = Store(str(tmp_path / "fresh.db"))
    p = tpoll.PollerTransport(account, store, lambda j: None, sources=[])
    p.start()
    try:
        assert store.get_meta(tpoll.CURSOR_KEY) is not None
    finally:
        p.stop()
        store.close()


def test_source_retired_after_401(tmp_path):
    p, store = make_poller(tmp_path, sources=[tpoll.RestSweepSource("tickets", "Ticket")])
    with mock.patch.object(tpoll.requests, "get",
                           return_value=FakeJsonResponse({}, status=401)) as get:
        p.sweep_once()
        assert get.call_count == 1
        assert p.sources == []      # retired, not retried
        p.sweep_once()
        assert get.call_count == 1  # no further API calls
    store.close()
