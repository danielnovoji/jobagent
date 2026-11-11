"""Send emails via Gmail SMTP (App Password)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
THREAD_PREFIX = "[jobagent]"


def thread_subject(company: str, role: str) -> str:
    return f"{THREAD_PREFIX} {company} - {role}"


def send_email(
    address: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    *,
    attachments: list[Path] | None = None,
    in_reply_to: str | None = None,
) -> str:
    """Send a plain-text email. Returns the generated Message-ID (used for threading)."""
    msg = MIMEMultipart()
    msg["From"] = address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg_id = make_msgid(domain="jobagent")
    msg["Message-ID"] = msg_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachments:
        for path in attachments:
            with open(path, "rb") as fh:
                part = MIMEApplication(fh.read(), Name=path.name)
            part["Content-Disposition"] = f'attachment; filename="{path.name}"'
            msg.attach(part)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(address, password)
        smtp.sendmail(address, [to], msg.as_bytes())
    return msg_id


def apply_by_email(
    address: str,
    password: str,
    to_addr: str,
    subject: str,
    cover_text: str,
    pdf_path: Path | None,
) -> str:
    """Send an application to a recruiter, CV attached. Returns the Message-ID.

    Takes the subject from the cover note: the [jobagent] prefix is for Daniel's
    own notification threads and must never reach a recruiter.
    """
    return send_email(
        address, password,
        to=to_addr,
        subject=subject,
        body=cover_text,
        attachments=[pdf_path] if pdf_path else None,
    )


def notify_event(
    address: str,
    password: str,
    notify_email: str,
    company: str,
    role: str,
    body: str,
    *,
    thread_id: str | None = None,
    attachments: list[Path] | None = None,
) -> str:
    """Send a status-update notification in the application thread. Returns the Message-ID."""
    return send_email(
        address, password,
        to=notify_email,
        subject=thread_subject(company, role),
        body=body,
        attachments=attachments,
        in_reply_to=thread_id,
    )
