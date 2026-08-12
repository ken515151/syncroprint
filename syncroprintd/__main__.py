"""syncroprintd — daemon entry point (§5.1).

    python3 -m syncroprintd [--config PATH] [--db PATH] [--spool DIR]
                            [--socket PATH] [--dry-run] [--verbose]

Lifecycle: load config → open SQLite → start control socket → start pipeline
→ start transports (Pusher primary, poller fallback/gap-fill) → run until
SIGTERM. Runs as the `syncroprint` system user under systemd; also runs in
the foreground for development.
"""

from __future__ import annotations

import argparse
import collections
import logging
import signal
import threading
import time
from typing import Any

from . import __version__, api, config as cfgmod, control, cupsprint
from .pipeline import Pipeline, PayloadError, job_from_payload
from .store import Store
from .transport_poll import PollerTransport
from .transport_pusher import DEFAULT_APP_KEY, PusherTransport

log = logging.getLogger("syncroprintd")


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records for the applet's Error log window
    (journald has the full history; this avoids shelling out to journalctl)."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


class Daemon:
    def __init__(self, cfg: cfgmod.Config, store: Store, pipeline: Pipeline,
                 config_path: str):
        self.cfg = cfg
        self.store = store
        self.pipeline = pipeline
        self.config_path = config_path
        # unconfigured | starting | connected | degraded | disconnected | error
        # ("paused" is reported by status() from the pipeline flag)
        self.state = "starting" if cfg.account.is_configured else "unconfigured"
        self.transport_name = "none"
        self._channel: str | None = None
        self._channel_lock = threading.Lock()
        self.log_handler = RingBufferHandler()
        logging.getLogger().addHandler(self.log_handler)
        self.pusher: PusherTransport | None = None
        self.poller: PollerTransport | None = None
        self._stop_event = threading.Event()

    # -- channel ----------------------------------------------------------

    def _get_channel(self) -> str:
        """Channel provider for the Pusher transport; fetched lazily and
        cached, refetched after auth/config changes."""
        with self._channel_lock:
            if not self._channel:
                data = api.fetch_printing_settings(self.cfg.account)
                self._channel = data["messaging_channel"]
                log.info("acquired messaging channel")
            return self._channel

    def _invalidate_channel(self) -> None:
        with self._channel_lock:
            self._channel = None

    # -- transport wiring -------------------------------------------------

    def start_transports(self) -> None:
        if not self.cfg.account.is_configured:
            log.info("account not configured yet — transports idle until "
                     "subdomain + API token are entered (applet Settings)")
            self.state = "unconfigured"
            return
        mode = self.cfg.transport.mode
        self.poller = PollerTransport(
            account_provider=lambda: self.cfg.account,
            store=self.store,
            on_job=self._on_wire_payload,
            interval_s=self.cfg.transport.poll_interval_s,
            on_state=self._on_transport_state,
        )
        self.poller.start()
        if mode in ("auto", "pusher"):
            self.pusher = PusherTransport(
                app_key=self.cfg.pusher_app_key or DEFAULT_APP_KEY,
                channel_provider=self._get_channel,
                on_job=self._on_wire_payload,
                on_connect=self._on_pusher_connect,
                on_state=self._on_transport_state,
            )
            self.pusher.start()
            if mode == "auto":
                self.poller.set_active(True)  # active until first WS connect
        else:
            self.transport_name = "poller"
            self.poller.set_active(True)

    def _on_pusher_connect(self) -> None:
        self.transport_name = "pusher"
        self.state = "connected"
        if self.cfg.transport.mode == "auto" and self.poller:
            # Gap-fill: one sweep covering the window we were down, then idle.
            try:
                self.poller.sweep_once()
            except Exception:
                log.exception("gap-fill sweep failed")
            self.poller.set_active(False)
            self.poller.mark_current()

    def _on_transport_state(self, state: str) -> None:
        if state == "connected":
            self.state = "connected"
        elif state == "disconnected":
            self.state = "degraded" if self.cfg.transport.mode == "auto" else "disconnected"
            if self.cfg.transport.mode == "auto" and self.poller:
                self.poller.set_active(True)
        elif state in ("degraded", "error"):
            if self.state != "connected":
                self.state = state

    def _on_wire_payload(self, payload: dict[str, Any]) -> None:
        try:
            job = job_from_payload(payload)
        except PayloadError as exc:
            log.warning("dropping malformed payload: %s", exc)
            return
        self.pipeline.submit(job)

    # -- control-surface operations --------------------------------------

    def status(self) -> dict[str, Any]:
        active = self.store.active_jobs()
        stuck = self.store.stuck_jobs(self.cfg.timeouts.stuck_flag_s)
        return {
            "version": __version__,
            "state": "paused" if self.pipeline.paused else self.state,
            "transport": self.transport_name,
            "paused": self.pipeline.paused,
            "dry_run": self.pipeline.dry_run,
            "active_jobs": len(active),
            "stuck_jobs": [j["job_id"] for j in stuck],
        }

    def log_tail(self, n: int) -> list[str]:
        return list(self.log_handler.records)[-n:]

    def list_system_printers(self) -> list[dict[str, Any]]:
        try:
            names = cupsprint.list_printers()
        except cupsprint.PrintError as exc:
            # Settings must stay usable (e.g. account setup) even if CUPS
            # is unreachable — report an empty list, keep the detail in logs.
            log.warning("cannot list CUPS printers: %s", exc)
            return []
        return [{"name": name, "duplex": cupsprint.printer_supports_duplex(name)}
                for name in names]

    def update_config(self, update: dict[str, Any]) -> None:
        new_cfg = cfgmod.apply_update(self.cfg, update)
        cfgmod.save(new_cfg, self.config_path)
        old_account = self.cfg.account
        self.cfg = new_cfg
        self.pipeline.set_config(new_cfg)
        if self.poller:
            self.poller.interval_s = new_cfg.transport.poll_interval_s
        if vars(new_cfg.account) != vars(old_account):
            # Account changed (including first-run setup from the applet):
            # drop the cached channel and bring transports up/down to match.
            self._invalidate_channel()
            self.restart_transports()
        log.info("config updated and saved")

    def restart_transports(self) -> None:
        if self.pusher:
            self.pusher.stop()
            self.pusher = None
        if self.poller:
            self.poller.stop()
            self.poller = None
        self.transport_name = "none"
        self.state = "starting" if self.cfg.account.is_configured else "unconfigured"
        self.start_transports()

    def test_account(self, host: str | None, subdomain: str | None,
                     api_token: str | None) -> tuple[bool, str]:
        """Try the given credentials against settings/printing WITHOUT
        saving them — backs the applet's 'Test connection' button. A masked
        token (all asterisks) means 'test with the saved one'."""
        token = (api_token or "").strip()
        if token and set(token) == {"*"}:
            token = self.cfg.account.api_token
        acct = cfgmod.Account(host=host or "syncromsp.com",
                              subdomain=(subdomain or "").strip().lower(),
                              api_token=token)
        if not acct.is_configured:
            return False, "enter both subdomain and API token first"
        if acct.host not in cfgmod.KNOWN_HOSTS:
            return False, f"unknown host {acct.host!r}"
        return api.test_connection(acct)

    def reload_config(self) -> None:
        new_cfg = cfgmod.load(self.config_path)
        self.cfg = new_cfg
        self.pipeline.set_config(new_cfg)
        self._invalidate_channel()
        log.info("config reloaded from disk")

    # -- maintenance loop --------------------------------------------------

    def maintenance_loop(self) -> None:
        """Daily retention sweep (§5.3); cheap enough to run hourly."""
        while not self._stop_event.wait(timeout=3600):
            try:
                self.pipeline.retention_sweep()
            except Exception:
                log.exception("retention sweep failed")

    def stop(self) -> None:
        self._stop_event.set()
        if self.pusher:
            self.pusher.stop()
        if self.poller:
            self.poller.stop()
        self.pipeline.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="syncroprintd")
    parser.add_argument("--config", default=cfgmod.DEFAULT_CONFIG_PATH)
    parser.add_argument("--db", default=None, help="SQLite path (default /var/lib/syncroprint/jobs.db)")
    parser.add_argument("--spool", default=None, help="spool dir (default /var/lib/syncroprint/spool)")
    parser.add_argument("--socket", default=control.DEFAULT_SOCKET_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + route + log, but never call lp")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as exc:
        if "not found" in str(exc):
            # Fresh install: run unconfigured; the applet's Settings window
            # completes setup over the control socket.
            log.warning("no config file at %s — starting unconfigured", args.config)
            cfg = cfgmod.Config()
            try:
                cfgmod.save(cfg, args.config)
            except OSError as save_exc:
                log.warning("could not create config file: %s", save_exc)
        else:
            log.error("cannot start: %s", exc)
            return 1

    from .store import DEFAULT_DB_PATH
    from .pipeline import DEFAULT_SPOOL_DIR
    store = Store(args.db or DEFAULT_DB_PATH)
    pipeline = Pipeline(cfg, store, spool_dir=args.spool or DEFAULT_SPOOL_DIR,
                        dry_run=args.dry_run)
    daemon = Daemon(cfg, store, pipeline, config_path=args.config)

    server = control.ControlServer(control.Dispatcher(daemon), address=args.socket)
    server.start()
    pipeline.start()
    daemon.start_transports()
    threading.Thread(target=daemon.maintenance_loop, name="maintenance", daemon=True).start()

    stop = threading.Event()

    def _term(signum, frame):
        log.info("received signal %d, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log.info("syncroprintd %s up (dry_run=%s)", __version__, args.dry_run)
    while not stop.is_set():
        time.sleep(0.5)

    server.stop()
    daemon.stop()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
