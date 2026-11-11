"""Deliver every application update on both Telegram and email.

Daniel's rule: an update on an applied job goes to both channels, and if one
fails the other still delivers and the failed one is retried. A send that fails
goes in the SQLite outbox, so a retry survives a restart.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from jobagent.db import JobDb
from jobagent.gmail_smtp import notify_event
from jobagent.telegram_bot import TelegramBot

log = logging.getLogger(__name__)

TELEGRAM = "telegram"
EMAIL = "email"


class Notifier:
    def __init__(self, settings, db: JobDb, bot: TelegramBot):
        self._settings = settings
        self._db = db
        self._bot = bot

    async def event(
        self,
        *,
        url: str,
        company: str,
        role: str,
        tg_text: str,
        email_body: str,
        proposal_id: int | None = None,
        proposed: str = "",
        current: str = "",
        attachment: str | None = None,
    ) -> None:
        """Send one update on both channels. Never raises for a delivery failure.

        `attachment` is a file path sent on both channels. Stored as a path, not
        bytes, so a queued retry stays small; out/ is a persisted volume.
        """
        extra = {"attachment": attachment} if attachment else {}
        stages = {"proposed": proposed, "current": current} if proposal_id else {}
        await self._deliver(TELEGRAM, {"text": tg_text, "proposal_id": proposal_id, **stages, **extra})
        await self._deliver(EMAIL, {"url": url, "company": company, "role": role,
                                    "body": email_body, **extra})

    async def telegram(self, text: str) -> None:
        """Send an operational message that isn't about an application."""
        await self._deliver(TELEGRAM, {"text": text, "proposal_id": None})

    async def flush(self) -> None:
        """Retry everything queued. Called at the start of each cycle."""
        for item_id, channel, payload in self._db.outbox_items():
            try:
                await self._send(channel, payload)
            except Exception as exc:
                log.warning("notify: %s retry failed (item %d): %s", channel, item_id, exc)
                self._db.outbox_failed(item_id)
            else:
                log.info("notify: delivered queued %s (item %d)", channel, item_id)
                self._db.outbox_done(item_id)

    async def _deliver(self, channel: str, payload: dict) -> None:
        try:
            await self._send(channel, payload)
        except Exception as exc:  # any failure here means "queue it", whatever the cause
            log.error("notify: %s failed, queued for retry: %s", channel, exc)
            self._db.outbox_add(channel, payload)

    async def _send(self, channel: str, payload: dict) -> None:
        if channel == TELEGRAM:
            await self._send_telegram(payload)
        else:
            await self._send_email(payload)

    async def _send_telegram(self, payload: dict) -> None:
        attachment = _existing(payload.get("attachment"))
        if payload.get("proposal_id"):
            await self._bot.send_status_proposal(payload["proposal_id"], payload["text"],
                                                 payload.get("proposed", ""), payload.get("current", ""))
        elif attachment:
            await self._bot.send_document(attachment, payload["text"])
        else:
            await self._bot.send_message(payload["text"])

    async def _send_email(self, payload: dict) -> None:
        s = self._settings
        if s.dry_run:
            log.info("[dry-run] skipping email: %s - %s", payload["company"], payload["role"])
            return
        url = payload["url"]
        thread_id = self._db.get_thread_message_id(url) if url else None
        message_id = await asyncio.to_thread(
            notify_event, s.gmail_address, s.gmail_app_password, s.notify_email,
            payload["company"], payload["role"], payload["body"], thread_id=thread_id,
            attachments=[a] if (a := _existing(payload.get("attachment"))) else None,
        )
        if url and thread_id is None:
            self._db.update_job(url, thread_message_id=message_id)


def _existing(path: str | None) -> Path | None:
    """The attachment if it's still on disk. A deleted file sends the text alone, not nothing."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        log.warning("notify: attachment %s is gone, sending text only", path)
        return None
    return p
