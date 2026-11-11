import sqlite3
from datetime import datetime, timezone

import pytest
from jobagent.db import (
    JobDb,
    REPLY_CONFIRMED,
    REPLY_IGNORED,
    STATUS_APPROVED,
    STATUS_FILTERED_OUT,
    STATUS_NEW,
    STATUS_SCORED,
    STATUS_SENT,
    STATUS_SKIPPED,
    _make_key,
)
from jobagent.models import JobRecord


def make_job(**overrides) -> JobRecord:
    defaults = dict(
        source="workday:nvidia",
        company="Nvidia",
        title="Software Engineer",
        location="Tel Aviv, Israel",
        url="https://example.com/job/1",
        description="C++ required",
        posted_date="2025-10-15",
    )
    return JobRecord(**{**defaults, **overrides})


@pytest.fixture()
def db():
    with JobDb(":memory:") as d:
        yield d


# --- insert_if_new ---

def test_insert_new_job(db):
    job = make_job()
    assert db.insert_if_new(job) is True
    assert db.count() == 1


def test_insert_duplicate_url(db):
    job = make_job()
    db.insert_if_new(job)
    duplicate = make_job(title="Different Title")  # same URL
    assert db.insert_if_new(duplicate) is False
    assert db.count() == 1


def test_insert_duplicate_normalized_key(db):
    job = make_job(url="https://example.com/job/1")
    db.insert_if_new(job)
    # Same company+title+location, different URL
    same = make_job(url="https://example.com/job/2")
    assert db.insert_if_new(same) is False
    assert db.count() == 1


def test_insert_different_title_same_company(db):
    db.insert_if_new(make_job(title="SWE I", url="https://example.com/1"))
    db.insert_if_new(make_job(title="SWE II", url="https://example.com/2"))
    assert db.count() == 2


def test_insert_title_case_insensitive_dedup(db):
    db.insert_if_new(make_job(title="Software Engineer", url="https://a.com/1"))
    # Same title different casing → duplicate normalized key
    dup = make_job(title="software engineer", url="https://a.com/2")
    assert db.insert_if_new(dup) is False


# --- default status ---

def test_new_job_defaults_to_new_status(db):
    job = make_job()
    db.insert_if_new(job)
    jobs = db.get_by_status(STATUS_NEW)
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"


# --- update_job ---

def test_update_status(db):
    job = make_job()
    db.insert_if_new(job)
    db.update_job(job.url, status=STATUS_FILTERED_OUT)
    assert db.get_by_status(STATUS_NEW) == []
    assert len(db.get_by_status(STATUS_FILTERED_OUT)) == 1


def test_update_score_and_variant(db):
    job = make_job()
    db.insert_if_new(job)
    db.update_job(job.url, score=82, variant="systems")
    # Score/variant are not in get_by_status output but shouldn't crash
    jobs = db.get_by_status(STATUS_NEW)
    assert len(jobs) == 1


def test_update_description(db):
    job = make_job()
    db.insert_if_new(job)
    db.update_job(job.url, description="Full description fetched")
    jobs = db.get_by_status(STATUS_NEW)
    assert jobs[0].description == "Full description fetched"


def test_update_no_fields_is_noop(db):
    job = make_job()
    db.insert_if_new(job)
    db.update_job(job.url)  # no kwargs → should not raise
    assert db.count() == 1


# --- exists ---

def test_exists_true(db):
    job = make_job()
    db.insert_if_new(job)
    assert db.exists(job.url) is True


def test_exists_false(db):
    assert db.exists("https://nonexistent.com") is False


# --- _make_key ---

def test_make_key_normalizes():
    k1 = _make_key("Nvidia", "Software Engineer", "Tel Aviv, Israel")
    k2 = _make_key("NVIDIA", "software  engineer", "Tel Aviv, Israel")
    assert k1 == k2


def test_make_key_different_companies_differ():
    assert _make_key("Nvidia", "SWE", "TLV") != _make_key("Intel", "SWE", "TLV")


# --- dedupe across sources ---

def test_same_job_on_two_sources_is_one_row(db):
    db.insert_if_new(JobRecord(source="workday:nvidia", company="Nvidia", title="C++ Engineer",
                               location="Israel, Yokneam", url="https://a/1"))
    inserted = db.insert_if_new(JobRecord(source="jobify", company="Nvidia", title="C++ Engineer",
                                          location="יקנעם, Israel", url="https://b/2"))
    assert inserted is False


# --- pipeline state ---

def _insert(db, n=1, company="Nvidia", **updates):
    job = JobRecord(source="test", company=company, title=f"Role {n}", location="Israel",
                    url=f"https://x/{company}/{n}")
    db.insert_if_new(job)
    if updates:
        db.update_job(job.url, **updates)
    return job


def test_claim_only_succeeds_once(db):
    job = _insert(db, status=STATUS_SENT)
    job_id = db.id_for(job.url)
    assert db.claim(job_id, STATUS_SENT, STATUS_APPROVED) is True
    assert db.claim(job_id, STATUS_SENT, STATUS_APPROVED) is False


