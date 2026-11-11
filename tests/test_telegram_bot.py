import pytest
from telegram.error import BadRequest
from unittest.mock import AsyncMock, MagicMock, patch

from jobagent.telegram_bot import JobCard, TelegramBot, _format_card


# --- _format_card ---

def test_format_card_with_prep():
    card = JobCard(
        job_id=1, company="Nvidia", role="SW Engineer",
        match_pct=82, reasoning="Strong C++ match",
        variant="systems", prep_notes="Review CUDA basics",
        apply_url="https://example.com/job/1",
        has_recruiter_email=False,
    )
    text = _format_card(card)
    assert "Nvidia" in text
    assert "82%" in text
    assert "systems" in text
    assert "Review CUDA basics" in text
    assert "https://example.com/job/1" in text


def test_format_card_no_prep():
    card = JobCard(
        job_id=2, company="Intel", role="C++ Dev",
        match_pct=70, reasoning="Good fit",
        variant="hft", prep_notes="",
        apply_url="https://example.com/job/2",
        has_recruiter_email=True,
    )
    text = _format_card(card)
    assert "Prep:" not in text


# --- TelegramBot ---

@pytest.fixture()
def mock_app():
    app = MagicMock()
    app.bot = AsyncMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    app.add_handler = MagicMock()
    return app


@pytest.fixture()
def bot(mock_app):
    with patch("jobagent.telegram_bot.Application") as MockApp:
        MockApp.builder.return_value.token.return_value.build.return_value = mock_app
        b = TelegramBot(token="fake:token", chat_id=123)
        b._app = mock_app
        return b


@pytest.mark.asyncio
async def test_send_job_card_no_pdf(bot, mock_app):
    card = JobCard(
        job_id=10, company="KLA", role="Algo Dev",
        match_pct=75, reasoning="C++ aligns well",
        variant="systems", prep_notes="",
        apply_url="https://example.com/10",
        has_recruiter_email=False,
    )
    await bot.send_job_card(card)
    mock_app.bot.send_message.assert_called_once()
    call_kwargs = mock_app.bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == 123
    assert "KLA" in call_kwargs["text"]
    assert "75%" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_send_job_card_stores_pending(bot):
    card = JobCard(
        job_id=20, company="Microsoft", role="SWE",
        match_pct=65, reasoning="Decent match",
        variant="systems", prep_notes="",
        apply_url="https://example.com/20",
        has_recruiter_email=True,
    )
    await bot.send_job_card(card)
    assert bot._pending[20] is card


@pytest.mark.asyncio
async def test_approve_email_job_calls_handler(bot, mock_app):
    card = JobCard(
        job_id=30, company="Apple", role="SWE",
        match_pct=80, reasoning="Good",
        variant="systems", prep_notes="",
        apply_url="https://example.com/30",
        has_recruiter_email=True,
    )
    bot._pending[30] = card

    approved_ids = []

    async def approve_handler(job_id: int):
        approved_ids.append(job_id)

    bot.on_approve(approve_handler)

    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "approve:30"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    await bot._handle_callback(update, MagicMock())

    assert approved_ids == [30]
    # email job: no follow-up Submitted message
    mock_app.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_approve_portal_job_calls_handler_keeps_pending(bot, mock_app):
    """Portal jobs: on_approve called, card stays in pending (run.py sends the follow-up)."""
    card = JobCard(
        job_id=40, company="Meta", role="SWE",
        match_pct=72, reasoning="OK match",
        variant="fullstack", prep_notes="",
        apply_url="https://example.com/40",
        has_recruiter_email=False,
    )
    bot._pending[40] = card
    approved_ids = []
    bot.on_approve(lambda jid: approved_ids.append(jid) or __import__('asyncio').sleep(0))

    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "approve:40"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    await bot._handle_callback(update, MagicMock())

    # portal job: bot does NOT send the follow-up (run.py does)
    mock_app.bot.send_message.assert_not_called()
    # card stays in pending until Submitted
    assert 40 in bot._pending


@pytest.mark.asyncio
async def test_skip_calls_handler_and_removes_pending(bot):
    card = JobCard(
        job_id=50, company="Google", role="SWE",
        match_pct=55, reasoning="Below threshold",
        variant="systems", prep_notes="",
        apply_url="https://example.com/50",
        has_recruiter_email=False,
    )
    bot._pending[50] = card

    skipped_ids = []

    async def skip_handler(job_id: int):
        skipped_ids.append(job_id)

    bot.on_skip(skip_handler)

    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "skip:50"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    await bot._handle_callback(update, MagicMock())

    assert skipped_ids == [50]
    assert 50 not in bot._pending


@pytest.mark.asyncio
async def test_submitted_calls_handler_and_removes_pending(bot):
    bot._pending[60] = MagicMock()

    submitted_ids = []

    async def submitted_handler(job_id: int):
        submitted_ids.append(job_id)

    bot.on_submitted(submitted_handler)

    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "submitted:60"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    await bot._handle_callback(update, MagicMock())

    assert submitted_ids == [60]
    assert 60 not in bot._pending


@pytest.mark.asyncio
async def test_approve_email_job_removes_pending(bot, mock_app):
    """Email jobs should be removed from pending immediately on approve (no Submitted step)."""
    card = JobCard(
        job_id=35, company="Intel", role="C++ Dev",
        match_pct=78, reasoning="Solid match",
        variant="systems", prep_notes="",
        apply_url="https://example.com/35",
        has_recruiter_email=True,
    )
    bot._pending[35] = card
    bot.on_approve(AsyncMock())

    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "approve:35"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    await bot._handle_callback(update, MagicMock())

    assert 35 not in bot._pending


