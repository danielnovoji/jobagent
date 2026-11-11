"""The both-channels rule: one failure never loses an update."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jobagent.db import JobDb
from jobagent.models import JobRecord
from jobagent.notify import EMAIL, TELEGRAM, Notifier

URL = "https://x/1"


@pytest.fixture
def db(tmp_path):
    db = JobDb(tmp_path / "t.db")
    db.insert_if_new(JobRecord(source="t", company="KLA", title="SWE", location="Israel", url=URL))
    yield db
    db.close()


def _bot():
    return MagicMock(send_message=AsyncMock(), send_status_proposal=AsyncMock())


def _notifier(db, bot, dry_run=False):
    return Notifier(MagicMock(dry_run=dry_run), db, bot)


async def _event(notifier, **extra):
    await notifier.event(url=URL, company="KLA", role="SWE", tg_text="tg", email_body="body", **extra)


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m1>")
async def test_event_goes_to_both_channels(mock_email, db):
    bot = _bot()
    await _event(_notifier(db, bot))
    bot.send_message.assert_awaited_once_with("tg")
    mock_email.assert_called_once()
    assert db.outbox_items() == []


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m1>")
async def test_telegram_failure_still_emails_and_queues_retry(mock_email, db):
    bot = _bot()
    bot.send_message.side_effect = RuntimeError("telegram down")
    await _event(_notifier(db, bot))
    mock_email.assert_called_once()
    assert [c for _, c, _ in db.outbox_items()] == [TELEGRAM]


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", side_effect=OSError("smtp down"))
async def test_email_failure_still_sends_telegram_and_queues_retry(mock_email, db):
    bot = _bot()
    await _event(_notifier(db, bot))
    bot.send_message.assert_awaited_once()
    assert [c for _, c, _ in db.outbox_items()] == [EMAIL]


@pytest.mark.asyncio
async def test_flush_delivers_queued_items_and_clears_them(db):
    db.outbox_add(TELEGRAM, {"text": "queued", "proposal_id": None})
    bot = _bot()
    await _notifier(db, bot).flush()
    bot.send_message.assert_awaited_once_with("queued")
    assert db.outbox_items() == []


@pytest.mark.asyncio
async def test_flush_keeps_items_that_fail_again(db):
    db.outbox_add(TELEGRAM, {"text": "queued", "proposal_id": None})
    bot = _bot()
    bot.send_message.side_effect = RuntimeError("still down")
    await _notifier(db, bot).flush()
    assert len(db.outbox_items()) == 1


@pytest.mark.asyncio
async def test_queued_proposal_keeps_its_buttons(db):
    db.outbox_add(TELEGRAM, {"text": "reply", "proposal_id": 9})
    bot = _bot()
    await _notifier(db, bot).flush()
    bot.send_status_proposal.assert_awaited_once_with(9, "reply", "", "")


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", side_effect=["<first>", "<second>"])
async def test_updates_for_one_job_share_a_gmail_thread(mock_email, db):
    notifier = _notifier(db, _bot())
    await _event(notifier)
    await _event(notifier)
    assert mock_email.call_args_list[0].kwargs["thread_id"] is None
    assert mock_email.call_args_list[1].kwargs["thread_id"] == "<first>"
    assert db.get_thread_message_id(URL) == "<first>"


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event")
async def test_dry_run_sends_no_email(mock_email, db):
    await _event(_notifier(db, _bot(), dry_run=True))
    mock_email.assert_not_called()
    assert db.outbox_items() == []


# --- attachments (interview prep sheet) ---

def _bot_with_docs():
    return MagicMock(send_message=AsyncMock(), send_status_proposal=AsyncMock(),
                     send_document=AsyncMock())


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m1>")
async def test_attachment_goes_to_both_channels(mock_email, db, tmp_path):
    pdf = tmp_path / "prep.pdf"
    pdf.write_bytes(b"%PDF")
    bot = _bot_with_docs()
    await _event(_notifier(db, bot), attachment=str(pdf))
    bot.send_document.assert_awaited_once_with(pdf, "tg")
    bot.send_message.assert_not_awaited()
    assert mock_email.call_args.kwargs["attachments"] == [pdf]


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m1>")
async def test_queued_retry_keeps_the_attachment(mock_email, db, tmp_path):
    pdf = tmp_path / "prep.pdf"
    pdf.write_bytes(b"%PDF")
    bot = _bot_with_docs()
    bot.send_document.side_effect = RuntimeError("telegram down")
    notifier = _notifier(db, bot)
    await _event(notifier, attachment=str(pdf))
    bot.send_document.side_effect = None
    await notifier.flush()
    assert bot.send_document.await_count == 2
    assert bot.send_document.await_args.args[0] == pdf
    assert db.outbox_items() == []


@pytest.mark.asyncio
@patch("jobagent.notify.notify_event", return_value="<m1>")
async def test_missing_attachment_still_sends_the_text(mock_email, db, tmp_path):
    bot = _bot_with_docs()
    await _event(_notifier(db, bot), attachment=str(tmp_path / "gone.pdf"))
    bot.send_message.assert_awaited_once_with("tg")
    assert mock_email.call_args.kwargs["attachments"] is None
