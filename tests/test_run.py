"""Tests for run.py pipeline logic. External calls are mocked; SQLite is real,
because most of what can go wrong here is about state surviving between cycles."""
import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.db import (
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_FILTERED_OUT,
    STATUS_NEW,
    STATUS_SCORE_FAILED,
    STATUS_SCORED,
    STATUS_SENT,
    STATUS_SKIPPED,
    JobDb,
)
from jobagent.models import JobRecord
from jobagent.notify import Notifier
from jobagent.scorer import LLMUnavailable, ScoreResult
from jobagent.tracker import ApplicationRow, STATUS_REJECTED, STATUS_REPLIED, STATUS_SUBMITTED
import run
from run import _filter, _register_handlers, run_fetch_cycle, run_prep_retries, run_reply_scan
from run import _send_prep, _start_prep as real_start_prep  # before the autouse patch below


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_job(title="C++ Engineer", company="Nvidia", location="Israel",
             url="https://example.com/1", description="Develop C++ systems.", source="test"):
    return JobRecord(source=source, company=company, title=title,
                     location=location, url=url, description=description)


def make_jobs(n):
    """n jobs at n different companies, so the per-company limit stays out of the way."""
    return [make_job(url=f"https://example.com/{i}", title=f"Engineer {i}", company=f"Co{i}")
            for i in range(n)]


def make_settings(threshold=60, card_cap=10, company_cap=2, api_key="test-key", dry_run=False):
    s = MagicMock()
    s.match_threshold = threshold
    s.daily_card_cap = card_cap
    s.max_apps_per_company_30d = company_cap
    s.anthropic_api_key = api_key
    s.dry_run = dry_run
    s.tracker_sheet_id = "sheet"
    return s


def make_score(score=80, variant="systems"):
    return ScoreResult(score=score, variant=variant, seniority_ok=True, matched=["C++"],
                       reason="Strong C++ match.")


@pytest.fixture
def db(tmp_path):
    db = JobDb(tmp_path / "t.db")
    yield db
    db.close()


def make_tracker(duplicate=False, apps_30d=0):
    t = MagicMock()
    t.is_duplicate.return_value = duplicate
    t.apps_in_last_30_days.return_value = apps_30d
    t.read_all.return_value = []
    return t


def make_bot():
    bot = MagicMock()
    bot.send_job_card = AsyncMock(return_value=101)  # the card's Telegram message id
    bot.send_bump = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_approved_portal = AsyncMock()
    bot.send_status_proposal = AsyncMock()
    bot.send_document = AsyncMock()
    return bot


@pytest.fixture(autouse=True)
def start_prep():
    """Handlers start the prep sheet as a background task. Tests that aren't about
    it would otherwise race a stray task, so it's a recorded no-op by default."""
    with patch("run._start_prep") as m:
        yield m


def status_of(db, url):
    return db.get_by_id(db.id_for(url))["status"]


# ---------------------------------------------------------------------------
# _filter
# ---------------------------------------------------------------------------

def test_filter_passes_israel_junior():
    assert _filter(make_job()) is True


def test_filter_rejects_senior_title():
    assert _filter(make_job(title="Senior C++ Engineer")) is False


def test_filter_rejects_non_israel():
    assert _filter(make_job(location="New York")) is False


def test_filter_rejects_5plus_years_description():
    assert _filter(make_job(description="Requires 5+ years of experience.")) is False


def test_filter_keeps_israeli_jobs_that_need_clearance():
    """Daniel can obtain an Israeli clearance (2026-11-11)."""
    assert _filter(make_job(description="Security clearance required for this position.")) is True


def test_filter_rejects_foreign_clearance_requirement():
    assert _filter(make_job(location="Remote", description="Active TS/SCI clearance required.")) is False


# ---------------------------------------------------------------------------
# run_fetch_cycle: what gets a card
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run._fetch_all", return_value=[])
async def test_fetch_cycle_no_jobs(mock_fetch, db):
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    assert db.count() == 0


