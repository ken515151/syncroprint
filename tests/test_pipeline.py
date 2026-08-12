import os
from unittest import mock

import pytest

from syncroprintd import config as cfgmod
from syncroprintd import pipeline as pl
from syncroprintd.store import Store


class FakePrinter:
    """Stands in for the cupsprint module."""

    def __init__(self, fail_times=0):
        self.calls = []
        self.cancelled = []
        self.fail_times = fail_times
        self.PrintError = pl.cupsprint.PrintError

    def submit(self, printer, path, *, copies=1, options=None, title=None, timeout=30):
        self.calls.append({"printer": printer, "path": path, "copies": copies,
                           "options": options or [], "title": title})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise pl.cupsprint.PrintError("jam")
        return f"{printer}-{len(self.calls)}"

    def cancel(self, cups_job_id, timeout=10):
        self.cancelled.append(cups_job_id)


class FakeResponse:
    def __init__(self, body=b"%PDF-1.4 fake", content_type="application/pdf", status=200,
                 location=None):
        self.body = body
        self.headers = {"Content-Type": content_type}
        if location:
            self.headers["Location"] = location
        self.status = status
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise pl.requests.HTTPError(f"{self.status}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        pass


def make_cfg(**over):
    data = {
        "account": {"host": "syncromsp.com", "subdomain": "exampleshop", "api_token": "tok"},
        "printers": {"a4": {"cups_name": "HP", "options": ["fit-to-page"]},
                     "label": {"cups_name": "QL", "options": ["media=Custom.62x100mm"]}},
        "routing": {
            "ticket": {"enabled": True, "auto_print": True, "printer": "a4", "quantity": 2},
            "invoice": {"enabled": False, "printer": "a4"},
            "asset_label": {"enabled": True, "printer": "label", "rotate": True},
        },
    }
    data.update(over)
    return cfgmod.from_dict(data)


def make_pipeline(tmp_path, cfg=None, printer=None, **kw):
    store = Store(str(tmp_path / "jobs.db"))
    p = pl.Pipeline(cfg or make_cfg(), store, spool_dir=str(tmp_path / "spool"),
                    printer_backend=printer or FakePrinter(), **kw)
    return p, store


def url(path="doc.pdf"):
    return f"https://exampleshop.syncromsp.com/files/{path}"


def drain(p):
    while not p._queue.empty():
        job = p._queue.get_nowait()
        if job:
            p._process(job)


# -- validation ----------------------------------------------------------

def test_validate_rejects_bad_urls():
    cfg = make_cfg()
    with pytest.raises(pl.PayloadError, match="non-HTTPS"):
        pl.validate_job(pl.Job("1", "ticket", "http://exampleshop.syncromsp.com/x.pdf"), cfg)
    with pytest.raises(pl.PayloadError, match="allowlist"):
        pl.validate_job(pl.Job("1", "ticket", "https://evil.example.com/x.pdf"), cfg)
    with pytest.raises(pl.PayloadError, match="allowlist"):
        pl.validate_job(pl.Job("1", "ticket", "https://exampleshop.syncromsp.com.evil.com/x.pdf"), cfg)
    with pytest.raises(pl.PayloadError, match="no file URL"):
        pl.validate_job(pl.Job("1", "ticket", None), cfg)
    pl.validate_job(pl.Job("1", "ticket", url()), cfg)  # good one passes
    pl.validate_job(pl.Job("1", "cash_drawer", None, register=True), cfg)  # register needs no URL


def test_validate_rejects_bad_fields():
    cfg = make_cfg()
    with pytest.raises(pl.PayloadError):
        pl.validate_job(pl.Job("", "ticket", url()), cfg)
    with pytest.raises(pl.PayloadError):
        pl.validate_job(pl.Job("1", "", url()), cfg)
    with pytest.raises(pl.PayloadError):
        pl.validate_job(pl.Job("1", "ticket", url(), copies=500), cfg)


# -- happy path ----------------------------------------------------------

def test_end_to_end_print(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        assert p.submit(pl.Job("100", "ticket", url(), title="Ticket #100"))
        drain(p)
    job = store.get_job("100")
    assert job["status"] == "printed"
    assert job["cups_job_id"] == "HP-1"
    call = printer.calls[0]
    assert call["copies"] == 2  # route quantity
    assert "fit-to-page" in call["options"] and "sides=one-sided" in call["options"]
    assert os.path.exists(call["path"])


def test_payload_copies_overrides_route_quantity(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("101", "ticket", url(), copies=5))
        drain(p)
    assert printer.calls[0]["copies"] == 5


def test_rotate_option_applied(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("102", "asset_label", url()))
        drain(p)
    assert "orientation-requested=4" in printer.calls[0]["options"]


# -- dedupe / routing ----------------------------------------------------

def test_duplicate_dropped(tmp_path):
    p, store = make_pipeline(tmp_path)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        assert p.submit(pl.Job("7", "ticket", url())) is True
        assert p.submit(pl.Job("7", "ticket", url())) is False


def test_unmapped_and_disabled_types_skipped(tmp_path):
    p, store = make_pipeline(tmp_path)
    p.submit(pl.Job("8", "estimate", url()))       # no route
    p.submit(pl.Job("9", "invoice", url()))        # route disabled
    drain(p)
    assert store.get_job("8")["status"] == "skipped"
    assert store.get_job("9")["status"] == "skipped"
    assert "disabled" in store.get_job("9")["error"]


def test_invalid_payload_recorded_visibly(tmp_path):
    p, store = make_pipeline(tmp_path)
    assert p.submit(pl.Job("10", "ticket", "https://evil.example.com/x.pdf")) is False
    hist = store.history(status="skipped")
    assert len(hist) == 1 and hist[0]["document_type"] == "invalid"


# -- failure handling ----------------------------------------------------

def test_download_failure_marks_failed(tmp_path):
    p, store = make_pipeline(tmp_path)
    with mock.patch.object(pl.requests, "get", side_effect=pl.requests.ConnectionError("net down")), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("11", "ticket", url()))
        drain(p)
    job = store.get_job("11")
    assert job["status"] == "failed"
    assert "download" in job["error"]


def test_bad_content_type_fails(tmp_path):
    p, store = make_pipeline(tmp_path)
    with mock.patch.object(pl.requests, "get",
                           return_value=FakeResponse(content_type="text/html")), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("12", "ticket", url()))
        drain(p)
    assert store.get_job("12")["status"] == "failed"