@pytest.mark.asyncio
async def test_stale_callback_is_safe(bot, mock_app, caplog):
    """Approve on a job_id not in _pending should log a warning and not crash."""
    import logging
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = "approve:999"
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    query.message.chat.id = 123  # the bot fixture's chat
    update.callback_query = query

    with caplog.at_level(logging.WARNING, logger="jobagent.telegram_bot"):
        await bot._handle_callback(update, MagicMock())

    assert "stale" in caplog.text.lower() or "unknown" in caplog.text.lower()
    mock_app.bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Escaping, restarts, status buttons
# ---------------------------------------------------------------------------

def _card(**overrides):
    fields = dict(job_id=1, company="KLA", role="Electronics & Control Engineer", match_pct=70,
                  reasoning="Needs <5 years", variant="systems", prep_notes="",
                  apply_url="https://x/1?a=1&b=2", has_recruiter_email=False)
    return JobCard(**{**fields, **overrides})


def _query(data, chat_id=123):
    query = MagicMock()
    query.data = data
    query.message.chat.id = chat_id  # the bot fixture's chat
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    return MagicMock(callback_query=query)


def test_format_card_escapes_html():
    """A real KLA title has "&", and one unescaped character fails the whole send."""
    text = _format_card(_card())
    assert "Electronics &amp; Control Engineer" in text
    assert "Needs &lt;5 years" in text
    assert "a=1&amp;b=2" in text


def test_format_card_shows_recruiter_address():
    text = _format_card(_card(has_recruiter_email=True, recruiter_email="hr@x.co"))
    assert "hr@x.co" in text


def test_format_card_fits_a_document_caption():
    text = _format_card(_card(reasoning="x" * 5000, prep_notes="y" * 5000))
    assert len(text) < 1024


@pytest.mark.asyncio
async def test_button_survives_restart_via_card_loader(bot):
    handler = AsyncMock()
    bot.on_approve(handler)
    bot.set_card_loader(lambda job_id: _card(job_id=job_id))  # nothing in _pending
    await bot._handle_callback(_query("approve:7"), MagicMock())
    handler.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_tap_still_works_when_telegram_expired_the_query(bot):
    handler = AsyncMock()
    bot.on_skip(handler)
    bot.set_card_loader(lambda job_id: _card(job_id=job_id))
    update = _query("skip:7")
    update.callback_query.answer.side_effect = BadRequest("Query is too old")
    await bot._handle_callback(update, MagicMock())
    handler.assert_awaited_once_with(7)


@pytest.mark.asyncio
@pytest.mark.parametrize("data,status", [("rs:3:rej", "נדחה"), ("rs:3:int", "מחכה לראיון"), ("rs:3:x", None)])
async def test_status_buttons_call_handler(bot, data, status):
    handler = AsyncMock()
    bot.on_status(handler)
    await bot._handle_callback(_query(data), MagicMock())
    handler.assert_awaited_once_with(3, status)


def test_status_keyboard_puts_the_guess_first_and_skips_the_current_stage():
    from jobagent.statuses import STATUSES
    from jobagent.telegram_bot import status_keyboard
    rows = status_keyboard(12345, "מחכה לראיון", current="התקבלה תשובה").inline_keyboard
    labels = [b.text for row in rows for b in row]
    assert labels[0] == "✓ מחכה לראיון" and labels[-1] == "Ignore ✗"
    assert "התקבלה תשובה" not in labels and len(labels) == len(STATUSES)  # guess + others + ignore
    assert all(len(b.callback_data.encode()) <= 64 for row in rows for b in row)


def test_format_card_shows_job_number():
    assert "Job number: JR2025751" in _format_card(_card(job_number="JR2025751"))


@pytest.mark.asyncio
async def test_send_job_card_returns_the_message_id(bot, mock_app):
    mock_app.bot.send_message.return_value = MagicMock(message_id=55)
    card = JobCard(job_id=1, company="KLA", role="SWE", match_pct=80, reasoning="r", variant="systems",
                   prep_notes="", apply_url="https://x", has_recruiter_email=False)
    assert await bot.send_job_card(card) == 55


@pytest.mark.asyncio
async def test_bump_replies_to_the_card_and_keeps_its_buttons(bot, mock_app):
    card = JobCard(job_id=7, company="KLA", role="SWE", match_pct=80, reasoning="r", variant="systems",
                   prep_notes="", apply_url="https://x", has_recruiter_email=False)
    await bot.send_bump(card, "ping", 55)
    kwargs = mock_app.bot.send_message.await_args.kwargs
    assert kwargs["reply_parameters"].message_id == 55
    assert kwargs["reply_parameters"].allow_sending_without_reply is True
    assert [b.callback_data for b in kwargs["reply_markup"].inline_keyboard[0]] == ["approve:7", "skip:7"]


@pytest.mark.asyncio
async def test_taps_from_another_chat_do_nothing(bot):
    handler = AsyncMock()
    bot.on_approve(handler)
    bot.on_status(handler)
    for data in ("approve:1", "rs:3:rej"):
        await bot._handle_callback(_query(data, chat_id=999), MagicMock())
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_command_answers_only_daniels_chat(bot, mock_app):
    from telegram.ext import CommandHandler
    reply = AsyncMock(return_value="✅ running")
    bot.on_command("status", "Is the agent running?", reply)
    handler = mock_app.add_handler.call_args.args[0]
    assert isinstance(handler, CommandHandler) and handler.commands == frozenset({"status"})
    assert handler.filters.chat_ids == frozenset({123})          # nobody else gets an answer
    update = MagicMock(); update.effective_message.reply_text = AsyncMock()
    await handler.callback(update, MagicMock())
    update.effective_message.reply_text.assert_awaited_once_with("✅ running", parse_mode="HTML")
