"""Pusher Channels transport — realtime job delivery (§5.2).

Implements the Pusher client protocol directly over `websockets` rather
than pulling in an unmaintained SDK: connect, `pusher:connection_established`,
`pusher:subscribe`, `pusher:ping`/`pusher:pong`, event dispatch. The
protocol is small and publicly documented; the original clients used
protocol 6-7 against `ws.pusherapp.com` with no cluster (PROTOCOL.md §3).

Runs its own asyncio loop on a daemon thread; all callbacks fire on that
thread, so callers must be thread-safe (Pipeline and Store are).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
from typing import Any, Callable

import websockets

log = logging.getLogger("syncroprintd.pusher")

DEFAULT_APP_KEY = "4a12d53c136a2d3dade7"   # shipped in every AutoPrintr client; public identifier
# WPF client (newest) uses the mt1 cluster host; mac used the legacy global
# alias. Both resolve to the same app — we cycle through them on failure.
PUSHER_HOSTS = ("ws-mt1.pusher.com", "ws.pusherapp.com")
PUSHER_HOST = PUSHER_HOSTS[0]
PROTOCOL_VERSION = 7
PRINT_JOB_EVENT = "print-job"

_BACKOFF_START = 1.0
_BACKOFF_CAP = 60.0
_DEFAULT_ACTIVITY_TIMEOUT = 120
_PONG_TIMEOUT = 30


class PusherTransport:
    """Maintains one subscribed channel and hands `print-job` payloads up.

    on_job(payload_dict)      — a print-job event arrived
    on_connect()              — (re)connected + subscribed; daemon runs a
                                poller gap-fill sweep here (§5.2)
    on_state(state_str)       — "connected" | "disconnected" for the applet icon
    channel_provider()        — returns the channel name; called on every
                                (re)connect so a re-login is picked up live
    """

    def __init__(self, app_key: str, channel_provider: Callable[[], str],
                 on_job: Callable[[dict[str, Any]], None],
                 on_connect: Callable[[], None] = lambda: None,
                 on_state: Callable[[str], None] = lambda s: None,
                 host: str = PUSHER_HOST):
        self.app_key = app_key
        self.channel_provider = channel_provider
        self.on_job = on_job
        self.on_connect = on_connect
        self.on_state = on_state
        self.host = host
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        return (f"wss://{self.host}/app/{self.app_key}"
                f"?protocol={PROTOCOL_VERSION}&client=syncroprint-linux&version=0.1.0")

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="pusher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        loop, task = self._loop, self._task
        if loop and loop.is_running() and task:
            loop.call_soon_threadsafe(task.cancel)
        if self._thread:
            self._thread.join(timeout=10)

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._run())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    # -- connection loop --------------------------------------------------

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        while not self._stopping.is_set():
            try:
                await self._session()
                backoff = _BACKOFF_START  # a completed session had connected OK
            except Exception as exc:
                log.warning("pusher connection failed: %s", exc)
                if self.host in PUSHER_HOSTS:  # not a test/custom host
                    self.host = PUSHER_HOSTS[(PUSHER_HOSTS.index(self.host) + 1) % len(PUSHER_HOSTS)]
            if self._stopping.is_set():
                break
            self.on_state("disconnected")
            delay = backoff + random.uniform(0, backoff / 2)
            log.info("reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(asyncio.to_thread(self._stopping.wait, delay), delay + 1)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def _session(self) -> None:
        channel = self.channel_provider()
        if not channel:
            raise RuntimeError("no channel name available")
        async with websockets.connect(self.url, open_timeout=20, close_timeout=5,
                                      max_size=1 << 20) as ws:
            activity_timeout = await self._handshake(ws, channel)
            # The channel name is effectively a credential (unguessable name
            # is the access control on a public Pusher channel) — never log
            # it in full.
            log.info("connected and subscribed to channel %s…", channel[:4])
            self.on_state("connected")
            self.on_connect()
            await self._read_loop(ws, activity_timeout)

    async def _handshake(self, ws, channel: str) -> int:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        msg = json.loads(raw)
        if msg.get("event") != "pusher:connection_established":
            raise RuntimeError(f"expected connection_established, got {msg.get('event')!r}")
        data = _event_data(msg)
        activity_timeout = int(data.get("activity_timeout", _DEFAULT_ACTIVITY_TIMEOUT))
        await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"channel": channel}}))
        return activity_timeout

    async def _read_loop(self, ws, activity_timeout: int) -> None:
        while not self._stopping.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=activity_timeout)
            except asyncio.TimeoutError:
                # Quiet too long: probe with pusher:ping, expect pusher:pong
                await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=_PONG_TIMEOUT)
            self._dispatch(json.loads(raw), ws)

    def _dispatch(self, msg: dict[str, Any], ws) -> None:
        event = msg.get("event", "")
        if event == "pusher:ping":
            asyncio.ensure_future(ws.send(json.dumps({"event": "pusher:pong", "data": {}})))
        elif event == PRINT_JOB_EVENT:
            try:
                payload = _event_data(msg)
            except (ValueError, TypeError) as exc:
                log.warning("undecodable print-job event: %s", exc)
                return
            log.info("print-job event received")
            self.on_job(payload)
        elif event == "pusher:error":
            data = msg.get("data") or {}
            log.error("pusher error %s: %s", data.get("code"), data.get("message"))
        # pusher:pong, pusher_internal:subscription_succeeded etc. need no action


def _event_data(msg: dict[str, Any]) -> dict[str, Any]:
    """Pusher double-encodes event data as a JSON string; tolerate both."""
    data = msg.get("data", {})
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise TypeError(f"event data is {type(data).__name__}, expected object")
    return data