@pytest.fixture
def llm():
    """Fake the expensive parts of the LLM path: score 80, tailor, no PDF."""
    with patch("run.score_job", return_value=make_score()) as score, \
            patch("run.tailor_summary", return_value="Tailored."), \
            patch("run.render_cv", return_value=None):
        yield score


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_card_sent_above_threshold(mock_fetch, llm, db):
    job = make_job()
    mock_fetch.return_value = [job]
    bot = make_bot()
    await run_fetch_cycle(make_settings(threshold=60), db, make_tracker(), bot)
    bot.send_job_card.assert_awaited_once()
    assert status_of(db, job.url) == STATUS_SENT


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_card_id_is_the_database_id(mock_fetch, llm, db):
    """Ids that reset on restart let an old button approve a different job."""
    job = make_job()
    mock_fetch.return_value = [job]
    bot = make_bot()
    await run_fetch_cycle(make_settings(), db, make_tracker(), bot)
    assert bot.send_job_card.await_args.args[0].job_id == db.id_for(job.url)


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_no_card_below_threshold(mock_fetch, llm, db):
    mock_fetch.return_value = [make_job()]
    llm.return_value = make_score(score=40)
    bot = make_bot()
    await run_fetch_cycle(make_settings(threshold=60), db, make_tracker(), bot)
    bot.send_job_card.assert_not_awaited()


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_same_job_next_cycle_gets_no_second_card(mock_fetch, llm, db):
    mock_fetch.return_value = [make_job()]
    bot = make_bot()
    settings = make_settings()
    await run_fetch_cycle(settings, db, make_tracker(), bot)
    await run_fetch_cycle(settings, db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 1
    assert llm.call_count == 1


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_job_already_in_sheet_is_filtered(mock_fetch, llm, db):
    job = make_job()
    mock_fetch.return_value = [job]
    bot = make_bot()
    await run_fetch_cycle(make_settings(), db, make_tracker(duplicate=True), bot)
    bot.send_job_card.assert_not_awaited()
    llm.assert_not_called()
    assert status_of(db, job.url) == STATUS_FILTERED_OUT


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_free_filters_run_before_anything_costs_money(mock_fetch, llm, db):
    mock_fetch.return_value = [
        make_job(title="Senior C++ Engineer", url="https://example.com/a"),
        make_job(location="Virtual/Remote USA", url="https://example.com/b"),
    ]
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    llm.assert_not_called()
    assert db.count() == 0


# ---------------------------------------------------------------------------
# Daily cap and per-company limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_cap_is_per_day_not_per_cycle(mock_fetch, llm, db):
    mock_fetch.return_value = make_jobs(5)
    bot = make_bot()
    settings = make_settings(card_cap=2)
    await run_fetch_cycle(settings, db, make_tracker(), bot)
    await run_fetch_cycle(settings, db, make_tracker(), bot)  # the evening fetch
    assert bot.send_job_card.await_count == 2


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_jobs_over_the_cap_wait_for_the_next_day(mock_fetch, llm, db):
    mock_fetch.return_value = make_jobs(3)
    bot = make_bot()
    settings = make_settings(card_cap=2)
    await run_fetch_cycle(settings, db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 2

    db._conn.execute("UPDATE jobs SET sent_at = '2020-01-01T00:00:00+00:00' WHERE sent_at IS NOT NULL")
    db._conn.commit()  # it's tomorrow
    await run_fetch_cycle(settings, db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 3


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_best_scores_go_first(mock_fetch, llm, db):
    mock_fetch.return_value = make_jobs(3)
    llm.side_effect = [make_score(70), make_score(95), make_score(80)]
    bot = make_bot()
    await run_fetch_cycle(make_settings(card_cap=1), db, make_tracker(), bot)
    assert bot.send_job_card.await_args.args[0].match_pct == 95


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_company_limit_applies_within_a_cycle(mock_fetch, llm, db):
    """Ten good Nvidia roles should still only produce two cards."""
    mock_fetch.return_value = [make_job(url=f"https://example.com/{i}", title=f"Role {i}")
                               for i in range(5)]
    bot = make_bot()
    await run_fetch_cycle(make_settings(company_cap=2), db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 2


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_company_limit_counts_undecided_cards_across_cycles(mock_fetch, llm, db):
    settings = make_settings(company_cap=2)
    bot = make_bot()
    for batch in range(2):
        mock_fetch.return_value = [make_job(url=f"https://example.com/{batch}-{i}",
                                            title=f"Role {batch} {i}") for i in range(2)]
        await run_fetch_cycle(settings, db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 2


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_company_at_limit_in_sheet_is_filtered(mock_fetch, llm, db):
    job = make_job()
    mock_fetch.return_value = [job]
    bot = make_bot()
    await run_fetch_cycle(make_settings(company_cap=2), db, make_tracker(apps_30d=2), bot)
    bot.send_job_card.assert_not_awaited()
    assert status_of(db, job.url) == STATUS_FILTERED_OUT


# ---------------------------------------------------------------------------
# Descriptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run._fetch_description", return_value="Full description, long enough to count as real text.")
@patch("run._fetch_all")
async def test_stub_description_is_fetched_and_stored(mock_fetch, mock_desc, llm, db):
    job = make_job(description="", source="jobify")
    mock_fetch.return_value = [job]
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    assert db.get_by_id(db.id_for(job.url))["description"].startswith("Full description")
    assert llm.call_args.args[0].description.startswith("Full description")


@pytest.mark.asyncio
@patch("run._fetch_description", side_effect=["", "Now the page loads, with enough text."])
@patch("run._fetch_all")
async def test_job_without_description_waits_instead_of_being_scored(mock_fetch, mock_desc, llm, db):
    job = make_job(description="", source="linkedin_email")
    mock_fetch.return_value = [job]
    settings = make_settings()
    await run_fetch_cycle(settings, db, make_tracker(), make_bot())
    llm.assert_not_called()
    assert status_of(db, job.url) == STATUS_NEW

    await run_fetch_cycle(settings, db, make_tracker(), make_bot())
    llm.assert_called_once()


# ---------------------------------------------------------------------------
# When things fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_api_outage_alerts_once_and_keeps_jobs_queued(mock_fetch, llm, db):
    jobs = make_jobs(3)
    mock_fetch.return_value = jobs
    llm.side_effect = LLMUnavailable("credit balance too low")
    bot = make_bot()
    await run_fetch_cycle(make_settings(), db, make_tracker(), bot)

    assert llm.call_count == 1  # stopped at the first failure
    assert bot.send_message.await_count == 1
    assert "credit balance" in bot.send_message.await_args.args[0]
    assert all(status_of(db, j.url) == STATUS_NEW for j in jobs)

    llm.side_effect = None
    llm.return_value = make_score()
    await run_fetch_cycle(make_settings(), db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 3


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_job_the_model_cant_score_is_not_retried(mock_fetch, llm, db):
    job = make_job()
    mock_fetch.return_value = [job]
    llm.return_value = None
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    assert status_of(db, job.url) == STATUS_SCORE_FAILED
    assert llm.call_count == 1


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_failed_card_doesnt_stop_the_rest_and_retries(mock_fetch, llm, db):
    jobs = make_jobs(2)
    mock_fetch.return_value = jobs
    bot = make_bot()
    bot.send_job_card.side_effect = [RuntimeError("telegram"), None]
    await run_fetch_cycle(make_settings(), db, make_tracker(), bot)
    statuses = sorted(status_of(db, j.url) for j in jobs)
    assert statuses == [STATUS_SCORED, STATUS_SENT]

    bot.send_job_card.side_effect = None
    await run_fetch_cycle(make_settings(), db, make_tracker(), bot)
    assert all(status_of(db, j.url) == STATUS_SENT for j in jobs)


# ---------------------------------------------------------------------------
# Keyword mode and switching to the LLM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run.keyword_score_job")
@patch("run.base_summary", return_value="Base summary.")
@patch("run.render_cv", return_value=None)
@patch("run._fetch_all")
async def test_no_api_key_uses_keyword_scorer(mock_fetch, mock_render, mock_base, mock_kw, db):
    mock_fetch.return_value = [make_job()]
    mock_kw.return_value = make_score(score=60)
    bot = make_bot()
    with patch("run.score_job") as mock_llm:
        await run_fetch_cycle(make_settings(api_key=""), db, make_tracker(), bot)
        mock_llm.assert_not_called()
    assert bot.send_job_card.await_args.args[0].score_label == "Keyword match"


@pytest.mark.asyncio
@patch("run.keyword_score_job")
@patch("run.base_summary", return_value="Base summary.")
@patch("run._fetch_all")
async def test_adding_api_key_rescores_keyword_jobs(mock_fetch, mock_base, mock_kw, llm, db):
    mock_fetch.return_value = [make_job()]
    mock_kw.return_value = make_score(score=5)  # below even the keyword threshold
    await run_fetch_cycle(make_settings(api_key=""), db, make_tracker(), make_bot())

    bot = make_bot()
    await run_fetch_cycle(make_settings(api_key="key"), db, make_tracker(), bot)
    llm.assert_called_once()
    assert bot.send_job_card.await_args.args[0].score_label == "Match"


@pytest.mark.asyncio
@patch("run.keyword_score_job")
@patch("run.base_summary", return_value="Base summary.")
@patch("run.render_cv", return_value=None)
@patch("run._fetch_all")
async def test_dry_run_caps_at_five(mock_fetch, mock_render, mock_base, mock_kw, db):
    mock_fetch.return_value = make_jobs(10)
    mock_kw.return_value = make_score(score=60)
    bot = make_bot()
    await run_fetch_cycle(make_settings(api_key="", card_cap=10, dry_run=True),
                          db, make_tracker(), bot)
    assert bot.send_job_card.await_count == 5


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

class Harness:
    """A job with a card sent, plus the handlers run.py registers."""

    def __init__(self, db, tmp_path, *, email="", dry_run=False, with_pdf=True,
                 url="https://example.com/1"):
        self.db, self.tracker, self.bot = db, make_tracker(), make_bot()
        self.settings = make_settings(dry_run=dry_run)
        self.job = make_job(company="KLA", title="R&D Engineer", url=url)
        db.insert_if_new(self.job)
        self.pdf = tmp_path / "cv.pdf"
        self.pdf.write_bytes(b"%PDF")
        db.update_job(
            self.job.url, status=STATUS_SENT, score=80, variant="systems", scored_by="llm",
            score_json=json.dumps(dataclasses.asdict(make_score())),
            sent_at=datetime.now(timezone.utc).isoformat(),
            pdf_path=str(self.pdf) if with_pdf else None, recruiter_email=email,
        )
        self.job_id = db.id_for(self.job.url)
        self.notifier = Notifier(self.settings, db, self.bot)
        _register_handlers(self.bot, self.settings, db, self.tracker, self.notifier)

    @property
    def approve(self):
        return self.bot.on_approve.call_args.args[0]

    @property
    def skip(self):
        return self.bot.on_skip.call_args.args[0]

    @property
    def submitted(self):
        return self.bot.on_submitted.call_args.args[0]

    @property
    def status(self):
        return self.bot.on_status.call_args.args[0]

    @property
    def load_card(self):
        return self.bot.set_card_loader.call_args.args[0]


@pytest.mark.asyncio
async def test_approve_portal_job_asks_for_submitted(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.approve(h.job_id)
    h.bot.send_approved_portal.assert_awaited_once()
    assert status_of(db, h.job.url) == STATUS_APPROVED
    h.tracker.append_application.assert_not_called()  # not submitted yet


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.apply_by_email")
async def test_approve_email_job_sends_cv_records_and_notifies(mock_apply, mock_notify, db, tmp_path):
    h = Harness(db, tmp_path, email="hr@kla.com")
    await h.approve(h.job_id)

    args = mock_apply.call_args.args
    assert args[2] == "hr@kla.com"
    assert "[jobagent]" not in args[3]          # subject
    assert "Subject:" not in args[4]            # body
    assert args[5] == Path(h.pdf)               # this job's own CV
    assert h.tracker.append_application.call_args.args[0].channel == "email"
    assert status_of(db, h.job.url) == STATUS_APPLIED
    h.bot.send_message.assert_awaited()         # Telegram
    mock_notify.assert_called_once()            # email


@pytest.mark.asyncio
@patch("run.apply_by_email")
async def test_double_tap_approve_sends_one_application(mock_apply, db, tmp_path):
    with patch("jobagent.notify.notify_event", return_value="<m>"):
        h = Harness(db, tmp_path, email="hr@kla.com")
        await h.approve(h.job_id)
        await h.approve(h.job_id)
    mock_apply.assert_called_once()


@pytest.mark.asyncio
@patch("run.apply_by_email", side_effect=OSError("smtp down"))
async def test_failed_application_email_falls_back_to_manual(mock_apply, db, tmp_path):
    h = Harness(db, tmp_path, email="hr@kla.com")
    await h.approve(h.job_id)
    h.bot.send_approved_portal.assert_awaited_once()
    h.tracker.append_application.assert_not_called()  # the sheet never lists an unsent job


@pytest.mark.asyncio
@patch("run.apply_by_email")
async def test_missing_cv_never_emails_an_empty_application(mock_apply, db, tmp_path):
    h = Harness(db, tmp_path, email="hr@kla.com", with_pdf=False)
    await h.approve(h.job_id)
    mock_apply.assert_not_called()
    h.bot.send_approved_portal.assert_awaited_once()


@pytest.mark.asyncio
@patch("run.apply_by_email")
async def test_dry_run_approve_writes_nothing(mock_apply, db, tmp_path):
    h = Harness(db, tmp_path, email="hr@kla.com", dry_run=True)
    await h.approve(h.job_id)
    mock_apply.assert_not_called()
    assert status_of(db, h.job.url) == STATUS_SENT


@pytest.mark.asyncio
@patch("run.apply_by_email")
async def test_approve_rechecks_sheet_before_sending(mock_apply, db, tmp_path):
    """A row added since the card went out must stop the application."""
    h = Harness(db, tmp_path, email="hr@kla.com")
    h.tracker.is_duplicate.return_value = True
    await h.approve(h.job_id)
    mock_apply.assert_not_called()
    h.bot.send_approved_portal.assert_not_awaited()
    assert h.tracker.is_duplicate.call_args.kwargs == {"fresh": True}
    assert "Already in the sheet" in h.bot.send_message.await_args.args[0]
    assert status_of(db, h.job.url) == STATUS_SKIPPED


@pytest.mark.asyncio
async def test_submitted_adds_no_second_row(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.approve(h.job_id)
    h.tracker.is_duplicate.return_value = True
    await h.submitted(h.job_id)
    h.tracker.append_application.assert_not_called()
    assert status_of(db, h.job.url) == STATUS_APPLIED


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_sheet_row_gets_the_job_number(mock_notify, db, tmp_path):
    h = Harness(db, tmp_path, url="https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Yavne/Dev_2638486-2")
    await h.approve(h.job_id)
    await h.submitted(h.job_id)
    assert h.tracker.append_application.call_args.args[0].job_number == "2638486-2"
    assert "Job number: 2638486-2" in mock_notify.call_args.args[5]


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_scoring_dedupe_checks_the_job_number(mock_fetch, llm, db):
    job = make_job(url="https://www.linkedin.com/jobs/view/4485055993/")
    mock_fetch.return_value = [job]
    tracker = make_tracker()
    await run_fetch_cycle(make_settings(), db, tracker, make_bot())
    assert tracker.is_duplicate.call_args.args == (job.company, job.title, "4485055993")


@pytest.mark.asyncio
async def test_skip_updates_status(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.skip(h.job_id)
    assert status_of(db, h.job.url) == STATUS_SKIPPED


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_submitted_writes_sheet_and_notifies_both(mock_notify, db, tmp_path):
    h = Harness(db, tmp_path)
    await h.approve(h.job_id)
    await h.submitted(h.job_id)
    app = h.tracker.append_application.call_args.args[0]
    assert app.channel == "portal"
    assert app.company == "KLA"
    assert "R&amp;D" in h.bot.send_message.await_args.args[0]  # escaped for Telegram
    mock_notify.assert_called_once()
    assert status_of(db, h.job.url) == STATUS_APPLIED


@pytest.mark.asyncio
async def test_submitted_without_approve_is_ignored(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.submitted(h.job_id)
    h.tracker.append_application.assert_not_called()


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_sheet_failure_is_reported_not_lost(mock_notify, db, tmp_path):
    h = Harness(db, tmp_path)
    h.tracker.append_application.side_effect = RuntimeError("sheets down")
    await h.approve(h.job_id)
    await h.submitted(h.job_id)
    assert "SHEET WRITE FAILED" in h.bot.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_buttons_work_after_restart(db, tmp_path):
    """A new process has empty memory; the card must come back from SQLite."""
    h = Harness(db, tmp_path)
    fresh_bot = make_bot()
    _register_handlers(fresh_bot, h.settings, db, h.tracker, h.notifier)
    card = fresh_bot.set_card_loader.call_args.args[0](h.job_id)
    assert card.company == "KLA"
    assert card.pdf_path == str(h.pdf)
    await fresh_bot.on_approve.call_args.args[0](h.job_id)
    fresh_bot.send_approved_portal.assert_awaited_once()


def test_card_loader_handles_unknown_job(db, tmp_path):
    h = Harness(db, tmp_path)
    assert h.load_card(99999) is None


# ---------------------------------------------------------------------------
# Recruiter replies
# ---------------------------------------------------------------------------

def _sheet_row(status=STATUS_SUBMITTED):
    return ApplicationRow(row_num=5, date="01/11/2026", company="KLA", role="SWE", status=status,
                          last_updated="", domain="", location_source="", match_pct="",
                          cv_variant="", channel="", url="https://x/1", notes="")


def _reply():
    return MagicMock(company="KLA", role="SWE", sender="hr@kla.com", snippet="Sadly <no>",
                     proposed_status=STATUS_REJECTED, message_id="<1@x>")


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_reply_proposes_with_buttons_on_both_channels(mock_scan, mock_notify, db):
    mock_scan.return_value = [_reply()]
    tracker, bot, settings = make_tracker(), make_bot(), make_settings()
    tracker.read_all.return_value = [_sheet_row()]
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, bot))

    bot.send_status_proposal.assert_awaited_once()
    assert "&lt;no&gt;" in bot.send_status_proposal.await_args.args[1]
    mock_notify.assert_called_once()
    tracker.update_status.assert_not_called()  # asks first
    assert mock_scan.call_args.args[1] == [("KLA", "SWE")]


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_seen_replies_are_passed_to_the_scan(mock_scan, mock_notify, db):
    mock_scan.return_value = [_reply()]
    tracker, settings = make_tracker(), make_settings()
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, make_bot()))
    mock_scan.return_value = []
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, make_bot()))
    assert mock_scan.call_args.args[2] == {"<1@x>"}


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_confirm_updates_sheet_once_and_notifies(mock_notify, db, tmp_path):
    h = Harness(db, tmp_path)
    h.tracker.find_row.return_value = 5
    h.tracker.read_all.return_value = [_sheet_row()]
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REJECTED)
    await h.status(pid, STATUS_REJECTED)
    await h.status(pid, STATUS_REJECTED)
    h.tracker.update_status.assert_called_once_with(5, STATUS_REJECTED)
    mock_notify.assert_called_once()


@pytest.mark.asyncio
async def test_ignore_leaves_sheet_alone(db, tmp_path):
    h = Harness(db, tmp_path)
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REJECTED)
    await h.status(pid, None)
    h.tracker.update_status.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_with_no_sheet_row_tells_daniel(db, tmp_path):
    h = Harness(db, tmp_path)
    h.tracker.find_row.return_value = None
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REJECTED)
    await h.status(pid, STATUS_REJECTED)
    assert "No sheet row" in h.bot.send_message.await_args.args[0]
    h.tracker.update_status.assert_not_called()



# ---------------------------------------------------------------------------
# Interview prep sheet
# ---------------------------------------------------------------------------

def _applied(db, tmp_path, **kw):
    """A Harness whose job has gone through Approve + Submitted."""
    h = Harness(db, tmp_path, **kw)
    db.update_job(h.job.url, status=STATUS_APPLIED)
    return h


def _fake_prep(tmp_path):
    pdf = tmp_path / "Interview_prep_KLA.pdf"
    pdf.write_bytes(b"%PDF")
    return MagicMock(return_value=pdf), pdf


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_submitted_starts_the_prep_sheet(mock_notify, start_prep, db, tmp_path):
    h = Harness(db, tmp_path)
    await h.approve(h.job_id)
    start_prep.assert_not_called()  # Approve alone isn't an application
    await h.submitted(h.job_id)
    assert start_prep.call_args.args[3] == h.job_id


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_prep_sheet_goes_to_both_channels(mock_notify, db, tmp_path):
    h = _applied(db, tmp_path)
    fake, pdf = _fake_prep(tmp_path)
    with patch("run.write_prep", fake):
        assert await _send_prep(h.settings, db, h.notifier, h.job_id) is True
    h.bot.send_document.assert_awaited_once()
    assert h.bot.send_document.await_args.args[0] == pdf
    assert mock_notify.call_args.kwargs["attachments"] == [pdf]
    assert db.prep_path_for(h.job.url) == str(pdf)


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_prep_sheet_is_written_once(mock_notify, db, tmp_path):
    h = _applied(db, tmp_path)
    fake, _ = _fake_prep(tmp_path)
    with patch("run.write_prep", fake):
        await _send_prep(h.settings, db, h.notifier, h.job_id)
        await run_prep_retries(h.settings, db, h.notifier)
    fake.assert_called_once()


@pytest.mark.asyncio
async def test_failed_prep_retries_then_gives_up(db, tmp_path):
    h = _applied(db, tmp_path)
    fake = MagicMock(side_effect=RuntimeError("api down"))
    with patch("run.write_prep", fake):
        for _ in range(run.PREP_MAX_ATTEMPTS + 2):
            await run_prep_retries(h.settings, db, h.notifier)
    assert fake.call_count == run.PREP_MAX_ATTEMPTS
    assert "giving up" in h.bot.send_message.await_args.args[0]
    assert db.prep_path_for(h.job.url) is None


@pytest.mark.asyncio
async def test_no_prep_sheet_in_keyword_mode(db, tmp_path):
    h = _applied(db, tmp_path)
    h.settings.anthropic_api_key = ""
    fake, _ = _fake_prep(tmp_path)
    with patch("run.write_prep", fake):
        assert await _send_prep(h.settings, db, h.notifier, h.job_id) is False
    fake.assert_not_called()


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_start_prep_runs_in_the_background(mock_notify, db, tmp_path):
    h = _applied(db, tmp_path)
    fake, pdf = _fake_prep(tmp_path)
    with patch("run.write_prep", fake):
        real_start_prep(h.settings, db, h.notifier, h.job_id)
        assert db.prep_path_for(h.job.url) is None  # returned before the work was done
        await asyncio.gather(*run._background)
    assert db.prep_path_for(h.job.url) == str(pdf)


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_reply_status_reattaches_the_prep_sheet(mock_notify, db, tmp_path):
    h = _applied(db, tmp_path)
    _, pdf = _fake_prep(tmp_path)
    db.update_job(h.job.url, prep_path=str(pdf))
    h.tracker.find_row.return_value = 5
    h.tracker.read_all.return_value = [dataclasses.replace(_sheet_row(), url=h.job.url)]
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REPLIED)
    await h.status(pid, STATUS_REPLIED)
    assert h.bot.send_document.await_args.args[0] == pdf
    assert mock_notify.call_args.kwargs["attachments"] == [pdf]


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_rejection_status_has_no_attachment(mock_notify, db, tmp_path):
    h = _applied(db, tmp_path)
    _, pdf = _fake_prep(tmp_path)
    db.update_job(h.job.url, prep_path=str(pdf))
    h.tracker.find_row.return_value = 5
    h.tracker.read_all.return_value = [dataclasses.replace(_sheet_row(), url=h.job.url)]
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REJECTED)
    await h.status(pid, STATUS_REJECTED)
    h.bot.send_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# Follow-up replies on answered applications
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_reply_scan_watches_answered_rows_not_rejected(mock_scan, mock_notify, db):
    mock_scan.return_value = []
    tracker, settings = make_tracker(), make_settings()
    tracker.read_all.return_value = [
        dataclasses.replace(_sheet_row(), company="A", role="r1"),
        dataclasses.replace(_sheet_row(STATUS_REPLIED), company="B", role="r2"),
        dataclasses.replace(_sheet_row(STATUS_REJECTED), company="C", role="r3"),
    ]
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, make_bot()))
    assert mock_scan.call_args.args[1] == [("A", "r1"), ("B", "r2")]


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_rejection_after_interview_is_proposed(mock_scan, mock_notify, db):
    mock_scan.return_value = [_reply()]  # proposes נדחה
    tracker, bot, settings = make_tracker(), make_bot(), make_settings()
    tracker.read_all.return_value = [_sheet_row(STATUS_REPLIED)]
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, bot))
    bot.send_status_proposal.assert_awaited_once()


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_same_status_reply_informs_without_asking(mock_scan, mock_notify, db):
    reply = _reply()
    reply.proposed_status = STATUS_REPLIED  # e.g. a second-round invite
    mock_scan.return_value = [reply]
    tracker, bot, settings = make_tracker(), make_bot(), make_settings()
    tracker.read_all.return_value = [_sheet_row(STATUS_REPLIED)]
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, bot))
    bot.send_status_proposal.assert_not_awaited()
    assert "Another email" in bot.send_message.await_args.args[0]
    mock_notify.assert_called_once()                     # email too
    assert "<1@x>" in db.seen_reply_ids()                # not reported again
    tracker.update_status.assert_not_called()


