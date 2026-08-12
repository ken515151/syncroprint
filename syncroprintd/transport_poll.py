"""Poller transport — fallback + gap-fill (§5.4).

Phase 0 established (PROTOCOL.md §10) that the AutoPrintr protocol has **no
job-list endpoint**: realtime delivery is fire-and-forget. So this poller
sweeps standard Syncro REST resources (tickets/invoices updated since a
cursor) on a best-effort basis. Sources are pluggable; if an item exposes no
PDF URL field the source contributes nothing and says so once in the log —
in that case the poller's value is liveness/visibility, not replay.

Duplicate-print safety: poller payloads carry a `_poll_id` derived from the
REST resource, so re-sweeping the same window is idempotent. Cross-transport
duplicates (same document seen via Pusher and poller with different ids)
are avoided by only sweeping while realtime is down, plus one gap-fill
sweep on each reconnect covering the outage window.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from .config import Account
from .store import Store

log = logging.getLogger("syncroprintd.poller")

_TIMEOUT = (10, 30)
CURSOR_KEY = "poll_cursor"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RestSweepSource:
    """Sweeps one REST collection for recently updated printable documents.

    Emits print-job-shaped payloads. Looks for any of the known PDF URL
    fields on each item; if the account's API surface exposes none, this
    source yields nothing (logged once).
    """

    PDF_FIELDS = ("pdf_url", "ticket_pdf_url", "invoice_pdf_url", "file_url")

    def __init__(self, resource: str, document_type: str):
        self.resource = resource            # e.g. "tickets"
        self.document_type = document_type  # e.g. "Ticket"
        self._warned_no_pdf = False

    def fetch(self, account: Account, since_iso: str) -> list[dict[str, Any]]:
        url = f"{account.base_url}/api/v1/{self.resource}"
        resp = requests.get(url, params={"api_key": account.api_token,
                                         "since_updated_at": since_iso},
                            timeout=_TIMEOUT, verify=True)
        if resp.status_code in (401, 403):
            raise PermissionError(f"{self.resource}: token not authorized (HTTP {resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        items = data.get(self.resource) if isinstance(data, dict) else None
        if not isinstance(items, list):
            log.debug("%s sweep: unexpected response shape", self.resource)
            return []
        payloads = []
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                continue
            pdf = next((item[f] for f in self.PDF_FIELDS
                        if isinstance(item.get(f), str) and item[f]), None)
            if not pdf:
                if not self._warned_no_pdf:
                    log.info("%s sweep: items carry no PDF URL field — "
                             "poller cannot replay these (see PROTOCOL.md §10)", self.resource)
                    self._warned_no_pdf = True
                continue
            payloads.append({
                "document": self.document_type,
                "type": None,
                "file": pdf,
                "location": item.get("location_id"),
                "register": None,
                "autoprinted": True,   # nobody clicked Print on this client
                "_poll_id": f"{self.resource}-{item['id']}",
            })
        return payloads


DEFAULT_SOURCES = (RestSweepSource("tickets", "Ticket"),
                   RestSweepSource("invoices", "Invoice"))


class PollerTransport:
    """Periodic sweep loop. `active` controls whether timed sweeps run
    (True while realtime is down or in poll-only mode); `sweep_once()` is
    called by the daemon on every websocket reconnect as the gap-fill."""

    def __init__(self, account_provider: Callable[[], Account], store: Store,
                 on_job: Callable[[dict[str, Any]], None],
                 interval_s: int = 60,
                 sources=DEFAULT_SOURCES,
                 on_state: Callable[[str], None] = lambda s: None):
        self.account_provider = account_provider
        self.store = store
        self.on_job = on_job
        self.interval_s = interval_s
        self.sources = list(sources)
        self.on_state = on_state
        self.active = False
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.store.get_meta(CURSOR_KEY) is None:
            # First run: don't replay the whole account history.
            self.store.set_meta(CURSOR_KEY, _utcnow_iso())
        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def set_active(self, active: bool) -> None:
        """Realtime down → True (timed sweeps). Realtime up → False."""
        if active and not self.active:
            log.info("poller activated (realtime unavailable)")
        self.active = active
        if active:
            self._wake.set()

    def mark_current(self) -> None:
        """Advance the cursor to now without sweeping — called while realtime
        is healthy so a later outage only sweeps the outage window."""
        self.store.set_meta(CURSOR_KEY, _utcnow_iso())

    def sweep_once(self) -> int:
        """One sweep from the stored cursor; returns number of jobs emitted.
        Used both by the timed loop and as the reconnect gap-fill."""
        since = self.store.get_meta(CURSOR_KEY) or _utcnow_iso()
        account = self.account_provider()
        emitted = 0
        ok = True
        for source in list(self.sources):
            try:
                for payload in source.fetch(account, since):
                    self.on_job(payload)
                    emitted += 1
            except PermissionError as exc:
                # Expected with the AutoPrinter App Center token: it has no
                # REST API scopes, so resource sweeps 401. Say so once and
                # retire the source instead of warning every sweep. (A new
                # poller is built on account change, re-enabling them.)
                log.info("%s — expected with an AutoPrinter card token; "
                         "disabling %s replay (see PROTOCOL.md §10)",
                         exc, source.resource)
                self.sources.remove(source)
            except (requests.RequestException, ValueError) as exc:
                log.warning("poll sweep failed for %s: %s", source.resource, exc)
                ok = False
        if ok:
            # Only advance past the window once every source covered it.
            self.store.set_meta(CURSOR_KEY, _utcnow_iso())
        if emitted:
            log.info("poll sweep emitted %d job(s)", emitted)
        return emitted

    def _run(self) -> None:
        while not self._stopping:
            self._wake.wait(timeout=self.interval_s)
            self._wake.clear()
            if self._stopping:
                break
            if not self.active:
                continue
            try:
                self.sweep_once()
                self.on_state("degraded")   # polling works; realtime is down
            except Exception:
                log.exception("poll sweep crashed")
                self.on_state("error")
