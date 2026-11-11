"""Scan the Gmail inbox for replies about applications (IMAP, App Password), read-only.

Two passes, so the agent doesn't download the whole inbox twice a day: headers
(From, Subject, Message-ID) for every message of the last week, then full bodies
only for messages whose sender is a company Daniel applied to.

A sender is the company when its domain is the company's own (hr@nvidia.com, or a
listed alias such as amat.com), or when it is a hiring platform (Greenhouse,
Workday, Comeet, ...) that names the company in the display name or subject. A
company merely mentioned in a body (a newsletter, an Amazon order) is not a reply.
"""
from __future__ import annotations

import email as email_lib
import html
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header as _raw_decode
from email.utils import parseaddr

from jobagent.models import company_key
from jobagent.statuses import (
    STATUS_ASSIGNMENT, STATUS_INTERVIEW, STATUS_OFFER, STATUS_REJECTED, STATUS_REPLIED,
    STATUS_SCREENING,
)

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
LINKEDIN_SENDER = "jobalerts-noreply@linkedin.com"
OWN_THREAD_PREFIX = "[jobagent]"  # the agent's own notifications land in the inbox too
SCAN_DAYS = 7
SNIPPET_CHARS = 600  # enough of the email to judge it on Telegram without opening Gmail

# Older names, kept for imports from before the stages were added.
STATUS_REJECTION = STATUS_REJECTED
STATUS_REPLY = STATUS_REPLIED

_REJECT_KW = [
    "unfortunately", "not moving forward", "decided to pursue other",
    "regret to inform", "won't be proceeding", "not be moving forward",
    "not selected", "לצערנו", "לא נוכל לקדם",
]
# "We got your application" mails carry no decision and often promise an "interview"
# if selected, so they never propose a stage.
_ACK_KW = [
    "thank you for applying", "thanks for applying", "received your application",
    "application has been received", "application was received", "we will review your",
    "תודה על הגשת", "קיבלנו את קורות החיים", "קיבלנו את מועמדותך", "מועמדותך התקבלה",
]
# Checked in order, so the more specific stage wins ("phone interview" is a screening).
_STAGE_KW = [
    (STATUS_OFFER, ["offer letter", "job offer", "pleased to offer", "happy to offer",
                    "extend an offer", "הצעת עבודה", "הצעת שכר"]),
    (STATUS_ASSIGNMENT, ["home assignment", "home test", "take-home", "take home",
                         "coding challenge", "online assessment", "technical assessment",
                         "codility", "hackerrank", "codesignal", "מטלת בית", "מבחן בית",
                         "מטלה", "מבחן"]),
    (STATUS_SCREENING, ["phone screen", "phone interview", "phone call", "intro call",
                        "introductory call", "screening call", "שיחת היכרות", "שיחה טלפונית",
                        "ראיון טלפוני"]),
    (STATUS_INTERVIEW, ["interview", "meet the team", "onsite", "on-site", "ראיון"]),
]
# A mail from the company without any of these is marketing, not about the application.
_CONTEXT_KW = [
    "application", "applied", "applying", "candidacy", "candidate", "position", "role",
    "next steps", "your cv", "your resume", "מועמד", "משרה", "תפקיד", "קורות חיים",
]
# Hiring platforms send on the company's behalf.
PLATFORM_DOMAINS = (
    "greenhouse.io", "greenhouse-mail.io", "myworkday.com", "workday.com", "smartrecruiters.com",
    "comeet.co", "comeet.com", "lever.co", "ashbyhq.com", "eightfold.ai", "icims.com",
    "successfactors.com", "linkedin.com",
)
# company_key -> mail domains, for companies whose domain isn't their name.
DOMAIN_ALIASES = {
    "applied materials": ("amat.com", "appliedmaterials.com"),
    "cardo": ("cardosystems.com",),
    "unity": ("unity3d.com", "unity.com"),
    "annapurna labs": ("amazon.com", "annapurnalabs.com"),
    "amazon": ("amazon.com", "amazon.jobs"),
    "palo alto networks": ("paloaltonetworks.com", "cyberark.com"),
    "elbit": ("elbitsystems.com",),
    "elta": ("iai.co.il", "elta.co.il"),
    "iai": ("iai.co.il",),
}


@dataclass
class ReplyInfo:
    company: str
    role: str
    sender: str
    snippet: str
    proposed_status: str
    message_id: str = ""
    subject: str = ""


def classify(text: str) -> str | None:
    """The stage an email points to, or None if it has no clear signal."""
    lower = text.lower()
    if any(kw in lower for kw in _REJECT_KW):
        return STATUS_REJECTED
    if any(kw in lower for kw in _ACK_KW):
        return None
    for status, keywords in _STAGE_KW:
        if any(kw in lower for kw in keywords):
            return status
    if any(kw in lower for kw in _CONTEXT_KW):
        return STATUS_REPLIED
    return None