# ---------------------------------------------------------------------------
# Stages, source health, housekeeping (2026-11-11)
# ---------------------------------------------------------------------------

from jobagent.statuses import STATUS_INTERVIEW  # noqa: E402


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
async def test_daniel_can_pick_a_different_stage_than_the_guess(mock_notify, db, tmp_path):
    h = Harness(db, tmp_path)
    h.tracker.find_row.return_value = 5
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REPLIED)
    await h.status(pid, STATUS_INTERVIEW)
    h.tracker.update_status.assert_called_once_with(5, STATUS_INTERVIEW)


@pytest.mark.asyncio
async def test_unknown_stage_is_never_written(db, tmp_path):
    h = Harness(db, tmp_path)
    pid = db.add_reply_proposal("<1@x>", "KLA", "SWE", STATUS_REPLIED)
    await h.status(pid, "True")
    h.tracker.update_status.assert_not_called()


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m>")
@patch("run.scan_for_replies")
async def test_proposal_buttons_know_the_guess_and_the_current_stage(mock_scan, mock_notify, db):
    reply = _reply()
    reply.proposed_status, reply.subject = STATUS_INTERVIEW, "Interview"
    mock_scan.return_value = [reply]
    tracker, bot, settings = make_tracker(), make_bot(), make_settings()
    tracker.read_all.return_value = [_sheet_row(STATUS_REPLIED)]
    await run_reply_scan(settings, db, tracker, Notifier(settings, db, bot))
    args = bot.send_status_proposal.await_args.args
    assert args[2:] == (STATUS_INTERVIEW, STATUS_REPLIED)
    assert "Subject: Interview" in args[1]