def test_print_retries_then_succeeds(tmp_path):
    printer = FakePrinter(fail_times=2)
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("13", "ticket", url()))
        drain(p)
    assert store.get_job("13")["status"] == "printed"
    assert len(printer.calls) == 3


def test_print_exhausts_retries_marks_failed(tmp_path):
    printer = FakePrinter(fail_times=99)
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("14", "ticket", url()))
        drain(p)
    job = store.get_job("14")
    assert job["status"] == "failed" and "print" in job["error"]
    assert len(printer.calls) == 3


# -- pause / resume / cancel --------------------------------------------

def test_pause_holds_resume_flushes(tmp_path):
    p, store = make_pipeline(tmp_path)
    p.pause()
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("15", "ticket", url()))
        assert store.get_job("15")["status"] == "queued"
        assert p._queue.empty()
        p.resume()
        drain(p)
    assert store.get_job("15")["status"] == "printed"


def test_cancel_held_job(tmp_path):
    p, store = make_pipeline(tmp_path)
    p.pause()
    p.submit(pl.Job("16", "ticket", url()))
    assert p.cancel("16") is True
    p.resume()
    drain(p)
    assert store.get_job("16")["status"] == "cancelled"
    assert p._queue.empty()


def test_cancel_unknown_job(tmp_path):
    p, store = make_pipeline(tmp_path)
    assert p.cancel("nope") is False


# -- dry run / reprint / retention / test print --------------------------

def test_dry_run_prints_nothing(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer, dry_run=True)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("17", "ticket", url()))
        drain(p)
    assert store.get_job("17")["status"] == "printed"
    assert printer.calls == []


