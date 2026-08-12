import json
from unittest import mock

import pytest

from syncroprintd import config as cfgmod
from syncroprintd import control
from syncroprintd.__main__ import Daemon
from syncroprintd.pipeline import Pipeline
from syncroprintd.store import Store

from test_pipeline import FakePrinter, FakeResponse, make_cfg, url
import syncroprintd.pipeline as pl


@pytest.fixture
def daemon(tmp_path):
    cfg = make_cfg()
    cfg_path = tmp_path / "config.json"
    cfgmod.save(cfg, str(cfg_path))
    store = Store(str(tmp_path / "jobs.db"))
    printer = FakePrinter()
    pipeline = Pipeline(cfg, store, spool_dir=str(tmp_path / "spool"),
                        printer_backend=printer)
    d = Daemon(cfg, store, pipeline, config_path=str(cfg_path))
    d.fake_printer = printer
    yield d
    import logging
    logging.getLogger().removeHandler(d.log_handler)
    store.close()


@pytest.fixture
def dispatcher(daemon):
    return control.Dispatcher(daemon)


def test_status(dispatcher):
    resp = dispatcher.handle({"cmd": "status"})
    assert resp["ok"] is True
    data = resp["data"]
    assert data["paused"] is False
    assert data["state"] == "starting"
    assert data["active_jobs"] == 0


def test_unknown_command(dispatcher):
    resp = dispatcher.handle({"cmd": "explode"})
    assert resp["ok"] is False and "unknown command" in resp["error"]


def test_pause_resume_roundtrip(dispatcher, daemon):
    assert dispatcher.handle({"cmd": "pause"})["data"]["paused"] is True
    assert daemon.pipeline.paused is True
    assert dispatcher.handle({"cmd": "status"})["data"]["state"] == "paused"
    assert dispatcher.handle({"cmd": "resume"})["data"]["paused"] is False


def test_recent_jobs_and_history(dispatcher, daemon):
    daemon.store.add_job("j1", "ticket")
    daemon.store.set_status("j1", "printed")
    daemon.store.add_job("j2", "invoice")
    daemon.store.set_status("j2", "failed", error="boom")
    recent = dispatcher.handle({"cmd": "recent_jobs", "limit": 5})["data"]
    assert [j["job_id"] for j in recent] == ["j2", "j1"]
    hist = dispatcher.handle({"cmd": "history", "status": "failed"})["data"]
    assert [j["job_id"] for j in hist] == ["j2"]


def test_test_print_command(dispatcher, daemon):
    resp = dispatcher.handle({"cmd": "test_print", "printer": "a4"})
    assert resp["ok"] is True
    assert daemon.fake_printer.calls[0]["printer"] == "HP"
    resp = dispatcher.handle({"cmd": "test_print", "printer": "nope"})
    assert resp["ok"] is False
    resp = dispatcher.handle({"cmd": "test_print"})
    assert resp["ok"] is False


def test_cancel_and_reprint(dispatcher, daemon):
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        daemon.pipeline.submit(pl.Job("c1", "ticket", url()))
        while not daemon.pipeline._queue.empty():
            daemon.pipeline._process(daemon.pipeline._queue.get_nowait())
    assert dispatcher.handle({"cmd": "reprint", "id": "c1"})["ok"] is True
    assert dispatcher.handle({"cmd": "cancel_job", "id": "missing"})["ok"] is False


def test_get_config_redacts_token(dispatcher):
    data = dispatcher.handle({"cmd": "get_config"})["data"]
    assert data["account"]["api_token"] == "********"


def test_set_config_persists_and_applies(dispatcher, daemon, tmp_path):
    resp = dispatcher.handle({"cmd": "set_config", "config": {
        "transport": {"poll_interval_s": 300},
        "account": {"api_token": "********"},   # masked = keep
    }})
    assert resp["ok"] is True
    assert daemon.cfg.transport.poll_interval_s == 300
    assert daemon.cfg.account.api_token == "tok"      # kept
    on_disk = cfgmod.load(daemon.config_path)
    assert on_disk.transport.poll_interval_s == 300


def test_set_config_invalid_rejected(dispatcher, daemon):
    resp = dispatcher.handle({"cmd": "set_config", "config": {"transport": {"mode": "x"}}})
    assert resp["ok"] is False
    assert daemon.cfg.transport.mode == "auto"  # unchanged


def test_log_tail(dispatcher, daemon):
    import logging
    logging.getLogger("syncroprintd.test").warning("hello from test")
    tail = dispatcher.handle({"cmd": "get_log_tail", "n": 10})["data"]
    assert any("hello from test" in line for line in tail)