def test_health_flags_sources_that_returned_nothing():
    run._record_health([make_job(url=f"https://x/{i}", source="apple") for i in range(3)])
    tg, mail = run._health_lines()
    assert "⚠ No jobs from:" in tg and "Meta" in tg and "Apple" not in tg.split("No jobs from:")[1]
    assert "LinkedIn alerts" in tg  # one arrives daily, so zero means something broke
    assert "Apple: 3" in mail


def test_prune_keeps_applied_jobs_files(db, tmp_path, monkeypatch):
    monkeypatch.setattr(run, "OUT_DIR", tmp_path)
    old = "2026-01-01T00:00:00+00:00"
    for i, status in ((1, STATUS_SKIPPED), (2, STATUS_APPLIED)):
        job = make_job(url=f"https://x/{i}", title=f"Role {i}")
        db.insert_if_new(job)
        job_id = db.id_for(job.url)
        (tmp_path / str(job_id)).mkdir()
        db.update_job(job.url, status=status, sent_at=old, pdf_path=str(tmp_path / str(job_id) / "cv.pdf"))
    assert run.prune_outputs(db) == 1
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == [str(db.id_for("https://x/2"))]


def test_fetch_is_overdue_only_when_a_slot_was_missed(db):
    assert run.fetch_overdue(db)  # very first start
    now = datetime.now(timezone.utc)
    db.set_meta("last_fetch_at", (now - timedelta(hours=11)).isoformat())
    assert not run.fetch_overdue(db, now)
    assert run.fetch_overdue(db, now + timedelta(hours=3))


