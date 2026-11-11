"""Read LinkedIn job-alert emails from Gmail via IMAP (App Password)."""
from __future__ import annotations

import email
import email.header
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
LINKEDIN_SENDER = "jobalerts-noreply@linkedin.com"
# Daniel has hundreds of old unread alerts. If the Gmail filter is applied to them,
# this keeps the agent on fresh jobs rather than years of closed postings.
MAX_ALERT_AGE_DAYS = 7


@dataclass
class LinkedInJob:
    title: str
    company: str
    location: str
    url: str


@dataclass
class AlertEmail:
    uid: bytes
    subject: str
    jobs: list[LinkedInJob] = field(default_factory=list)


class GmailImap:
    def __init__(self, address: str, app_password: str, alerts_label: str = "job-alerts"):
        self._address = address
        self._password = app_password
        self._label = alerts_label
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "GmailImap":
        self._conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        self._conn.login(self._address, self._password)
        return self

    def __exit__(self, *_) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def recent_alerts(self) -> Iterator[AlertEmail]:
        """Every alert of the last MAX_ALERT_AGE_DAYS, read or not, without changing the mailbox.

        Not just unread ones: Daniel opens the 07:22 alert on his phone before the
        fetch, and an unread-only search skipped every alert for good (2026-11-11).
        Re-reading an alert costs nothing: its jobs are already in the DB, so they
        are neither fetched nor scored again.
        """
        status, _ = self._conn.select(f'"{self._label}"', readonly=True)
        if status != "OK":
            # Try without quotes (some Gmail labels work either way)
            status, _ = self._conn.select(self._label, readonly=True)
        if status != "OK":
            log.warning("gmail_imap: label %r not found — create it and add a filter for jobalerts-noreply@linkedin.com", self._label)
            return
        since = (date.today() - timedelta(days=MAX_ALERT_AGE_DAYS)).strftime("%d-%b-%Y")
        _, data = self._conn.search(None, f'SINCE {since} FROM "{LINKEDIN_SENDER}"')
        for uid in data[0].split():
            # PEEK, and the read-only select, keep the alert's read/unread state as Daniel left it.
            _, msg_data = self._conn.fetch(uid, "(BODY.PEEK[])")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            subject = _decode_header(msg.get("Subject", ""))
            yield AlertEmail(uid=uid, subject=subject, jobs=list(_parse_jobs(msg)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def _get_html_body(msg: email.message.Message) -> str | None:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
    elif msg.get_content_type() == "text/html":
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return None


_JOB_LINK_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")


def _normalize_url(href: str) -> str:
    m = _JOB_LINK_RE.search(href)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return href


def _parse_jobs(msg: email.message.Message) -> Iterator[LinkedInJob]:
    html = _get_html_body(msg)
    if not html:
        return
    yield from _parse_html(html)


def _parse_html(html: str) -> Iterator[LinkedInJob]:
    """Yield one job per job id.

    Each job has several links with the same id: an empty logo link, a wrapper
    around all the text (title + company + "Easy Apply"), and the title alone.
    The shortest link with text is the title.
    """
    soup = BeautifulSoup(html, "html.parser")
    by_url: dict[str, list] = {}
    for a in soup.find_all("a", href=_JOB_LINK_RE):
        if a.get_text(strip=True):
            by_url.setdefault(_normalize_url(a["href"]), []).append(a)
    for url, links in by_url.items():
        title_link = min(links, key=lambda a: len(a.get_text(strip=True)))
        company, location = _company_location_near(title_link)
        yield LinkedInJob(title=title_link.get_text(strip=True), company=company,
                          location=location, url=url)


_SEPARATOR_RE = re.compile(r"\s*[·•|]\s*")


def _company_location_near(title_link) -> tuple[str, str]:
    """Find the nearest "Company · Location" line, walking up from the title."""
    node = title_link.parent
    for _ in range(6):
        if node is None:
            break
        for line in node.find_all("p"):
            parts = _SEPARATOR_RE.split(line.get_text(" ", strip=True), maxsplit=1)
            if len(parts) == 2 and parts[0]:
                return parts[0], parts[1]
        node = node.parent
    return "", ""