def test_reprint_reuses_spool_file(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()) as get:
        p.submit(pl.Job("18", "ticket", url()))
        drain(p)
        assert get.call_count == 1
        assert p.reprint("18") is True
        drain(p)
        assert get.call_count == 1  # no second download
    assert len(printer.calls) == 2
    assert printer.calls[0]["path"] == printer.calls[1]["path"]


def test_reprint_refetches_when_spool_gone(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()) as get:
        p.submit(pl.Job("19", "ticket", url()))
        drain(p)
        os.remove(printer.calls[0]["path"])
        assert p.reprint("19") is True
        drain(p)
        assert get.call_count == 2
    assert len(printer.calls) == 2


def test_retention_sweep(tmp_path):
    p, store = make_pipeline(tmp_path)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("20", "ticket", url()))
        drain(p)
    spool = store.get_job("20")["spool_path"]
    assert os.path.exists(spool)
    p.cfg.retention.failed_spool_days = 0
    assert p.retention_sweep() == 1
    assert not os.path.exists(spool)
    assert store.get_job("20")["spool_path"] is None


def test_test_print(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    assert p.test_print("label") is True
    assert printer.calls[0]["printer"] == "QL"
    assert p.test_print("bogus") is False


# -- wire payload mapping / protocol gates -------------------------------

def wire_payload(**over):
    p = {"document": "Ticket", "type": "Ticket", "file": url("t.pdf"),
         "location": None, "register": None, "autoprinted": False}
    p.update(over)
    return p


def test_job_from_payload_maps_fields():
    job = pl.job_from_payload(wire_payload(location=1019, register=2748, autoprinted=True))
    assert job.document_type == "ticket"  # normalized to lowercase routing key
    assert job.file_url == url("t.pdf")
    assert job.location_id == 1019
    assert job.register_id == 2748
    assert job.autoprinted is True
    assert job.id  # derived
    # same payload -> same id (dedupe on redelivery); different file -> different id
    assert pl.job_from_payload(wire_payload()).id == pl.job_from_payload(wire_payload()).id
    assert pl.job_from_payload(wire_payload(file=url("other.pdf"))).id != job.id


def test_job_from_payload_null_handling():
    job = pl.job_from_payload(wire_payload(location=None, register=None, autoprinted=None))
    assert job.location_id is None and job.register_id is None and job.autoprinted is False


@pytest.mark.parametrize("bad", [
    {"type": "Ticket", "file": "x"},           # missing document
    {"document": "Ticket", "type": "Ticket"},  # missing file
    {"document": "", "type": "", "file": ""},
    "not a dict",
])
def test_job_from_payload_rejects_malformed(bad):
    with pytest.raises(pl.PayloadError):
        pl.job_from_payload(bad)


def test_autoprinted_gate(tmp_path):
    cfg = make_cfg()
    cfg.routing["ticket"].auto_print = False
    p, store = make_pipeline(tmp_path, cfg=cfg)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("30", "ticket", url(), autoprinted=True))
        p.submit(pl.Job("31", "ticket", url("b.pdf"), autoprinted=False))
        drain(p)
    assert store.get_job("30")["status"] == "skipped"   # auto job, auto_print off
    assert store.get_job("31")["status"] == "printed"   # manual job still prints


def test_location_filter(tmp_path):
    cfg = make_cfg()
    cfg.location_id = 1019
    p, store = make_pipeline(tmp_path, cfg=cfg)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("32", "ticket", url(), location_id=2000))
        p.submit(pl.Job("33", "ticket", url("b.pdf"), location_id=1019))
        p.submit(pl.Job("34", "ticket", url("c.pdf"), location_id=None))
        drain(p)
    assert store.get_job("32")["status"] == "skipped"
    assert store.get_job("33")["status"] == "printed"
    assert store.get_job("34")["status"] == "printed"   # untagged always prints