# ---------------------------------------------------------------------------
# Scored once, bumped a few times (2026-11-11)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_one_job_from_two_sources_is_scored_once(mock_fetch, llm, db):
    mock_fetch.return_value = [
        make_job(company="Check Point Software Technologies Ltd.", url="https://linkedin.com/jobs/view/1"),
        make_job(company="Check Point", url="https://jobs.smartrecruiters.com/cp/2"),
    ]
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    assert llm.call_count == 1


NOON = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)  # 12:00 in Israel


def _waiting_card(db, *, hours_before_noon, bumps=0, message_id="101", url="https://x/1"):
    job = make_job(url=url, title=f"Engineer {url[-1]}")
    db.insert_if_new(job)
    db.update_job(job.url, status=STATUS_SENT, score=80, variant="systems", scored_by="llm",
                  score_json=json.dumps(dataclasses.asdict(make_score())),
                  sent_at=(NOON - timedelta(hours=hours_before_noon)).isoformat(),
                  card_message_id=message_id)
    for _ in range(bumps):
        db.note_bump(job.url, NOON - timedelta(hours=hours_before_noon))
    return job


@pytest.mark.asyncio
async def test_bump_after_four_hours_as_a_reply_to_the_card(db):
    _waiting_card(db, hours_before_noon=3, url="https://x/1")
    _waiting_card(db, hours_before_noon=5, url="https://x/2", message_id="202")
    bot = make_bot()
    assert await run.run_bumps(make_settings(), db, make_tracker(), bot, now=NOON) == 1
    card, text, reply_to = bot.send_bump.await_args.args
    assert reply_to == 202 and card.job_id == db.id_for("https://x/2")
    assert "Nvidia" in text and "Bump 1/" in text