def test_server_client_over_socket(dispatcher):
    """Full wire round-trip. Uses TCP loopback because Python on Windows
    lacks AF_UNIX; the framing/dispatch code is identical for both."""
    server = control.ControlServer(dispatcher, address=("127.0.0.1", 0))
    server.start()
    try:
        client = control.ControlClient(("127.0.0.1", server.port))
        data = client.call("status")
        assert data["paused"] is False
        client.call("pause")
        assert client.call("status")["paused"] is True
        with pytest.raises(control.ControlError, match="unknown command"):
            client.call("bogus")
        # malformed line straight over the socket
        import socket as socketmod
        raw = socketmod.create_connection(("127.0.0.1", server.port), timeout=5)
        raw.sendall(b"this is not json\n")
        reply = json.loads(raw.makefile().readline())
        assert reply["ok"] is False
        raw.close()
        client.close()
    finally:
        server.stop()


# -- first-run / GUI setup flow ------------------------------------------

def unconfigured_daemon(tmp_path):
    cfg = cfgmod.Config()  # empty account, no printers/routing
    cfg_path = tmp_path / "config.json"
    cfgmod.save(cfg, str(cfg_path))
    store = Store(str(tmp_path / "jobs.db"))
    pipeline = Pipeline(cfg, store, spool_dir=str(tmp_path / "spool"),
                        printer_backend=FakePrinter())
    return Daemon(cfg, store, pipeline, config_path=str(cfg_path))


def test_empty_account_config_is_valid():
    cfg = cfgmod.Config()
    cfg.validate()  # must not raise
    assert cfg.account.is_configured is False
    with pytest.raises(cfgmod.ConfigError, match="api_token is required"):
        cfgmod.from_dict({"account": {"subdomain": "x"}})


def test_unconfigured_daemon_reports_state_and_idles_transports(tmp_path):
    d = unconfigured_daemon(tmp_path)
    try:
        assert d.status()["state"] == "unconfigured"
        d.start_transports()
        assert d.pusher is None and d.poller is None
    finally:
        import logging
        logging.getLogger().removeHandler(d.log_handler)
        d.store.close()


def test_set_config_with_account_starts_transports(tmp_path):
    import syncroprintd.__main__ as main_mod

    class FakeTransport:
        def __init__(self, *a, **kw):
            self.started = False
        def start(self):
            self.started = True
        def stop(self):
            pass
        def set_active(self, active):
            pass

    d = unconfigured_daemon(tmp_path)
    disp = control.Dispatcher(d)
    try:
        with mock.patch.object(main_mod, "PusherTransport", FakeTransport), \
             mock.patch.object(main_mod, "PollerTransport", FakeTransport):
            resp = disp.handle({"cmd": "set_config", "config": {
                "account": {"host": "syncromsp.com", "subdomain": "exampleshop",
                            "api_token": "newtok"}}})
            assert resp["ok"] is True
            assert d.cfg.account.is_configured
            assert d.pusher is not None and d.pusher.started
            assert d.poller is not None and d.poller.started
            assert d.status()["state"] == "starting"
    finally:
        import logging
        logging.getLogger().removeHandler(d.log_handler)
        d.store.close()


def test_test_account_command(tmp_path):
    from syncroprintd import api
    d = unconfigured_daemon(tmp_path)
    disp = control.Dispatcher(d)
    try:
        # missing details
        data = disp.handle({"cmd": "test_account", "subdomain": "", "api_token": ""})["data"]
        assert data["ok_account"] is False
        # good details, api mocked
        with mock.patch.object(api, "fetch_printing_settings",
                               return_value={"messaging_channel": "ch", "registers": []}):
            data = disp.handle({"cmd": "test_account", "host": "syncromsp.com",
                                "subdomain": "ExampleShop", "api_token": "tok"})["data"]
        assert data["ok_account"] is True and "OK" in data["message"]
    finally:
        import logging
        logging.getLogger().removeHandler(d.log_handler)
        d.store.close()


def test_test_account_masked_token_uses_saved(daemon):
    from syncroprintd import api
    seen = {}

    def fake_fetch(account):
        seen["token"] = account.api_token
        return {"messaging_channel": "ch", "registers": []}

    with mock.patch.object(api, "fetch_printing_settings", side_effect=fake_fetch):
        ok, _ = daemon.test_account("syncromsp.com", "exampleshop", "********")
    assert ok is True
    assert seen["token"] == "tok"  # the saved token, not the mask
