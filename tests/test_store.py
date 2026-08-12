import time

from syncroprintd.store import Store


def make_store(tmp_path):
    return Store(str(tmp_path / "jobs.db"))


def test_add_job_dedupe(tmp_path):
    s = make_store(tmp_path)
    assert s.add_job("42", "ticket", payload={"a": 1}) is True
    assert s.add_job("42", "ticket") is False  # duplicate dropped
    assert s.add_job("43", "invoice") is True
    s.close()


def test_status_lifecycle(tmp_path):
    s = make_store(tmp_path)
    s.add_job("42", "ticket", title="Ticket #42", copies=2)
    s.set_status("42", "downloading")
    s.set_status("42", "printing", printer="HP_LaserJet", spool_path="/spool/42.pdf")
    s.set_status("42", "printed", cups_job_id="HP_LaserJet-7")
    job = s.get_job("42")
    assert job["status"] == "printed"
    assert job["printer"] == "HP_LaserJet"
    assert job["cups_job_id"] == "HP_LaserJet-7"
    assert job["printed_at"] is not None
    assert job["copies"] == 2
    assert job["payload"] is None
    s.close()


def test_failed_records_error(tmp_path):
    s = make_store(tmp_path)
    s.add_job("f1", "invoice")
    s.set_status("f1", "failed", error="lp exploded")
    assert s.get_job("f1")["error"] == "lp exploded"
    s.close()


def test_recent_jobs_order_and_limit(tmp_path):
    s = make_store(tmp_path)
    for i in range(15):
        s.add_job(str(i), "ticket")
    recent = s.recent_jobs(10)
    assert len(recent) == 10
    ids = [j["job_id"] for j in recent]
    assert ids == sorted(ids, key=int, reverse=True)
    s.close()


def test_history_filters(tmp_path):
    s = make_store(tmp_path)
    s.add_job("1", "ticket", title="Bob's laptop")
    s.add_job("2", "invoice")
    s.set_status("1", "printed")
    s.set_status("2", "failed", error="printer on fire")
    assert [j["job_id"] for j in s.history(status="failed")] == ["2"]
    assert [j["job_id"] for j in s.history(document_type="ticket")] == ["1"]
    assert [j["job_id"] for j in s.history(search="laptop")] == ["1"]
    assert [j["job_id"] for j in s.history(search="on fire")] == ["2"]
    assert len(s.history()) == 2
    s.close()


def test_active_and_stuck(tmp_path):
    s = make_store(tmp_path)
    s.add_job("a", "ticket")
    s.set_status("a", "downloading")
    s.add_job("b", "ticket")
    s.set_status("b", "printed")
    active = s.active_jobs()
    assert [j["job_id"] for j in active] == ["a"]
    assert s.stuck_jobs(threshold_s=3600) == []
    assert [j["job_id"] for j in s.stuck_jobs(threshold_s=0)] == ["a"]
    s.close()


def test_spool_retention_query(tmp_path):
    s = make_store(tmp_path)
    s.add_job("old", "ticket")
    s.set_status("old", "printed", spool_path="/spool/old.pdf")
    s.add_job("active", "ticket")
    s.set_status("active", "downloading", spool_path="/spool/active.pdf")
    assert s.spool_paths_older_than(days=1) == []          # too recent
    assert s.spool_paths_older_than(days=0) == [("old", "/spool/old.pdf")]  # active excluded
    s.clear_spool_path("old")
    assert s.spool_paths_older_than(days=0) == []
    s.close()


def test_meta_cursor(tmp_path):
    s = make_store(tmp_path)
    assert s.get_meta("poll_cursor") is None
    assert s.get_meta("poll_cursor", "0") == "0"
    s.set_meta("poll_cursor", "2026-08-12T10:00:00Z")
    s.set_meta("poll_cursor", "2026-08-12T11:00:00Z")
    assert s.get_meta("poll_cursor") == "2026-08-12T11:00:00Z"
    s.close()
