"""Tests for jobagent/gmail_scanner.py — mocked imaplib, no real network."""
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import MagicMock, patch

import pytest

from jobagent.statuses import STATUS_ASSIGNMENT, STATUS_INTERVIEW, STATUS_OFFER, STATUS_SCREENING
from jobagent.gmail_scanner import (
    LINKEDIN_SENDER,
    STATUS_REJECTION,
    STATUS_REPLY,
    _get_body,
    classify,
    scan_for_replies,
)


# ---------------------------------------------------------------------------
# classify (pure function)
# ---------------------------------------------------------------------------

def test_classify_rejection():
    assert classify("We regret to inform you that unfortunately we won't proceed.") == STATUS_REJECTION


def test_classify_rejection_not_moving_forward():
    assert classify("We are not moving forward with your application at this time.") == STATUS_REJECTION


def test_classify_interview_phone_screen():
    assert classify("We'd like to schedule a phone screen with you for next steps.") == STATUS_SCREENING


def test_classify_interview_next_steps():
    assert classify("We'd love to discuss next steps and schedule a call.") == STATUS_REPLY


def test_classify_unknown_returns_none():
    assert classify("Thank you for your interest in our company.") is None


def test_classify_rejection_wins_over_interview():
    text = "Unfortunately not moving forward, though we'd love to interview you someday."
    assert classify(text) == STATUS_REJECTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_settings(address: str = "me@gmail.com", password: str = "pass") -> MagicMock:
    s = MagicMock()
    s.gmail_address = address
    s.gmail_app_password = password
    return s


def _email_bytes(subject: str, body: str, sender: str) -> bytes:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["Message-ID"] = f"<{abs(hash((subject, sender)))}@test>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg.as_bytes()


def make_scanner_conn(*emails) -> MagicMock:
    """emails: (subject, body, sender) tuples. Returns a mock IMAP conn."""
    raws = [_email_bytes(s, b, f) for s, b, f in emails]
    conn = MagicMock()
    uids = b" ".join(str(i + 1).encode() for i in range(len(raws)))
    conn.search.return_value = ("OK", [uids])
    fetch_map = {str(i + 1).encode(): raw for i, raw in enumerate(raws)}

    def fetch_side(uids, fmt):
        if "HEADER.FIELDS" in fmt:  # first pass: headers of every message in one call
            items = []
            for seq in uids.split(b","):
                head = fetch_map[seq].split(b"\n\n", 1)[0] + b"\n\n"
                items += [(seq + b" (BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)] {%d}" % len(head), head), b")"]
            return ("OK", items)
        return ("OK", [(uids + b" (BODY[] {1})", fetch_map.get(uids, b"")), b")"])

    conn.fetch.side_effect = fetch_side
    conn.select.return_value = ("OK", [b"INBOX"])
    conn.login.return_value = ("OK", [b"OK"])
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# scan_for_replies
# ---------------------------------------------------------------------------

def test_scan_finds_rejection_from_known_company():
    conn = make_scanner_conn(
        ("RE: Your application", "Unfortunately we won't be proceeding.", "hr@nvidia.com"),
    )
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        results = scan_for_replies(make_settings(), [("Nvidia", "C++ Engineer")])
    assert len(results) == 1
    assert results[0].company == "Nvidia"
    assert results[0].proposed_status == STATUS_REJECTION


def test_scan_finds_interview_invite():
    conn = make_scanner_conn(
        ("Next steps", "We'd like to schedule a phone screen.", "talent@intel.com"),
    )
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        results = scan_for_replies(make_settings(), [("Intel", "C++ Developer")])
    assert len(results) == 1
    assert results[0].proposed_status == STATUS_SCREENING


def test_scan_skips_linkedin_sender():
    conn = make_scanner_conn(
        ("Job alert", "Unfortunately many jobs.", LINKEDIN_SENDER),
    )
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        results = scan_for_replies(make_settings(), [("Nvidia", "C++ Engineer")])
    assert results == []


def test_scan_empty_applied_returns_empty_without_connecting():
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL") as mock_ssl:
        results = scan_for_replies(make_settings(), [])
    assert results == []
    mock_ssl.assert_not_called()


def test_scan_reply_without_a_stage_proposes_reply_received():
    """Daniel wants to see every reply about the application, stage or not."""
    conn = make_scanner_conn(
        ("RE: Application", "Thank you for reaching out to Nvidia.", "hr@nvidia.com"),
    )
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        results = scan_for_replies(make_settings(), [("Nvidia", "C++ Engineer")])
    assert results[0].proposed_status == STATUS_REPLY


def test_scan_handles_imap_error_gracefully():
    conn = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)
    conn.login.side_effect = Exception("auth error")
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        results = scan_for_replies(make_settings(), [("Nvidia", "C++ Engineer")])
    assert results == []