@pytest.mark.asyncio
async def test_bump_stops_after_the_last_one(db):
    _waiting_card(db, hours_before_noon=30, bumps=run.MAX_BUMPS)
    bot = make_bot()
    assert await run.run_bumps(make_settings(), db, make_tracker(), bot, now=NOON) == 0


@pytest.mark.asyncio
async def test_no_bumps_at_night(db):
    _waiting_card(db, hours_before_noon=8)
    night = datetime(2026, 11, 11, 21, 30, tzinfo=timezone.utc)  # 23:30 in Israel
    assert await run.run_bumps(make_settings(), db, make_tracker(), make_bot(), now=night) == 0


@pytest.mark.asyncio
async def test_cards_without_a_message_id_are_never_bumped(db):
    """Cards from before bumps existed (and test cards) have no message to reply to."""
    _waiting_card(db, hours_before_noon=8, message_id=None)
    assert await run.run_bumps(make_settings(), db, make_tracker(), make_bot(), now=NOON) == 0


@pytest.mark.asyncio
async def test_bump_closes_a_job_already_in_the_sheet(db):
    job = _waiting_card(db, hours_before_noon=8)
    bot = make_bot()
    assert await run.run_bumps(make_settings(), db, make_tracker(duplicate=True), bot, now=NOON) == 0
    assert status_of(db, job.url) == STATUS_SKIPPED
    bot.send_bump.assert_not_awaited()


