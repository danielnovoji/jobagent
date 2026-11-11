import html
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from jobagent.statuses import CODE_OF, CODES, STATUSES

logger = logging.getLogger(__name__)

ApproveHandler = Callable[[int], Awaitable[None]]
SkipHandler = Callable[[int], Awaitable[None]]
SubmittedHandler = Callable[[int], Awaitable[None]]
StatusHandler = Callable[[int, bool], Awaitable[None]]  # proposal id, confirmed?


def esc(text: object) -> str:
    """Escape for Telegram HTML. Job titles contain "&", which fails the whole send."""
    return html.escape(str(text), quote=False)


@dataclass
class JobCard:
    job_id: int
    company: str
    role: str
    match_pct: int
    reasoning: str       # one-line
    variant: str
    prep_notes: str      # empty string if none
    apply_url: str
    has_recruiter_email: bool
    pdf_path: Optional[str] = None
    score_label: str = "Match"
    recruiter_email: str = ""
    job_number: str = ""


class TelegramBot:
    def __init__(self, token: str, chat_id: int):
        self._chat_id = chat_id
        self._app = Application.builder().token(token).build()
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._pending: dict[int, JobCard] = {}  # job_id -> card, for callback context
        self._on_approve: Optional[ApproveHandler] = None
        self._on_skip: Optional[SkipHandler] = None
        self._on_submitted: Optional[SubmittedHandler] = None
        self._on_status: Optional[StatusHandler] = None
        self._load_card: Optional[Callable[[int], Optional[JobCard]]] = None
        self._commands: list[BotCommand] = []

    # --- handler registration ---

    def on_approve(self, handler: ApproveHandler) -> None:
        self._on_approve = handler

    def on_skip(self, handler: SkipHandler) -> None:
        self._on_skip = handler

    def on_submitted(self, handler: SubmittedHandler) -> None:
        self._on_submitted = handler

    def on_status(self, handler: StatusHandler) -> None:
        self._on_status = handler

    def on_command(self, name: str, description: str, reply: Callable[[], Awaitable[str]]) -> None:
        """Answer /<name> with reply()'s text. Only Daniel's chat; anyone else gets silence."""
        async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await update.effective_message.reply_text(await reply(), parse_mode="HTML")
        self._app.add_handler(CommandHandler(name, handle, filters=filters.Chat(chat_id=self._chat_id)))
        self._commands.append(BotCommand(name, description))

    def set_card_loader(self, loader: Callable[[int], Optional[JobCard]]) -> None:
        """Rebuild a card from storage, so buttons keep working after a restart."""
        self._load_card = loader

    # --- public API ---

    async def send_job_card(self, card: JobCard) -> int:
        """Send the card; returns its Telegram message id, which a later bump replies to."""
        self._pending[card.job_id] = card
        text = _format_card(card)
        if card.pdf_path:
            with open(card.pdf_path, "rb") as f:
                msg = await self._app.bot.send_document(
                    chat_id=self._chat_id,
                    document=f,
                    caption=text,
                    reply_markup=_card_keyboard(card.job_id),
                    parse_mode="HTML",
                )
        else:
            msg = await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                reply_markup=_card_keyboard(card.job_id),
                parse_mode="HTML",
            )
        return msg.message_id

    async def send_bump(self, card: JobCard, text: str, reply_to: int | None) -> None:
        """A nudge for a card still waiting for Approve or Skip, as a reply to that card.

        Same buttons as the card. If the card was deleted it still goes out on its own.
        """
        self._pending[card.job_id] = card
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            reply_markup=_card_keyboard(card.job_id),
            reply_parameters=(ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                              if reply_to else None),
            parse_mode="HTML",
        )

    # --- lifecycle ---

    async def send_message(self, text: str) -> None:
        """Send a plain text notification (no inline keyboard)."""
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode="HTML",
        )

    async def send_document(self, path, caption: str) -> None:
        """Send a file with an HTML caption (Telegram caps captions at 1024 chars)."""
        with open(path, "rb") as f:
            await self._app.bot.send_document(
                chat_id=self._chat_id, document=f, caption=caption, parse_mode="HTML")

    async def send_approved_portal(self, card: "JobCard") -> None:
        """Send the apply-link message with Submitted button (portal jobs only)."""
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=f"Approved <b>{esc(card.company)} — {esc(card.role)}</b>.\n"
                 f'Apply here: <a href="{esc(card.apply_url)}">{esc(card.apply_url)}</a>\n'
                 "Tap <b>Submitted</b> once you've applied.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Submitted", callback_data=f"submitted:{card.job_id}"),
            ]]),
            parse_mode="HTML",
        )

    async def send_status_proposal(self, proposal_id: int, text: str,
                                   proposed: str = "", current: str = "") -> None:
        """Ask before changing a status in the sheet: the guess first, then every other stage."""
        await self._app.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            reply_markup=status_keyboard(proposal_id, proposed, current),
            parse_mode="HTML",
        )

    async def start_polling(self, drop_pending_updates: bool = False) -> None:
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=drop_pending_updates)
        if self._commands:
            try:  # the "/" menu in Telegram; nice to have, never worth failing over
                await self._app.bot.set_my_commands(self._commands)
            except Exception as exc:
                logger.warning("could not set the command menu: %s", exc)

    async def stop(self) -> None:
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    # --- internal ---

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            await query.answer()
        except BadRequest as exc:
            # Telegram expires old taps. The answer only stops the button spinner,
            # so a tap made during a long fetch cycle must still be acted on.
            logger.info("callback answer failed, continuing: %s", exc)

        # Only Daniel's chat can act. Today only his chat ever gets buttons, but a tap
        # from anywhere else must never approve, send or change anything.
        chat = query.message.chat.id if query.message else None
        if chat != self._chat_id:
            logger.warning("ignored a button tap from another chat (%s)", chat)
            return
        parts = query.data.split(":")
        if parts[0] == "rs":  # reply status: "rs:<proposal id>:<status code or x>"
            await query.edit_message_reply_markup(reply_markup=None)
            if self._on_status:
                await self._on_status(int(parts[1]), CODES.get(parts[2]))
            return
        action, job_id = parts[0], int(parts[1])

        card = self._pending.get(job_id)
        if card is None and self._load_card:
            card = self._load_card(job_id)

        if action == "approve":
            await query.edit_message_reply_markup(reply_markup=None)
            if card is None:
                logger.warning("approve callback for unknown job_id=%d (stale button?)", job_id)
                return
            if self._on_approve:
                await self._on_approve(job_id)
            # Email jobs have no Submitted step — drop from pending immediately.
            if card.has_recruiter_email:
                self._pending.pop(job_id, None)

        elif action == "skip":
            await query.edit_message_reply_markup(reply_markup=None)
            if card is None:
                logger.warning("skip callback for unknown job_id=%d", job_id)
                return
            if self._on_skip:
                await self._on_skip(job_id)
            self._pending.pop(job_id, None)

        elif action == "submitted":
            await query.edit_message_reply_markup(reply_markup=None)
            if self._on_submitted:
                await self._on_submitted(job_id)
            self._pending.pop(job_id, None)


