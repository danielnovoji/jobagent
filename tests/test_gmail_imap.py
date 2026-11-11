"""Tests for jobagent/gmail_imap.py — mocked imaplib, no real network."""
import email
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobagent.gmail_imap import (
    AlertEmail,
    GmailImap,
    LinkedInJob,
    _normalize_url,
    _parse_html,
    _parse_jobs,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> email.message.Message:
    raw = (FIXTURES / name).read_bytes()
    return email.message_from_bytes(raw)


def make_imap_conn(raw_emails: list[bytes]) -> MagicMock:
    """Return a mock imaplib connection that serves the given raw RFC822 emails."""
    conn = MagicMock()
    uids = b" ".join(str(i + 1).encode() for i in range(len(raw_emails)))
    conn.search.return_value = ("OK", [uids])
    fetch_map = {str(i + 1).encode(): raw for i, raw in enumerate(raw_emails)}

    def fetch_side(uid, fmt):
        raw = fetch_map.get(uid, b"")
        return ("OK", [(b"(RFC822)", raw), b")"])

    conn.fetch.side_effect = fetch_side
    conn.select.return_value = ("OK", [b"1"])
    conn.login.return_value = ("OK", [b"OK"])
    return conn


# ---------------------------------------------------------------------------
# URL normalisation (pure)
# ---------------------------------------------------------------------------

def test_normalize_tracking_url():
    url = "https://www.linkedin.com/comm/jobs/view/3456789012/?alertAction=view&savedSearchId=1"
    assert _normalize_url(url) == "https://www.linkedin.com/jobs/view/3456789012/"


def test_normalize_already_canonical():
    url = "https://www.linkedin.com/jobs/view/9876543210/"
    assert _normalize_url(url) == url


def test_normalize_unknown_url_passthrough():
    url = "https://example.com/not-a-job"
    assert _normalize_url(url) == url


# ---------------------------------------------------------------------------
# HTML parsing via fixture
# ---------------------------------------------------------------------------

def test_parse_fixture_returns_three_jobs():
    msg = load_fixture("linkedin_alert.eml")
    jobs = list(_parse_jobs(msg))
    assert len(jobs) == 3


def test_parse_fixture_job_titles():
    msg = load_fixture("linkedin_alert.eml")
    jobs = list(_parse_jobs(msg))
    titles = [j.title for j in jobs]
    assert "Software Engineer - Systems" in titles
    assert "C++ Developer" in titles
    assert "Embedded Software Engineer" in titles


def test_parse_fixture_canonical_urls():
    msg = load_fixture("linkedin_alert.eml")
    urls = {j.url for j in _parse_jobs(msg)}
    assert "https://www.linkedin.com/jobs/view/3456789012/" in urls
    assert "https://www.linkedin.com/jobs/view/9876543210/" in urls
    assert "https://www.linkedin.com/jobs/view/1122334455/" in urls


def test_parse_fixture_company_and_location():
    msg = load_fixture("linkedin_alert.eml")
    jobs = {j.title: j for j in _parse_jobs(msg)}
    nvidia = jobs["Software Engineer - Systems"]
    assert nvidia.company == "Nvidia"
    assert "Tel Aviv" in nvidia.location


def test_no_duplicate_jobs():
    html = """
    <html><body>
      <a href="https://www.linkedin.com/comm/jobs/view/111/?x=1">Dev</a>
      <p>Co &middot; City</p>
      <a href="https://www.linkedin.com/comm/jobs/view/111/?x=2">Dev</a>
      <p>Co &middot; City</p>
    </body></html>
    """
    assert len(list(_parse_html(html))) == 1


def test_parse_empty_html_returns_nothing():
    assert list(_parse_html("<html><body><p>No jobs here</p></body></html>")) == []


# ---------------------------------------------------------------------------
# GmailImap — imaplib mocked
# ---------------------------------------------------------------------------

def test_recent_alerts_yields_emails():
    raw = (FIXTURES / "linkedin_alert.eml").read_bytes()
    conn = make_imap_conn([raw, raw])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts") as imap:
            alerts = list(imap.recent_alerts())
    assert len(alerts) == 2
    assert all(isinstance(a, AlertEmail) for a in alerts)
    assert all(len(a.jobs) == 3 for a in alerts)


def test_recent_alerts_label_in_search():
    raw = (FIXTURES / "linkedin_alert.eml").read_bytes()
    conn = make_imap_conn([raw])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "my-label") as imap:
            list(imap.recent_alerts())
    select_arg = conn.select.call_args[0][0]
    assert "my-label" in select_arg


def test_alert_uid_is_bytes():
    raw = (FIXTURES / "linkedin_alert.eml").read_bytes()
    conn = make_imap_conn([raw])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts") as imap:
            alerts = list(imap.recent_alerts())
    assert isinstance(alerts[0].uid, bytes)


def test_context_manager_login_logout():
    conn = make_imap_conn([])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts"):
            conn.login.assert_called_once_with("me@gmail.com", "pass")
    conn.logout.assert_called_once()


# ---------------------------------------------------------------------------
# Real alert layout: three links per job with the same id
# ---------------------------------------------------------------------------

def _real_layout_jobs():
    return list(_parse_html((FIXTURES / "linkedin_alert_real.html").read_text(encoding="utf-8")))


def test_real_layout_one_job_per_id():
    assert len(_real_layout_jobs()) == 2


def test_real_layout_title_is_only_the_title():
    """The wrapper link holds title + company + "Easy Apply" glued together."""
    assert [j.title for j in _real_layout_jobs()] == ["Full Stack Engineer", "Junior Backend Developer"]


def test_real_layout_splits_company_and_location():
    first, second = _real_layout_jobs()
    assert (first.company, first.location) == ("TRAVELFACTORY Lab", "Raanana (Hybrid)")
    assert (second.company, second.location) == ("Milgam Ltd.", "Center District, Israel (On-site)")


def test_real_layout_uses_canonical_urls():
    assert _real_layout_jobs()[0].url == "https://www.linkedin.com/jobs/view/4485055993/"


def test_title_is_never_used_as_company():
    html = '<a href="https://www.linkedin.com/comm/jobs/view/111/">Dev</a><p>no separator</p>'
    job = next(_parse_html(html))
    assert (job.company, job.location) == ("", "")


def test_old_alerts_are_not_fetched():
    """765 unread alerts exist; the search must stay on recent ones."""
    conn = make_imap_conn([])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts") as imap:
            list(imap.recent_alerts())
    assert "SINCE" in conn.search.call_args.args[1]


def test_reading_an_alert_doesnt_mark_it_read():
    conn = make_imap_conn([(FIXTURES / "linkedin_alert.eml").read_bytes()])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts") as imap:
            list(imap.recent_alerts())
    assert "PEEK" in conn.fetch.call_args.args[1]


def test_alerts_daniel_already_read_are_still_processed():
    """He opens the 07:22 alert on his phone before the fetch (bug found 2026-11-11)."""
    conn = make_imap_conn([(FIXTURES / "linkedin_alert.eml").read_bytes()])
    with patch("jobagent.gmail_imap.imaplib.IMAP4_SSL", return_value=conn):
        with GmailImap("me@gmail.com", "pass", "job-alerts") as imap:
            list(imap.recent_alerts())
    assert "UNSEEN" not in conn.search.call_args.args[1]
    assert conn.select.call_args.kwargs == {"readonly": True}
    conn.store.assert_not_called()
