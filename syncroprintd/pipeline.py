"""Job pipeline: dedupe → fetch PDF → route → print → record.

Transports (Pusher or poller) hand normalized `Job` objects to
`Pipeline.submit()`. A single worker thread processes them with per-job
timeouts so one stuck job can never block the queue (§5.3, §6).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from . import cupsprint
from .config import Config
from .store import Store

log = logging.getLogger("syncroprintd.pipeline")

DEFAULT_SPOOL_DIR = "/var/lib/syncroprint/spool"
MAX_PDF_BYTES = 25 * 1024 * 1024
_ACCEPTED_CONTENT_TYPES = ("application/pdf", "application/octet-stream", "binary/octet-stream")


class PayloadError(ValueError):
    """Inbound job payload failed validation — the channel is untrusted input."""


# Canonical document types (PROTOCOL.md §6). The wire value is the WPF enum
# member name matched case-insensitively; we normalize to lowercase and use
# these as routing keys in the config.
DOCUMENT_TYPES = ("invoice", "estimate", "ticket", "intakeform", "receipt",
                  "zreport", "ticketreceipt", "popdrawer", "adjustment",
                  "customerid", "asset", "ticketlabel", "outtakeform")


@dataclass
class Job:
    """Normalized print job, independent of which transport delivered it."""
    id: str
    document_type: str
    file_url: str | None
    title: str | None = None
    copies: int | None = None       # payload override; None → route quantity
    location_id: int | None = None
    register_id: int | None = None  # POS register the job targets (0/None = any)
    autoprinted: bool = False       # server automation trigger vs manual Print click
    register: bool = False          # cash-drawer pop, no document
    raw: dict[str, Any] = field(default_factory=dict)


def job_from_payload(payload: dict[str, Any]) -> Job:
    """Map an AutoPrintr `print-job` wire payload to a Job.

    Wire schema (PROTOCOL.md §5): {document, type, file, location, register,
    autoprinted} — required strings document/file/type; location/register
    nullable ints; autoprinted nullable bool. There is NO job id on the wire,
    so we derive a stable one from the payload: the file URL is unique per
    job event, which makes redeliveries (Pusher retry, poller overlap)
    hash identically and fall into the dedupe gate.
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload is not a JSON object")
    document = payload.get("document")
    file_url = payload.get("file")
    if not isinstance(document, str) or not document:
        raise PayloadError("missing 'document' field")
    if not isinstance(file_url, str) or not file_url:
        raise PayloadError("missing 'file' field")
    doc_type = document.strip().lower()
    if doc_type not in DOCUMENT_TYPES:
        log.warning("unknown document type %r — routing as-is", document)
    poll_id = payload.get("_poll_id")
    if isinstance(poll_id, str) and poll_id:
        # Poller jobs key off the REST resource id so re-sweeps are idempotent.
        digest = poll_id
    else:
        digest = hashlib.sha256(f"{doc_type}|{file_url}".encode()).hexdigest()[:24]
    location = payload.get("location") or None
    register = payload.get("register") or None
    size = payload.get("type")
    return Job(
        id=digest,
        document_type=doc_type,
        file_url=file_url,
        title=f"{document} ({size})" if isinstance(size, str) and size else document,
        location_id=int(location) if isinstance(location, int) else None,
        register_id=int(register) if isinstance(register, int) else None,
        autoprinted=bool(payload.get("autoprinted") or False),
        raw={k: v for k, v in payload.items()},
    )


def validate_job(job: Job, cfg: Config) -> None:
    """Reject anything malformed or outside our allowlist before it
    touches the network or a printer."""
    if not job.id or not isinstance(job.id, str) or len(job.id) > 128:
        raise PayloadError("missing or oversized job id")
    if not job.document_type or not isinstance(job.document_type, str) or len(job.document_type) > 64:
        raise PayloadError("missing or oversized document_type")
    if job.copies is not None and not (1 <= job.copies <= 99):
        raise PayloadError(f"copies out of range: {job.copies}")
    if not job.register:
        if not job.file_url:
            if job.raw.get("_reuse_spool"):  # reprint from retained spool file
                return
            raise PayloadError("document job has no file URL")
        parts = urlsplit(job.file_url)
        if parts.scheme != "https":
            raise PayloadError(f"refusing non-HTTPS URL: {job.file_url[:80]}")
        host = parts.hostname or ""
        allowed = cfg.account.pdf_allowed_hosts
        if host not in allowed and not any(host.endswith("." + a) for a in allowed):
            raise PayloadError(f"refusing URL host outside account allowlist: {host}")