def _card_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve ✓", callback_data=f"approve:{job_id}"),
        InlineKeyboardButton("Skip ✗", callback_data=f"skip:{job_id}"),
    ]])


def status_keyboard(proposal_id: int, proposed: str, current: str = "") -> InlineKeyboardMarkup:
    """The agent's guess on its own row, then the other stages two per row, then Ignore.

    The row's current stage is left out: choosing it would change nothing.
    """
    def button(status: str, label: str = "") -> InlineKeyboardButton:
        return InlineKeyboardButton(label or status, callback_data=f"rs:{proposal_id}:{CODE_OF[status]}")
    rows = [[button(proposed, f"✓ {proposed}")]] if proposed in CODE_OF else []
    others = [button(s) for s in STATUSES if s not in (proposed, current)]
    rows += [others[i:i + 2] for i in range(0, len(others), 2)]
    rows.append([InlineKeyboardButton("Ignore ✗", callback_data=f"rs:{proposal_id}:x")])
    return InlineKeyboardMarkup(rows)


def _format_card(card: JobCard) -> str:
    # Documents allow a 1024-character caption, so free text is capped here.
    lines = [
        f"<b>{esc(card.company)} — {esc(card.role)}</b>",
        f"{esc(card.score_label)}: <b>{card.match_pct}%</b> · variant: {esc(card.variant)}",
        esc(card.reasoning[:400]),
    ]
    if card.prep_notes:
        lines.append(f"Prep: {esc(card.prep_notes[:200])}")
    if card.job_number:
        lines.append(f"Job number: {esc(card.job_number)}")
    if card.recruiter_email:
        lines.append(f"Approve emails your CV to: {esc(card.recruiter_email)}")
    lines.append(f'<a href="{esc(card.apply_url)}">Apply link</a>')
    return "\n".join(lines)
