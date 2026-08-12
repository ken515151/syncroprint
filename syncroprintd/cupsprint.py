"""CUPS submission via the `lp` command line.

Deliberately subprocess-based rather than pycups: no compiled dependency,
and `lp`/`lpstat`/`cancel` are stable, scriptable interfaces that have not
changed in decades.
"""

from __future__ import annotations

import re
import subprocess

_REQUEST_ID_RE = re.compile(r"request id is (\S+)")


class PrintError(RuntimeError):
    """lp/cancel returned non-zero or produced unparseable output."""


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def list_printers(timeout: float = 10) -> list[str]:
    """Names of printers currently accepting jobs (`lpstat -a`)."""
    proc = _run(["lpstat", "-a"], timeout)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        # A machine with no queues yet is a normal state, not an error:
        # lpstat exits 1 with "lpstat: No destinations added."
        if "no destinations" in err.lower():
            return []
        raise PrintError(f"lpstat -a failed: {err}")
    printers = []
    for line in proc.stdout.splitlines():
        # "HP_LaserJet accepting requests since ..."
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "accepting":
            printers.append(parts[0])
    return printers


def printer_supports_duplex(printer: str, timeout: float = 10) -> bool:
    """True if the printer's PPD advertises a duplex option (`lpoptions -p X -l`)."""
    proc = _run(["lpoptions", "-p", printer, "-l"], timeout)
    if proc.returncode != 0:
        return False
    return any("duplex" in line.lower() for line in proc.stdout.splitlines())


def submit(printer: str, path: str, *, copies: int = 1,
           options: list[str] | None = None, title: str | None = None,
           timeout: float = 30) -> str:
    """Submit a file to CUPS; returns the CUPS request id (e.g. 'HP_LaserJet-42')."""
    argv = ["lp", "-d", printer, "-n", str(copies)]
    if title:
        argv += ["-t", title[:120]]
    for opt in options or []:
        argv += ["-o", opt]
    argv.append(path)
    try:
        proc = _run(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        raise PrintError(f"lp timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise PrintError(f"lp failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    match = _REQUEST_ID_RE.search(proc.stdout)
    if not match:
        raise PrintError(f"lp succeeded but no request id in output: {proc.stdout.strip()!r}")
    return match.group(1)


def cancel(cups_job_id: str, timeout: float = 10) -> None:
    proc = _run(["cancel", cups_job_id], timeout)
    if proc.returncode != 0:
        raise PrintError(f"cancel {cups_job_id} failed: {proc.stderr.strip() or proc.stdout.strip()}")


def _job_listed(argv: list[str], cups_job_id: str, timeout: float) -> bool | None:
    """True/False if the listing ran; None if the lpstat variant is
    unsupported (older CUPS)."""
    proc = _run(argv, timeout)
    if proc.returncode != 0:
        return None
    return any(line.split() and line.split()[0] == cups_job_id
               for line in proc.stdout.splitlines())


def job_outcome(cups_job_id: str, timeout: float = 10) -> str:
    """What became of a submitted CUPS job.

    Returns "active" (still queued/printing), "ok" (completed
    successfully), "failed" (cancelled or aborted by CUPS — e.g. a backend
    like cups-pdf died after accepting the job), or "unknown" (this CUPS
    can't tell us — older than 2.4, which added `lpstat -W successful`).
    """
    if _job_listed(["lpstat", "-o"], cups_job_id, timeout):
        return "active"
    successful = _job_listed(["lpstat", "-W", "successful", "-o"], cups_job_id, timeout)
    if successful is None:
        return "unknown"
    if successful:
        return "ok"
    completed = _job_listed(["lpstat", "-W", "completed", "-o"], cups_job_id, timeout)
    if completed:
        return "failed"     # finished but not successfully = cancelled/aborted
    return "unknown"        # fell out of CUPS history entirely