class Pipeline:
    def __init__(self, cfg: Config, store: Store, *, spool_dir: str = DEFAULT_SPOOL_DIR,
                 dry_run: bool = False,
                 auth_headers: Callable[[], dict[str, str]] | None = None,
                 printer_backend=cupsprint,
                 verify_delay_s: float = 8.0, verify_max_attempts: int = 15):
        self.cfg = cfg
        self.store = store
        self.spool_dir = spool_dir
        self.dry_run = dry_run
        self.auth_headers = auth_headers or (lambda: {})
        self.printer = printer_backend
        self.verify_delay_s = verify_delay_s
        self.verify_max_attempts = verify_max_attempts
        self.paused = False
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._held: list[Job] = []          # jobs received while paused
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stopping = False
        os.makedirs(spool_dir, exist_ok=True)

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stopping = True
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=10)

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg

    # -- intake -----------------------------------------------------------

    def submit(self, job: Job) -> bool:
        """Dedupe-gate and enqueue. Returns False if job was already seen."""
        try:
            validate_job(job, self.cfg)
        except PayloadError as exc:
            log.warning("rejected payload: %s", exc)
            # record under a synthetic id so bad payloads are visible in History
            self.store.add_job(f"invalid-{uuid.uuid4().hex[:12]}", "invalid",
                               title=str(exc)[:200], status="skipped", payload=job.raw)
            return False
        if job.file_url:
            job.raw.setdefault("_file_url", job.file_url)  # retained for Reprint
        if not self.store.add_job(job.id, job.document_type, title=job.title,
                                  copies=job.copies or 0, payload=job.raw):
            log.debug("duplicate job %s dropped", job.id)
            return False
        with self._lock:
            if self.paused:
                self.store.set_status(job.id, "queued")
                self._held.append(job)
                log.info("job %s queued (paused)", job.id)
                return True
        self._queue.put(job)
        return True

    def pause(self) -> None:
        with self._lock:
            self.paused = True
        log.info("pipeline paused")

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            held, self._held = self._held, []
        for job in held:
            self._queue.put(job)
        log.info("pipeline resumed, %d held job(s) flushed", len(held))

    def cancel(self, job_id: str) -> bool:
        """Abort an in-flight download, or cancel the CUPS job if spooled."""
        row = self.store.get_job(job_id)
        if not row:
            return False
        event = self._cancels.get(job_id)
        if event:
            event.set()
        with self._lock:
            self._held = [j for j in self._held if j.id != job_id]
        if row.get("cups_job_id") and row["status"] == "printing":
            try:
                self.printer.cancel(row["cups_job_id"])
            except cupsprint.PrintError as exc:
                log.warning("CUPS cancel of %s failed: %s", row["cups_job_id"], exc)
        if row["status"] not in ("printed",):
            self.store.set_status(job_id, "cancelled")
        log.info("job %s cancelled", job_id)
        return True

    def reprint(self, job_id: str) -> bool:
        """Re-run a completed/failed job, reusing the retained spool file
        when it still exists, else re-fetching from the recorded payload."""
        row = self.store.get_job(job_id)
        if not row:
            return False
        spool = row.get("spool_path")
        raw = row.get("payload") or {}
        job = Job(id=f"{job_id}-reprint-{uuid.uuid4().hex[:8]}",
                  document_type=row["document_type"],
                  file_url=None if (spool and os.path.exists(spool)) else raw.get("_file_url"),
                  title=row.get("title"), raw=raw)
        if spool and os.path.exists(spool):
            job.raw["_reuse_spool"] = spool
        elif not job.file_url:
            log.warning("reprint %s: no spool file and no recorded URL", job_id)
            return False
        return self.submit(job)

    def test_print(self, printer_key: str) -> bool:
        """Print a small generated test page to the named configured printer."""
        printer = self.cfg.printers.get(printer_key)
        if not printer:
            return False
        path = os.path.join(self.spool_dir, "testpage.pdf")
        _write_test_pdf(path)
        job_id = f"test-{uuid.uuid4().hex[:8]}"
        self.store.add_job(job_id, "test_page", title=f"Test page → {printer.cups_name}")
        self._print_file(job_id, path, printer.cups_name, 1,
                         [o for o in printer.options])
        return True

    # -- worker -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping:
            job = self._queue.get()
            if job is None:
                break
            try:
                self._process(job)
            except Exception:
                log.exception("unexpected error processing job %s", job.id)
                self.store.set_status(job.id, "failed", error="internal error (see log)")
            finally:
                self._cancels.pop(job.id, None)

    def _process(self, job: Job) -> None:
        cancel = threading.Event()
        self._cancels[job.id] = cancel

        route = self.cfg.route_for(job.document_type)
        if job.register:
            self._pop_register(job)
            return
        # Location filter (client-side, like the original): a job tagged for
        # another location is not ours. Untagged jobs always print.
        if (job.location_id and self.cfg.location_id
                and job.location_id != self.cfg.location_id):
            self.store.set_status(job.id, "skipped",
                                  error=f"job is for location {job.location_id}")
            return
        if route is None or not route.enabled:
            reason = "no routing configured" if route is None else "type disabled"
            log.info("job %s (%s) skipped: %s", job.id, job.document_type, reason)
            self.store.set_status(job.id, "skipped", error=reason)
            return
        # Original truth table: print iff enabled && (auto_print || !autoprinted).
        if job.autoprinted and not route.auto_print:
            self.store.set_status(job.id, "skipped", error="auto print disabled for type")
            return
        printer = self.cfg.printer_for(route)
        if printer is None:
            self.store.set_status(job.id, "skipped", error=f"printer {route.printer!r} not configured")
            return

        reuse = job.raw.get("_reuse_spool")
        if reuse and os.path.exists(reuse):
            path = reuse
        else:
            self.store.set_status(job.id, "downloading")
            try:
                path = self._download(job, cancel)
            except _Cancelled:
                self.store.set_status(job.id, "cancelled")
                return
            except Exception as exc:
                log.error("job %s download failed: %s", job.id, exc)
                self.store.set_status(job.id, "failed", error=f"download: {exc}")
                return
            self.store.set_status(job.id, "downloading", spool_path=path)
        if cancel.is_set():
            self.store.set_status(job.id, "cancelled")
            return

        copies = job.copies or route.quantity
        options = list(printer.options) + route.lp_options()
        self._print_file(job.id, path, printer.cups_name, copies, options,
                         title=job.title or f"{job.document_type} {job.id}")

    def _print_file(self, job_id: str, path: str, cups_name: str, copies: int,
                    options: list[str], title: str | None = None) -> None:
        # Copies are resolved here (payload override or route quantity), so
        # record the real number on the job row for the History view.
        self.store.set_status(job_id, "printing", printer=cups_name, copies=copies)
        if self.dry_run:
            log.info("[dry-run] would print %s on %s ×%d opts=%s", path, cups_name, copies, options)
            self.store.set_status(job_id, "printed", error="dry-run")
            return
        last_error = None
        for attempt in range(3):  # 1 try + 2 retries (§5.3)
            try:
                cups_id = self.printer.submit(
                    cups_name, path, copies=copies, options=options, title=title,
                    timeout=self.cfg.timeouts.print_submit_s)
                self.store.set_status(job_id, "printed", cups_job_id=cups_id)
                log.info("job %s printed on %s as %s", job_id, cups_name, cups_id)
                self._schedule_cups_verify(job_id, cups_id)
                return
            except cupsprint.PrintError as exc:
                last_error = exc
                log.warning("job %s print attempt %d failed: %s", job_id, attempt + 1, exc)
                time.sleep(min(2 ** attempt, 5))
        self.store.set_status(job_id, "failed", error=f"print: {last_error}")

    def _download(self, job: Job, cancel: threading.Event) -> str:
        dest = os.path.join(self.spool_dir, f"{_safe_name(job.id)}.pdf")
        last_error: Exception | None = None
        for attempt in range(3):
            if cancel.is_set():
                raise _Cancelled()
            try:
                with requests.get(job.file_url, stream=True, verify=True,
                                  headers=self.auth_headers(),
                                  timeout=(10, self.cfg.timeouts.download_s)) as resp:
                    resp.raise_for_status()
                    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if ctype and ctype not in _ACCEPTED_CONTENT_TYPES:
                        raise PayloadError(f"unexpected content-type {ctype!r}")
                    size = 0
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if cancel.is_set():
                                raise _Cancelled()
                            size += len(chunk)
                            if size > MAX_PDF_BYTES:
                                raise PayloadError("file exceeds 25 MB limit")
                            fh.write(chunk)
                    if size == 0:
                        raise PayloadError("empty file")
                    return dest
            except _Cancelled:
                raise
            except (requests.RequestException, PayloadError, OSError) as exc:
                last_error = exc
                log.warning("job %s download attempt %d failed: %s", job.id, attempt + 1, exc)
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(str(last_error))

    def _pop_register(self, job: Job) -> None:
        key = self.cfg.register_printer
        if not key or key not in self.cfg.printers:
            self.store.set_status(job.id, "skipped", error="no register printer configured")
            return
        printer = self.cfg.printers[key]
        path = os.path.join(self.spool_dir, "drawer_pop.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\x1b\x70\x00\x19\xfa\n")  # ESC/POS drawer kick, standard pulse
        self._print_file(job.id, path, printer.cups_name, 1, ["raw"], title="cash drawer")

    # -- CUPS outcome verification ----------------------------------------

    def _schedule_cups_verify(self, job_id: str, cups_id: str, attempt: int = 1) -> None:
        """'printed' means CUPS accepted the job — but a backend can still
        cancel/abort it afterwards (e.g. cups-pdf unable to write output).
        Re-check the CUPS job a little later and flip the row to failed if
        CUPS reports it didn't actually complete."""
        if not hasattr(self.printer, "job_outcome"):
            return
        timer = threading.Timer(self.verify_delay_s, self._verify_cups,
                                args=(job_id, cups_id, attempt))
        timer.daemon = True
        timer.start()

    def _verify_cups(self, job_id: str, cups_id: str, attempt: int) -> None:
        try:
            outcome = self.printer.job_outcome(cups_id)
        except Exception as exc:
            log.debug("CUPS verify of %s skipped: %s", cups_id, exc)
            return
        if outcome == "active":
            if attempt < self.verify_max_attempts:
                self._schedule_cups_verify(job_id, cups_id, attempt + 1)
            return
        if outcome == "failed":
            row = self.store.get_job(job_id)
            if row and row["status"] == "printed":  # don't clobber a user cancel
                self.store.set_status(
                    job_id, "failed",
                    error=f"CUPS cancelled/aborted job {cups_id} after accepting it "
                          "— check the printer/backend (journalctl, CUPS error_log)")
                log.warning("job %s: CUPS reports %s did not complete", job_id, cups_id)
        # "ok" and "unknown": leave the row as printed

    # -- maintenance ------------------------------------------------------

    def retention_sweep(self) -> int:
        """Delete spool files past retention; keep the job rows. Returns count."""
        removed = 0
        for job_id, path in self.store.spool_paths_older_than(self.cfg.retention.failed_spool_days):
            try:
                if os.path.exists(path):
                    os.remove(path)
                removed += 1
            except OSError as exc:
                log.warning("could not remove spool file %s: %s", path, exc)
                continue
            self.store.clear_spool_path(job_id)
        if removed:
            log.info("retention sweep removed %d spool file(s)", removed)
        return removed


class _Cancelled(Exception):
    pass


def _safe_name(job_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in job_id)[:100]


_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 80>>stream
BT /F1 18 Tf 72 770 Td (SyncroPrint test page) Tj 0 -24 Td (Printing works.) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""


def _write_test_pdf(path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(_MINIMAL_PDF)