def about_application(text: str, roles: list[str]) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _CONTEXT_KW) or any(r.lower() in lower for r in roles)


def sender_company(companies: list[str], sender: str, subject: str) -> str | None:
    """The applied-to company this sender speaks for, or None."""
    name, addr = parseaddr(sender)
    domain = addr.rpartition("@")[2].lower()
    labels = set(re.split(r"[.-]", domain))
    for company in companies:
        key = company_key(company)
        if key.replace(" ", "") in labels or any(_under(domain, d) for d in DOMAIN_ALIASES.get(key, ())):
            return company
    if any(_under(domain, p) for p in PLATFORM_DOMAINS):
        shown = company_key(f"{_decode_hdr(name)} {subject}")
        for company in companies:
            if _mentions(company_key(company), shown):
                return company
    return None


def scan_for_replies(
    settings, applied: list[tuple[str, str]], seen_ids: set[str] | frozenset = frozenset()
) -> list[ReplyInfo]:
    """Scan INBOX for replies from companies applied to.

    Read-only: the mailbox is opened with readonly=True and everything is fetched
    with BODY.PEEK, because a plain fetch would mark Daniel's mail as read. Reads
    don't filter on UNSEEN either, or a reply opened on his phone would be missed.
    `seen_ids` is what stops the same email being proposed twice.
    """
    if not applied:
        return []
    roles_of: dict[str, list[str]] = {}
    for company, role in applied:
        roles_of.setdefault(company, []).append(role)
    companies = list(roles_of)
    own = (settings.gmail_address or "").lower()

    since = (date.today() - timedelta(days=SCAN_DAYS)).strftime("%d-%b-%Y")
    results: list[ReplyInfo] = []
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as conn:
            conn.login(settings.gmail_address, settings.gmail_app_password)
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, f'(SINCE {since} NOT FROM "{LINKEDIN_SENDER}")')
            seqs = data[0].split()
            if not seqs:
                return []
            _, headers = conn.fetch(b",".join(seqs), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
            for seq, head in _parse_headers(headers):
                sender = head.get("From", "")
                subject = _decode_hdr(head.get("Subject", ""))
                message_id = head.get("Message-ID", "").strip()
                if (message_id in seen_ids or OWN_THREAD_PREFIX in subject
                        or LINKEDIN_SENDER in sender or (own and own in sender.lower())):
                    continue
                company = sender_company(companies, sender, subject)
                if company is None:
                    continue
                _, msg_data = conn.fetch(seq, "(BODY.PEEK[])")
                body = _get_body(email_lib.message_from_bytes(msg_data[0][1]))
                full_text = f"{subject} {body}"
                status = classify(full_text)
                if status is None or not about_application(full_text, roles_of[company]):
                    continue
                results.append(ReplyInfo(
                    company=company,
                    role=_pick_role(roles_of[company], full_text),
                    sender=sender,
                    snippet=re.sub(r"\s+", " ", body).strip()[:SNIPPET_CHARS],
                    proposed_status=status,
                    message_id=message_id or f"no-id:{seq.decode()}:{sender}:{subject}",
                    subject=subject,
                ))
    except Exception as exc:
        log.error("gmail_scanner: %s", exc)
    return results


def _parse_headers(fetched) -> list[tuple[bytes, email_lib.message.Message]]:
    """imaplib returns (b'<seq> (BODY[...] {n}', header bytes) tuples between b')' items."""
    out = []
    for item in fetched or []:
        if isinstance(item, tuple) and len(item) == 2:
            out.append((item[0].split()[0], email_lib.message_from_bytes(item[1])))
    return out


def _under(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


def _mentions(company: str, text: str) -> bool:
    """Whole-word match, so "Intel" doesn't match "intelligence"."""
    return bool(company) and bool(re.search(rf"(?<!\w){re.escape(company)}(?!\w)", text, re.IGNORECASE))


def _pick_role(roles: list[str], text: str) -> str:
    """With two applications at one company, prefer the role the email names."""
    lowered = text.lower()
    return next((r for r in roles if r.lower() in lowered), roles[0])


def _decode_hdr(value: str) -> str:
    out = []
    for part, enc in _raw_decode(value or ""):
        out.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(out)


def _get_body(msg) -> str:
    """The plain-text part, or the HTML part as text: many ATS mails are HTML only."""
    html_text = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) if ctype in ("text/plain", "text/html") else None
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain":
            return text
        html_text = html_text or text
    return html.unescape(re.sub(r"<[^>]+>", " ", html_text)) if html_text else ""