@pytest.mark.asyncio
async def test_bumps_cost_no_api_calls_and_change_their_joke(llm, db):
    _waiting_card(db, hours_before_noon=12)
    bot = make_bot()
    await run.run_bumps(make_settings(), db, make_tracker(), bot, now=NOON)
    await run.run_bumps(make_settings(), db, make_tracker(), bot, now=NOON + timedelta(hours=5))
    assert bot.send_bump.await_count == 2 and llm.call_count == 0
    first, second = (c.args[1] for c in bot.send_bump.await_args_list)
    assert first.splitlines()[1] != second.splitlines()[1]


@pytest.mark.asyncio
@patch("run._fetch_all")
async def test_card_remembers_its_telegram_message(mock_fetch, llm, db):
    mock_fetch.return_value = [make_job()]
    await run_fetch_cycle(make_settings(), db, make_tracker(), make_bot())
    assert db.get_by_id(db.id_for(make_job().url))["card_message_id"] == "101"


# ---------------------------------------------------------------------------
# Heartbeat, charger alerts, /status (2026-11-11)
# ---------------------------------------------------------------------------

from jobagent import health  # noqa: E402


@pytest.fixture
def power(tmp_path, monkeypatch):
    """A fake /sys/class/power_supply; set(ac, pct) changes it."""
    (tmp_path / "AC").mkdir(); (tmp_path / "BAT0").mkdir()
    monkeypatch.setattr(health, "POWER", tmp_path)

    def set_(ac: bool, pct: int):
        (tmp_path / "AC" / "online").write_text("1" if ac else "0")
        (tmp_path / "BAT0" / "capacity").write_text(str(pct))
    set_(True, 90)
    return set_