def test_retryable_new_excludes_old_jobs(db):
    job = _insert(db)
    db._conn.execute("UPDATE jobs SET fetched_at = '2020-01-01T00:00:00+00:00'")
    db._conn.commit()
    assert db.retryable_new() == []
    assert job.url not in [j.url for j in db.retryable_new()]


def test_pending_cards_best_first_and_above_threshold(db):
    _insert(db, 1, status=STATUS_SCORED, score=70)
    _insert(db, 2, status=STATUS_SCORED, score=90)
    _insert(db, 3, status=STATUS_SCORED, score=40)
    assert [r["score"] for r in db.pending_cards(60)] == [90, 70]


def test_count_sent_today_ignores_other_days(db):
    now = datetime.now(timezone.utc).isoformat()
    _insert(db, 1, status=STATUS_SENT, sent_at=now)
    _insert(db, 2, status=STATUS_SENT, sent_at="2020-01-01T00:00:00+00:00")
    assert db.count_sent_today() == 1


def test_count_open_cards_counts_undecided_only(db):
    now = datetime.now(timezone.utc).isoformat()
    _insert(db, 1, status=STATUS_SENT, sent_at=now)
    _insert(db, 2, status=STATUS_APPROVED, sent_at=now)
    _insert(db, 3, status=STATUS_SKIPPED, sent_at=now)
    _insert(db, 4, company="Intel", status=STATUS_SENT, sent_at=now)
    assert db.count_open_cards("NVIDIA") == 2


def test_requeue_only_touches_other_scorers(db):
    kw = _insert(db, 1, status=STATUS_SCORED, score=50, scored_by="keyword")
    llm = _insert(db, 2, status=STATUS_SCORED, score=50, scored_by="llm")
    assert db.requeue_not_scored_by("llm") == 1
    assert [j.url for j in db.retryable_new()] == [kw.url]
    assert llm.url in [r["url"] for r in db.pending_cards(0)]


def test_requeue_brings_old_keyword_jobs_back_into_retry_window(db):
    _insert(db, 1, status=STATUS_SCORED, score=50, scored_by="keyword")
    db._conn.execute("UPDATE jobs SET fetched_at = '2020-01-01T00:00:00+00:00'")
    db._conn.commit()
    db.requeue_not_scored_by("llm")
    assert len(db.retryable_new()) == 1


def test_opening_old_database_adds_new_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, company TEXT NOT NULL,
        title TEXT NOT NULL, location TEXT NOT NULL, url TEXT NOT NULL,
        normalized_key TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        posted_date TEXT NOT NULL DEFAULT '', fetched_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new', score INTEGER, variant TEXT NOT NULL DEFAULT '',
        UNIQUE(url), UNIQUE(normalized_key))""")
    conn.commit()
    conn.close()
    with JobDb(path) as db:
        job = _insert(db, status=STATUS_SCORED, score_json="{}", scored_by="llm",
                      recruiter_email="a@b.co")
        assert db.get_by_id(db.id_for(job.url))["recruiter_email"] == "a@b.co"


# --- outbox ---

def test_outbox_round_trip_keeps_hebrew(db):
    db.outbox_add("email", {"body": "סטטוס"})
    (item_id, channel, payload), = db.outbox_items()
    assert (channel, payload) == ("email", {"body": "סטטוס"})
    db.outbox_failed(item_id)
    assert len(db.outbox_items()) == 1
    db.outbox_done(item_id)
    assert db.outbox_items() == []


# --- reply proposals ---

def test_reply_proposal_lifecycle(db):
    pid = db.add_reply_proposal("<id@x>", "Nvidia", "SWE", "נדחה")
    assert db.seen_reply_ids() == {"<id@x>"}
    assert db.get_reply_proposal(pid)["company"] == "Nvidia"
    assert db.claim_reply_proposal(pid, REPLY_CONFIRMED) is True
    assert db.claim_reply_proposal(pid, REPLY_IGNORED) is False


def test_same_job_from_two_sources_is_one_row(tmp_path):
    """LinkedIn's alert and Check Point's own board list the same job differently."""
    from jobagent.db import JobDb
    from jobagent.models import JobRecord
    db = JobDb(tmp_path / "x.db")
    assert db.insert_if_new(JobRecord("linkedin_email", "Check Point Software Technologies Ltd.",
                                      "Software Engineer", "Tel Aviv, Israel", "https://linkedin.com/jobs/view/1"))
    assert not db.insert_if_new(JobRecord("smartrecruiters:x", "Check Point", "Software Engineer ",
                                          "Tel Aviv-Yafo, Tel Aviv District, Israel", "https://jobs.smartrecruiters.com/x/2"))
    db.close()


def test_open_cards_count_one_employer_under_any_name(tmp_path):
    from datetime import datetime, timezone
    from jobagent.db import STATUS_SENT, JobDb
    from jobagent.models import JobRecord
    db = JobDb(tmp_path / "x.db")
    now = datetime.now(timezone.utc).isoformat()
    for i, name in enumerate(("Check Point Software Technologies Ltd.", "Check Point")):
        job = JobRecord("t", name, f"Role {i}", "Israel", f"https://x/{i}")
        db.insert_if_new(job)
        db.update_job(job.url, status=STATUS_SENT, sent_at=now)
    assert db.count_open_cards("Check Point") == 2
    db.close()
