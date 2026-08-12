"""Control socket (§5.5): newline-delimited JSON over a UNIX domain socket.

The applet is a pure client of this interface. No TCP port is opened in
production; the socket lives at /run/syncroprint/control.sock with group
`syncroprint` so desktop users in that group can talk to the daemon.

Wire format: one JSON object per line.
  request:  {"cmd": "status", ...args}
  response: {"ok": true, "data": ...} | {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any

log = logging.getLogger("syncroprintd.control")

DEFAULT_SOCKET_PATH = "/run/syncroprint/control.sock"
MAX_LINE = 256 * 1024

COMMANDS = ("status", "recent_jobs", "history", "pause", "resume", "test_print",
            "cancel_job", "reprint", "get_log_tail", "get_config", "set_config",
            "reload", "printers", "test_account")


class Dispatcher:
    """Maps command dicts onto daemon methods. Pure logic — no sockets —
    so the whole command surface is unit-testable anywhere."""

    def __init__(self, daemon):
        self.daemon = daemon

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            cmd = request.get("cmd")
            if cmd not in COMMANDS:
                return {"ok": False, "error": f"unknown command {cmd!r}"}
            data = getattr(self, f"_cmd_{cmd}")(request)
            return {"ok": True, "data": data}
        except Exception as exc:
            log.exception("control command failed: %s", request.get("cmd"))
            return {"ok": False, "error": str(exc)}

    def _cmd_status(self, req):
        return self.daemon.status()

    def _cmd_recent_jobs(self, req):
        return self.daemon.store.recent_jobs(int(req.get("limit", 10)))

    def _cmd_history(self, req):
        kwargs = {k: req[k] for k in ("status", "document_type", "since", "until", "search")
                  if req.get(k) is not None}
        kwargs["limit"] = int(req.get("limit", 200))
        return self.daemon.store.history(**kwargs)

    def _cmd_pause(self, req):
        self.daemon.pipeline.pause()
        return {"paused": True}

    def _cmd_resume(self, req):
        self.daemon.pipeline.resume()
        return {"paused": False}

    def _cmd_test_print(self, req):
        printer = req.get("printer")
        if not printer:
            raise ValueError("test_print needs 'printer'")
        if not self.daemon.pipeline.test_print(printer):
            raise ValueError(f"no configured printer {printer!r}")
        return {"submitted": True}

    def _cmd_cancel_job(self, req):
        job_id = req.get("id")
        if not job_id:
            raise ValueError("cancel_job needs 'id'")
        if not self.daemon.pipeline.cancel(job_id):
            raise ValueError(f"unknown job {job_id!r}")
        return {"cancelled": True}

    def _cmd_reprint(self, req):
        job_id = req.get("id")
        if not job_id:
            raise ValueError("reprint needs 'id'")
        if not self.daemon.pipeline.reprint(job_id):
            raise ValueError(f"cannot reprint {job_id!r} (no spool file or URL)")
        return {"submitted": True}

    def _cmd_get_log_tail(self, req):
        return self.daemon.log_tail(int(req.get("n", 100)))

    def _cmd_get_config(self, req):
        return self.daemon.cfg.redacted_dict()

    def _cmd_set_config(self, req):
        update = req.get("config")
        if not isinstance(update, dict):
            raise ValueError("set_config needs 'config' object")
        self.daemon.update_config(update)
        return self.daemon.cfg.redacted_dict()

    def _cmd_reload(self, req):
        self.daemon.reload_config()
        return self.daemon.cfg.redacted_dict()

    def _cmd_printers(self, req):
        """CUPS printers available on the system, for the settings UI."""
        return self.daemon.list_system_printers()

    def _cmd_test_account(self, req):
        """Test credentials from the settings form without saving them."""
        ok, message = self.daemon.test_account(
            req.get("host"), req.get("subdomain"), req.get("api_token"))
        return {"ok_account": ok, "message": message}


class ControlServer:
    """Line-oriented socket server around a Dispatcher.

    `address` is a filesystem path (AF_UNIX, production) or a
    (host, port) tuple (AF_INET, used by tests on platforms without
    AF_UNIX support in Python).
    """

    def __init__(self, dispatcher: Dispatcher, address: str | tuple[str, int] = DEFAULT_SOCKET_PATH,
                 group_mode: int = 0o660):
        self.dispatcher = dispatcher
        self.address = address
        self.group_mode = group_mode
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    @property
    def port(self) -> int | None:
        if self._sock and isinstance(self.address, tuple):
            return self._sock.getsockname()[1]
        return None

    def start(self) -> None:
        if isinstance(self.address, tuple):
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(self.address)
        else:
            if os.path.exists(self.address):
                os.unlink(self.address)
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            self._sock.bind(self.address)
            os.chmod(self.address, self.group_mode)
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._accept_loop, name="control", daemon=True)
        self._thread.start()
        log.info("control socket listening on %s", self.address)

    def stop(self) -> None:
        self._stopping = True
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        if isinstance(self.address, str) and os.path.exists(self.address):
            try:
                os.unlink(self.address)
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            with conn, conn.makefile("rwb") as fh:
                for raw in fh:
                    if len(raw) > MAX_LINE:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                    except ValueError:
                        response = {"ok": False, "error": "request was not valid JSON"}
                    else:
                        response = self.dispatcher.handle(request)
                    fh.write(json.dumps(response).encode() + b"\n")
                    fh.flush()
        except (OSError, ValueError):
            pass


class ControlClient:
    """Client used by the applet and the CLI. One request per call;
    the connection is kept open across calls and reopened on error."""

    def __init__(self, address: str | tuple[str, int] = DEFAULT_SOCKET_PATH, timeout: float = 10):
        self.address = address
        self.timeout = timeout
        self._fh = None
        self._sock = None

    def _connect(self):
        if isinstance(self.address, tuple):
            sock = socket.create_connection(self.address, timeout=self.timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
            sock.settimeout(self.timeout)
            sock.connect(self.address)
        self._sock = sock
        self._fh = sock.makefile("rwb")

    def call(self, cmd: str, **args) -> Any:
        request = {"cmd": cmd, **args}
        for attempt in (1, 2):
            if self._fh is None:
                self._connect()
            try:
                self._fh.write(json.dumps(request).encode() + b"\n")
                self._fh.flush()
                line = self._fh.readline()
                if not line:
                    raise OSError("connection closed")
                break
            except OSError:
                self.close()
                if attempt == 2:
                    raise
        response = json.loads(line)
        if not response.get("ok"):
            raise ControlError(response.get("error", "unknown error"))
        return response.get("data")

    def close(self) -> None:
        for obj in (self._fh, self._sock):
            if obj is not None:
                try:
                    obj.close()
                except OSError:
                    pass
        self._fh = self._sock = None


class ControlError(RuntimeError):
    pass
