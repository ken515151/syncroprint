"""Integration test: PusherTransport against a local fake Pusher server."""

import asyncio
import json
import threading
import time

import pytest
import websockets

from syncroprintd import transport_pusher as tp


class FakePusherServer:
    """Speaks just enough of the Pusher protocol for the client under test."""

    def __init__(self):
        self.port = None
        self.connections = 0
        self.subscribed = []
        self.received = []          # non-subscribe frames from the client
        self.script = []            # frames to send after subscribe
        self.drop_after_script = False
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop = None

    def start(self):
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "fake server failed to start"

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=10)

    def _main(self):
        asyncio.run(self._serve())

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with websockets.serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({
            "event": "pusher:connection_established",
            "data": json.dumps({"socket_id": "1.1", "activity_timeout": 120}),
        }))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") == "pusher:subscribe":
                    channel = msg["data"]["channel"]
                    self.subscribed.append(channel)
                    await ws.send(json.dumps({
                        "event": "pusher_internal:subscription_succeeded",
                        "channel": channel, "data": "{}",
                    }))
                    for frame in self.script:
                        await ws.send(json.dumps(frame))
                    if self.drop_after_script:
                        await ws.close()
                        return
                else:
                    self.received.append(msg)
        except websockets.ConnectionClosed:
            pass


def make_transport(server, on_job=None, on_connect=None, on_state=None):
    t = tp.PusherTransport(
        "testkey", lambda: "test-channel",
        on_job=on_job or (lambda p: None),
        on_connect=on_connect or (lambda: None),
        on_state=on_state or (lambda s: None),
        host=f"127.0.0.1:{server.port}")
    # Local test server has no TLS
    tp.PusherTransport.url = property(
        lambda self: f"ws://{self.host}/app/{self.app_key}?protocol=7&client=test&version=0")
    return t


def wait_for(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server():
    s = FakePusherServer()
    s.start()
    yield s
    s.stop()


def test_connect_subscribe_and_receive_job(server):
    payload = {"document": "Ticket", "type": "Ticket",
               "file": "https://x.syncromsp.com/t.pdf",
               "location": None, "register": None, "autoprinted": False}
    server.script = [{"event": "print-job", "channel": "test-channel",
                      "data": json.dumps(payload)}]
    jobs, states, connects = [], [], []
    t = make_transport(server, on_job=jobs.append,
                       on_connect=lambda: connects.append(1),
                       on_state=states.append)
    t.start()
    try:
        assert wait_for(lambda: jobs)
        assert jobs[0] == payload
        assert server.subscribed == ["test-channel"]
        assert connects == [1]
        assert "connected" in states
    finally:
        t.stop()


def test_ping_answered_with_pong(server):
    server.script = [{"event": "pusher:ping", "data": "{}"}]
    t = make_transport(server)
    t.start()
    try:
        assert wait_for(lambda: any(m.get("event") == "pusher:pong" for m in server.received))
    finally:
        t.stop()


def test_reconnects_after_drop(server):
    server.drop_after_script = True
    connects = []
    t = make_transport(server, on_connect=lambda: connects.append(1))
    t.start()
    try:
        # server closes right after subscribe; client must come back (1s backoff)
        assert wait_for(lambda: len(connects) >= 2, timeout=15)
        assert server.connections >= 2
    finally:
        t.stop()


def test_undecodable_event_does_not_kill_connection(server):
    good = {"document": "Invoice", "type": "Invoice",
            "file": "https://x.syncromsp.com/i.pdf"}
    server.script = [
        {"event": "print-job", "data": "not json at all"},
        {"event": "print-job", "data": json.dumps(good)},
    ]
    jobs = []
    t = make_transport(server, on_job=jobs.append)
    t.start()
    try:
        assert wait_for(lambda: jobs)
        assert jobs == [good]
    finally:
        t.stop()
