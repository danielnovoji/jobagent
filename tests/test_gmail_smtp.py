"""Tests for jobagent/gmail_smtp.py — mocked smtplib, no real network."""
import email as email_lib
import smtplib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobagent.gmail_smtp import (
    THREAD_PREFIX,
    apply_by_email,
    notify_event,
    send_email,
    thread_subject,
)
from jobagent.models import JobRecord
from jobagent.scorer import ScoreResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_smtp_mock() -> MagicMock:
    smtp = MagicMock()
    smtp.__enter__ = lambda s: s
    smtp.__exit__ = MagicMock(return_value=False)
    return smtp


def get_sent_message(smtp_mock: MagicMock) -> email_lib.message.Message:
    raw_bytes = smtp_mock.sendmail.call_args[0][2]
    return email_lib.message_from_bytes(raw_bytes)


def make_job(company: str = "Nvidia", title: str = "C++ Engineer") -> JobRecord:
    return JobRecord(source="test", company=company, title=title,
                     location="Israel", url="https://example.com/1")


def make_score() -> ScoreResult:
    return ScoreResult(score=80, variant="systems", seniority_ok=True, reason="Good.")


# ---------------------------------------------------------------------------
# thread_subject
# ---------------------------------------------------------------------------

def test_thread_subject_format():
    assert thread_subject("Nvidia", "C++ Engineer") == f"{THREAD_PREFIX} Nvidia - C++ Engineer"


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------

def test_send_email_returns_message_id():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        result = send_email("me@gmail.com", "pass", "to@example.com", "Subj", "Body")
    assert result.startswith("<") and "@jobagent" in result


def test_send_email_sets_from_and_to():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        send_email("me@gmail.com", "pass", "to@example.com", "Subj", "Body")
    msg = get_sent_message(smtp)
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "to@example.com"


def test_send_email_sets_reply_headers():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        send_email("me@gmail.com", "pass", "to@example.com", "Subj", "Body",
                   in_reply_to="<orig@jobagent>")
    msg = get_sent_message(smtp)
    assert msg["In-Reply-To"] == "<orig@jobagent>"
    assert msg["References"] == "<orig@jobagent>"


def test_send_email_no_reply_headers_when_none():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        send_email("me@gmail.com", "pass", "to@example.com", "Subj", "Body")
    msg = get_sent_message(smtp)
    assert msg["In-Reply-To"] is None


def test_send_email_attaches_pdf(tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        send_email("me@gmail.com", "pass", "to@example.com", "Subj", "Body",
                   attachments=[pdf])
    msg = get_sent_message(smtp)
    attachments = [p for p in msg.walk() if p.get_filename()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "cv.pdf"


def test_send_email_logins_with_password():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        send_email("me@gmail.com", "mypass", "to@example.com", "Subj", "Body")
    smtp.login.assert_called_once_with("me@gmail.com", "mypass")


# ---------------------------------------------------------------------------
# apply_by_email
# ---------------------------------------------------------------------------

def test_apply_by_email_uses_cover_subject_not_internal_prefix():
    """Recruiters must never see the [jobagent] prefix."""
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        apply_by_email("me@gmail.com", "pass", "hr@nvidia.com",
                       "Application for C++ Engineer", "Hi", None)
    msg = get_sent_message(smtp)
    assert msg["Subject"] == "Application for C++ Engineer"
    assert THREAD_PREFIX not in msg["Subject"]


def test_apply_by_email_to_recruiter():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        apply_by_email("me@gmail.com", "pass", "hr@nvidia.com", "Subject", "Hi", None)
    msg = get_sent_message(smtp)
    assert msg["To"] == "hr@nvidia.com"


# ---------------------------------------------------------------------------
# notify_event
# ---------------------------------------------------------------------------

def test_notify_event_to_is_notify_email():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        notify_event("me@gmail.com", "pass", "daniel@gmail.com", "Nvidia", "C++ Engineer", "Body")
    msg = get_sent_message(smtp)
    assert msg["To"] == "daniel@gmail.com"


def test_notify_event_with_thread_id_sets_in_reply_to():
    smtp = make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=smtp):
        notify_event("me@gmail.com", "pass", "me@gmail.com", "Nvidia", "C++ Engineer", "Body",
                     thread_id="<prev@jobagent>")
    msg = get_sent_message(smtp)
    assert msg["In-Reply-To"] == "<prev@jobagent>"