# ---------------------------------------------------------------------------
# Mailbox safety and dedupe
# ---------------------------------------------------------------------------

def _scan(conn, applied=(("Nvidia", "C++ Engineer"),), seen=frozenset()):
    with patch("jobagent.gmail_scanner.imaplib.IMAP4_SSL", return_value=conn):
        return scan_for_replies(make_settings(), list(applied), seen)


REJECTION = ("RE: Your application", "Unfortunately we won't be proceeding.", "hr@nvidia.com")


def test_scan_never_marks_mail_as_read():
    """A plain RFC822 fetch on a read-write mailbox sets the Seen flag."""
    conn = make_scanner_conn(REJECTION)
    _scan(conn)
    conn.select.assert_called_once_with("INBOX", readonly=True)
    assert "PEEK" in conn.fetch.call_args.args[1]


def test_scan_finds_replies_daniel_already_opened():
    conn = make_scanner_conn(REJECTION)
    _scan(conn)
    assert "UNSEEN" not in conn.search.call_args.args[1]


def test_scan_skips_already_proposed_message():
    first = _scan(make_scanner_conn(REJECTION))
    assert len(first) == 1
    assert _scan(make_scanner_conn(REJECTION), seen={first[0].message_id}) == []


def test_scan_ignores_agents_own_notifications():
    """They mention the company and often the word "interview", and arrive in the inbox."""
    own = ("Re: [jobagent] Nvidia - C++ Engineer", "Proposed status after interview", "me@gmail.com")
    assert _scan(make_scanner_conn(own)) == []


def test_scan_matches_company_as_whole_word():
    newsletter = ("Weekly digest", "Artificial intelligence interview tips", "news@site.com")
    assert _scan(make_scanner_conn(newsletter), applied=[("Intel", "SWE")]) == []


def test_scan_keeps_company_spelling_from_the_sheet():
    conn = make_scanner_conn(("Your application", "Unfortunately no.", "hr@kla.com"))
    assert _scan(conn, applied=[("KLA", "SWE")])[0].company == "KLA"


def test_scan_picks_the_role_the_email_names():
    conn = make_scanner_conn(("Update: Backend Engineer", "Unfortunately no.", "hr@nvidia.com"))
    replies = _scan(conn, applied=[("Nvidia", "C++ Engineer"), ("Nvidia", "Backend Engineer")])
    assert replies[0].role == "Backend Engineer"



# --- stages and sender matching (2026-11-11) ---

@pytest.mark.parametrize("text, stage", [
    ("We are pleased to offer you the position", STATUS_OFFER),
    ("Next step is a short home assignment", STATUS_ASSIGNMENT),
    ("We'd like to invite you to an interview with the team", STATUS_INTERVIEW),
    ("נשמח לזמן אותך לראיון במשרדינו", STATUS_INTERVIEW),
    ("Let's set up a phone interview first", STATUS_SCREENING),
])
def test_classify_stages(text, stage):
    assert classify(text) == stage


def test_classify_ignores_application_acknowledgements():
    """They often say "if selected, we'll contact you for an interview"."""
    assert classify("Thank you for applying! If selected, we'll contact you for an interview.") is None


def test_scan_hiring_platform_sender_names_the_company():
    conn = make_scanner_conn(("Your application to JFrog", "Unfortunately we went with others.",
                              "JFrog <no-reply@us.greenhouse-mail.io>"))
    assert _scan(conn, applied=[("JFrog", "Backend Engineer")])[0].company == "JFrog"


def test_scan_company_only_in_the_body_is_not_a_reply():
    news = ("Weekly jobs", "Nvidia is hiring! Our interview tips for your application", "news@techsite.com")
    assert _scan(make_scanner_conn(news)) == []


def test_scan_company_mail_without_application_context_is_ignored():
    order = ("Your order", "Unfortunately your order is delayed.", "auto-confirm@amazon.com")
    assert _scan(make_scanner_conn(order), applied=[("Amazon", "SDE")]) == []


def test_scan_downloads_bodies_only_for_company_senders():
    conn = make_scanner_conn(REJECTION, ("Hi", "Lunch?", "friend@gmail.com"), ("Sale", "50% off", "shop@store.com"))
    _scan(conn)
    body_fetches = [c for c in conn.fetch.call_args_list if c.args[1] == "(BODY.PEEK[])"]
    assert len(body_fetches) == 1


def test_get_body_reads_html_only_mail():
    msg = MIMEText("<p>Unfortunately &amp; sadly no</p>", "html", "utf-8")
    assert "Unfortunately & sadly no" in _get_body(msg)