def test_vendor_pdf_hosts_allowed():
    """Live finding: job PDFs come from pdf.repairshopr.com even for Syncro
    accounts (shared backend). Both vendor domains must pass the allowlist."""
    cfg = make_cfg()  # a syncromsp.com account
    pl.validate_job(pl.Job("1", "ticket", "https://pdf.repairshopr.com/x/y.pdf"), cfg)
    pl.validate_job(pl.Job("1", "ticket", "https://pdf.syncromsp.com/x/y.pdf"), cfg)
    with pytest.raises(pl.PayloadError, match="allowlist"):
        pl.validate_job(pl.Job("1", "ticket", "https://pdf.repairshopr.com.evil.com/x.pdf"), cfg)


def test_cups_verify_flips_printed_to_failed(tmp_path):
    import time as _time

    class VerifyingPrinter(FakePrinter):
        outcome = "failed"

        def job_outcome(self, cups_job_id, timeout=10):
            return self.outcome

    printer = VerifyingPrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    p.verify_delay_s = 0.05
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("40", "ticket", url()))
        drain(p)
    assert store.get_job("40")["status"] == "printed"
    deadline = _time.time() + 5
    while _time.time() < deadline and store.get_job("40")["status"] == "printed":
        _time.sleep(0.05)
    job = store.get_job("40")
    assert job["status"] == "failed"
    assert "CUPS cancelled/aborted" in job["error"]


def test_cups_verify_ok_keeps_printed(tmp_path):
    import time as _time

    class VerifyingPrinter(FakePrinter):
        def job_outcome(self, cups_job_id, timeout=10):
            return "ok"

    p, store = make_pipeline(tmp_path, printer=VerifyingPrinter())
    p.verify_delay_s = 0.05
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("41", "ticket", url()))
        drain(p)
    _time.sleep(0.3)
    assert store.get_job("41")["status"] == "printed"


def test_no_job_outcome_backend_skips_verify(tmp_path):
    p, store = make_pipeline(tmp_path)  # plain FakePrinter, no job_outcome
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("42", "ticket", url()))
        drain(p)
    assert store.get_job("42")["status"] == "printed"


def test_resolved_copies_recorded_on_job_row(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    with mock.patch.object(pl.requests, "get", return_value=FakeResponse()):
        p.submit(pl.Job("50", "ticket", url()))       # route quantity = 2
        p.submit(pl.Job("51", "ticket", url("b.pdf"), copies=5))  # payload override
        drain(p)
    assert store.get_job("50")["copies"] == 2
    assert store.get_job("51")["copies"] == 5


def test_redirect_to_https_is_followed(tmp_path):
    printer = FakePrinter()
    p, store = make_pipeline(tmp_path, printer=printer)
    hops = [FakeResponse(status=302, location="https://pdf.repairshopr.com/real.pdf"),
            FakeResponse()]
    with mock.patch.object(pl.requests, "get", side_effect=hops) as get:
        p.submit(pl.Job("60", "ticket", url()))
        drain(p)
    assert store.get_job("60")["status"] == "printed"
    assert get.call_count == 2
    assert get.call_args_list[1].args[0] == "https://pdf.repairshopr.com/real.pdf"
    assert get.call_args_list[0].kwargs["allow_redirects"] is False


def test_redirect_to_plain_http_is_refused(tmp_path):
    p, store = make_pipeline(tmp_path)
    hops = [FakeResponse(status=302, location="http://pdf.repairshopr.com/real.pdf")] * 3
    with mock.patch.object(pl.requests, "get", side_effect=hops), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("61", "ticket", url()))
        drain(p)
    job = store.get_job("61")
    assert job["status"] == "failed"
    assert "non-HTTPS" in job["error"]


def test_endless_redirects_refused(tmp_path):
    p, store = make_pipeline(tmp_path)
    bouncer = FakeResponse(status=302, location=url("loop.pdf"))
    with mock.patch.object(pl.requests, "get", return_value=bouncer), \
         mock.patch.object(pl.time, "sleep"):
        p.submit(pl.Job("62", "ticket", url()))
        drain(p)
    job = store.get_job("62")
    assert job["status"] == "failed"
    assert "redirect" in job["error"]
