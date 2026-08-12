import subprocess
from unittest import mock

import pytest

from syncroprintd import cupsprint


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_submit_builds_argv_and_parses_request_id():
    with mock.patch.object(cupsprint, "_run",
                           return_value=completed("request id is HP_LaserJet-42 (1 file(s))\n")) as run:
        job_id = cupsprint.submit("HP_LaserJet", "/spool/x.pdf", copies=2,
                                  options=["fit-to-page", "sides=two-sided-long-edge"],
                                  title="Invoice 9")
    assert job_id == "HP_LaserJet-42"
    argv = run.call_args[0][0]
    assert argv[:5] == ["lp", "-d", "HP_LaserJet", "-n", "2"]
    assert argv[-1] == "/spool/x.pdf"
    assert "-t" in argv and "Invoice 9" in argv
    i = argv.index("-o")
    assert argv[i + 1] == "fit-to-page"
    assert argv.count("-o") == 2


def test_submit_failure_raises():
    with mock.patch.object(cupsprint, "_run",
                           return_value=completed(stderr="lp: The printer is not responding.", code=1)):
        with pytest.raises(cupsprint.PrintError, match="not responding"):
            cupsprint.submit("HP_LaserJet", "/spool/x.pdf")


def test_submit_timeout_raises():
    with mock.patch.object(cupsprint, "_run",
                           side_effect=subprocess.TimeoutExpired(cmd="lp", timeout=30)):
        with pytest.raises(cupsprint.PrintError, match="timed out"):
            cupsprint.submit("HP_LaserJet", "/spool/x.pdf")


def test_submit_unparseable_output_raises():
    with mock.patch.object(cupsprint, "_run", return_value=completed("something odd")):
        with pytest.raises(cupsprint.PrintError, match="no request id"):
            cupsprint.submit("HP_LaserJet", "/spool/x.pdf")


def test_list_printers_parses_lpstat():
    out = ("HP_LaserJet accepting requests since Tue 12 Aug 2026\n"
           "Brother_QL accepting requests since Tue 12 Aug 2026\n"
           "Old_Epson not accepting requests since Mon\n")
    with mock.patch.object(cupsprint, "_run", return_value=completed(out)):
        assert cupsprint.list_printers() == ["HP_LaserJet", "Brother_QL"]


def test_list_printers_error():
    with mock.patch.object(cupsprint, "_run", return_value=completed(stderr="lpstat: no CUPS", code=1)):
        with pytest.raises(cupsprint.PrintError):
            cupsprint.list_printers()


def test_duplex_detection():
    out = "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n"
    with mock.patch.object(cupsprint, "_run", return_value=completed(out)):
        assert cupsprint.printer_supports_duplex("HP_LaserJet") is True
    with mock.patch.object(cupsprint, "_run", return_value=completed("PageSize: A4 Letter\n")):
        assert cupsprint.printer_supports_duplex("Brother_QL") is False


def test_cancel():
    with mock.patch.object(cupsprint, "_run", return_value=completed()) as run:
        cupsprint.cancel("HP_LaserJet-42")
    assert run.call_args[0][0] == ["cancel", "HP_LaserJet-42"]
    with mock.patch.object(cupsprint, "_run", return_value=completed(stderr="no such job", code=1)):
        with pytest.raises(cupsprint.PrintError):
            cupsprint.cancel("HP_LaserJet-42")


def test_list_printers_no_destinations_is_empty_not_error():
    with mock.patch.object(cupsprint, "_run",
                           return_value=completed(stderr="lpstat: No destinations added.", code=1)):
        assert cupsprint.list_printers() == []


def _stat_seq(responses):
    """Map lpstat argv -> canned output for job_outcome tests."""
    def run(argv, timeout):
        key = " ".join(argv)
        out, code = responses.get(key, ("", 0))
        return completed(out, code=code)
    return run


def test_job_outcome_states():
    active = {"lpstat -o": ("PDF-42 syncroprint 1024 Tue\n", 0)}
    with mock.patch.object(cupsprint, "_run", side_effect=_stat_seq(active)):
        assert cupsprint.job_outcome("PDF-42") == "active"

    ok = {"lpstat -o": ("", 0),
          "lpstat -W successful -o": ("PDF-42 syncroprint 1024 Tue\n", 0)}
    with mock.patch.object(cupsprint, "_run", side_effect=_stat_seq(ok)):
        assert cupsprint.job_outcome("PDF-42") == "ok"

    failed = {"lpstat -o": ("", 0),
              "lpstat -W successful -o": ("", 0),
              "lpstat -W completed -o": ("PDF-42 syncroprint 1024 Tue\n", 0)}
    with mock.patch.object(cupsprint, "_run", side_effect=_stat_seq(failed)):
        assert cupsprint.job_outcome("PDF-42") == "failed"

    old_cups = {"lpstat -o": ("", 0),
                "lpstat -W successful -o": ("lpstat: bad option", 1)}
    with mock.patch.object(cupsprint, "_run", side_effect=_stat_seq(old_cups)):
        assert cupsprint.job_outcome("PDF-42") == "unknown"

    vanished = {"lpstat -o": ("", 0),
                "lpstat -W successful -o": ("", 0),
                "lpstat -W completed -o": ("", 0)}
    with mock.patch.object(cupsprint, "_run", side_effect=_stat_seq(vanished)):
        assert cupsprint.job_outcome("PDF-42") == "unknown"
