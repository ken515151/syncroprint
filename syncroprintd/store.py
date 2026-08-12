"""SQLite persistence for syncroprintd: job history, dedupe, poller cursors.

One database at /var/lib/syncroprint/jobs.db. Job rows are kept forever
(they are tiny and form the audit history); spool files are cleaned up
separately by the pipeline's retention sweep.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

DEFAULT_DB_PATH = "/var/lib/syncroprint/jobs.db"

# Job lifecycle. A stuck job is not a status — it's an active status older
# than the stuck threshold, computed at query time.
ACTIVE_STATUSES = ("received", "queued", "downloading", "printing")
TERMINAL_STATUSES = ("printed", "failed", "skipped", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    document_type TEXT NOT NULL,
    title         TEXT,
    received_at   REAL NOT NULL,
    updated_at    REAL NOT NULL,
    printed_at    REAL,
    printer       TEXT,
    copies        INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,
    error         TEXT,
    cups_job_id   TEXT,
    spool_path    TEXT,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_received ON jobs(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe wrapper around the daemon's SQLite database.

    All daemon threads (transport, pipeline worker, control socket) share
    one Store; a single lock serialises access, which is far below any
    contention that would matter at print-shop volumes.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- dedupe / job lifecycle ------------------------------------------

    def add_job(self, job_id: str, document_type: str, *, title: str | None = None,
                copies: int = 1, payload: dict[str, Any] | None = None,
                status: str = "received") -> bool:
        """Insert a new job. Returns False (and changes nothing) if the
        job_id was already seen — this is the dedupe gate."""
        now = time.time()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO jobs (job_id, document_type, title, received_at, updated_at,"
                    " copies, status, payload) VALUES (?,?,?,?,?,?,?,?)",
                    (job_id, document_type, title, now, now, copies, status,
                     json.dumps(payload) if payload is not None else None),
                )
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def set_status(self, job_id: str, status: str, *, error: str | None = None,
                   printer: str | None = None, cups_job_id: str | None = None,
                   spool_path: str | None = None, copies: int | None = None) -> None:
        now = time.time()
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [status, now]
        if copies is not None:
            sets.append("copies = ?")
            args.append(copies)
        if error is not None:
            sets.append("error = ?")
            args.append(error)
        if printer is not None:
            sets.append("printer = ?")
            args.append(printer)
        if cups_job_id is not None:
            sets.append("cups_job_id = ?")
            args.append(cups_job_id)
        if spool_path is not None:
            sets.append("spool_path = ?")
            args.append(spool_path)
        if status == "printed":
            sets.append("printed_at = ?")
            args.append(now)
        args.append(job_id)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", args)
            self._db.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    # -- queries for applet ----------------------------------------------

    def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def history(self, *, status: str | None = None, document_type: str | None = None,
                since: float | None = None, until: float | None = None,
                search: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, args = [], []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if document_type:
            clauses.append("document_type = ?")
            args.append(document_type)
        if since is not None:
            clauses.append("received_at >= ?")
            args.append(since)
        if until is not None:
            clauses.append("received_at <= ?")
            args.append(until)
        if search:
            clauses.append("(job_id LIKE ? OR title LIKE ? OR error LIKE ?)")
            like = f"%{search}%"
            args.extend([like, like, like])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(limit)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs {where} ORDER BY received_at DESC, rowid DESC LIMIT ?", args
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def active_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})"
                " ORDER BY received_at",
                ACTIVE_STATUSES,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stuck_jobs(self, threshold_s: float) -> list[dict[str, Any]]:
        cutoff = time.time() - threshold_s
        return [j for j in self.active_jobs()
                if j["status"] in ("downloading", "printing") and j["_updated_ts"] <= cutoff]

    def spool_paths_older_than(self, days: float) -> list[tuple[str, str]]:
        """(job_id, spool_path) rows in terminal states whose spool file is
        past retention and can be deleted."""
        cutoff = time.time() - days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id, spool_path FROM jobs WHERE spool_path IS NOT NULL"
                f" AND status IN ({','.join('?' * len(TERMINAL_STATUSES))}) AND updated_at <= ?",
                (*TERMINAL_STATUSES, cutoff),
            ).fetchall()
        return [(r["job_id"], r["spool_path"]) for r in rows]

    def clear_spool_path(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET spool_path = NULL WHERE job_id = ?", (job_id,))
            self._db.commit()

    # -- poller cursors / misc state -------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._db.commit()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["_received_ts"] = d["received_at"]
        d["_updated_ts"] = d["updated_at"]
        d["received_at"] = _iso(d["received_at"])
        d["updated_at"] = _iso(d["updated_at"])
        d["printed_at"] = _iso(d["printed_at"])
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except (ValueError, TypeError):
                pass
        return d