def _notifier_and_bot(db):
    bot = make_bot()
    return Notifier(make_settings(), db, bot), bot


@pytest.mark.asyncio
async def test_back_online_message_and_catch_up_after_sleep(db, power):
    notifier, bot = _notifier_and_bot(db)
    start = MagicMock()
    t0 = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
    db.set_meta("last_fetch_at", (t0 - timedelta(hours=10)).isoformat())
    await run.run_heartbeat(make_settings(), db, notifier, start, now=t0)
    start.assert_not_called()                                   # fetched 10h ago: fine
    await run.run_heartbeat(make_settings(), db, notifier, start, now=t0 + timedelta(hours=7))
    text = bot.send_message.await_args.args[0]
    assert "Back online after 7h" in text and "Catching up" in text and "charger" in text
    start.assert_called_once()


@pytest.mark.asyncio
async def test_no_message_when_nothing_happened(db, power):
    notifier, bot = _notifier_and_bot(db)
    now = datetime.now(timezone.utc)
    db.set_meta("last_fetch_at", now.isoformat())
    for minutes in (0, 10, 20):
        await run.run_heartbeat(make_settings(), db, notifier, MagicMock(), now=now + timedelta(minutes=minutes))
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_charger_changes_and_low_battery_are_reported_once(db, power):
    notifier, bot = _notifier_and_bot(db)
    now = datetime.now(timezone.utc)
    db.set_meta("last_fetch_at", now.isoformat())
    beat = lambda m: run.run_heartbeat(make_settings(), db, notifier, MagicMock(), now=now + timedelta(minutes=m))
    await beat(0)
    power(False, 40); await beat(10); await beat(20)
    power(False, 15); await beat(30); await beat(40)
    power(True, 16); await beat(50)
    texts = [c.args[0] for c in bot.send_message.await_args_list]
    assert [t.split()[0] for t in texts] == ["🔋", "🪫", "🔌"]


def test_status_reply_says_it_all(db, power, tmp_path, monkeypatch):
    monkeypatch.setattr(run, "DATA_DIR", tmp_path)  # data/ is gitignored: absent in a fresh clone
    settings = make_settings()
    settings.fetch_times, settings.daily_card_cap = "08:00,20:00", 10
    db.set_meta("last_fetch_at", "2026-11-11T09:34:00+00:00")
    db.set_meta("last_fetch_summary", "(2882 jobs, 510 new)")
    text = run.status_text(settings, db, now=datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc))
    assert "Agent running" in text and "On the charger, battery 90%" in text
    assert "11/11 11:34 (2882 jobs, 510 new)" in text and "Next fetch: 20:00 today" in text
    assert "Cards today: 0/10" in text and "GB free" in text


def test_power_line_without_a_battery(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "POWER", tmp_path)  # e.g. a server: nothing to read
    assert health.power_line() == "Power: unknown"


def test_duration_reads_naturally():
    assert health.duration(timedelta(minutes=7)) == "7m"
    assert health.duration(timedelta(hours=3, minutes=12)) == "3h 12m"
    assert health.duration(timedelta(days=2, hours=3)) == "2d 3h"
