#!/usr/bin/env python3
"""Main pipeline: fetch jobs, score, tailor, send Telegram cards, handle approvals.

Schedule (Israel time, configurable via .env):
  FETCH_TIMES=08:00,20:00   — two fetch+score cycles per day
  DIGEST_TIME=19:00         — one daily summary digest

Modes:
  ANTHROPIC_API_KEY set   → LLM scoring + tailoring, threshold MATCH_THRESHOLD
  ANTHROPIC_API_KEY empty → keyword scoring, threshold 10, base summaries only
  DRY_RUN=true            → cap at 5 cards; Approve/Submitted log only, no writes/emails

Design: a job's status in SQLite records how far it got, so nothing depends on
in-memory state. A crash, restart or API outage resumes on the next cycle, and a
Telegram button keeps working after a restart. Network calls run in threads so a
long cycle doesn't freeze the bot; all SQLite access stays on the event-loop thread.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import signal
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from sqlite3 import Row

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from jobagent.config import load_settings
from jobagent.db import (
    JobDb,
    REPLY_CONFIRMED,
    REPLY_IGNORED,
    REPLY_INFO,
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_FILTERED_OUT,
    STATUS_SCORE_FAILED,
    STATUS_SCORED,
    STATUS_SENT,
    STATUS_SKIPPED,
)
from jobagent.filters import (
    passes_clearance,
    passes_description_seniority,
    passes_location,
    passes_title_seniority,
)
from jobagent.gmail_imap import GmailImap
from jobagent.gmail_scanner import scan_for_replies
from jobagent.gmail_smtp import apply_by_email
from jobagent.http_client import HttpClient
from jobagent.keyword_scorer import keyword_score_job
from jobagent.models import JobRecord, job_number
from jobagent.health import disk_free_gb, duration, power_line, power_state
from jobagent.notify import Notifier
from jobagent.prep import write_prep
from jobagent.recruiter import recruiter_email
from jobagent.scorer import LLMUnavailable, ScoreResult, score_job
from jobagent.tailor import base_summary, cover_note, render_cv, tailor_summary
from jobagent.telegram_bot import JobCard, TelegramBot, esc
from jobagent.statuses import CLOSED_STATUSES, PREP_STATUSES, STATUSES, VALID_STATUSES
from jobagent.tracker import (
    STATUS_SUBMITTED,
    NewApplication,
    SheetTracker,
)

from jobagent.sources.amazon import fetch as amazon_fetch
from jobagent.sources.apple import fetch as apple_fetch
from jobagent.sources.apple import fetch_description as apple_fetch_desc
from jobagent.sources.comeet import BOARDS as COMEET_BOARDS
from jobagent.sources.comeet import fetch as comeet_fetch
from jobagent.sources.greenhouse import BOARDS as GREENHOUSE_BOARDS
from jobagent.sources.greenhouse import fetch as greenhouse_fetch
from jobagent.sources.greenhouse import fetch_description as greenhouse_fetch_desc
from jobagent.sources.jobify import fetch as jobify_fetch
from jobagent.sources.jobify import fetch_description as jobify_fetch_desc
from jobagent.sources.linkedin import fetch_description as linkedin_fetch_desc
from jobagent.sources.meta import fetch as meta_fetch
from jobagent.sources.microsoft import SITES as EIGHTFOLD_SITES
from jobagent.sources.microsoft import fetch as ms_fetch
from jobagent.sources.microsoft import fetch_description as ms_fetch_desc
from jobagent.sources.smartrecruiters import COMPANIES as SMARTRECRUITERS_COMPANIES
from jobagent.sources.smartrecruiters import fetch as smartrecruiters_fetch
from jobagent.sources.smartrecruiters import fetch_description as smartrecruiters_fetch_desc
from jobagent.sources.workday import COMPANIES as WORKDAY_COMPANIES
from jobagent.sources.workday import fetch_company as workday_fetch
from jobagent.sources.workday import fetch_description as workday_fetch_desc

log = logging.getLogger(__name__)

DATA_DIR = Path("data")
OUT_DIR = Path("out")

SCORER_LLM = "llm"
SCORER_KEYWORD = "keyword"

# Fetches are 12h apart, so a last fetch older than 13h means a slot was missed (laptop
# asleep or off). The extra hour keeps a heartbeat at 20:00 from racing the 20:00 fetch.
CATCH_UP_AFTER = timedelta(hours=13)
HEARTBEAT_EVERY = timedelta(minutes=10)
OFFLINE_ALERT_AFTER = timedelta(minutes=30)
LOW_BATTERY = 20
_STARTED_AT = datetime.now(timezone.utc)
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_NO_LLM_THRESHOLD = 10  # keyword scorer; any job with 2+ skill-term hits gets a card
_MIN_DESCRIPTION = 50   # shorter than this is a stub, not a real description

_DESCRIPTION_FETCHERS = {
    "workday:": workday_fetch_desc,
    "jobify": jobify_fetch_desc,
    "linkedin": linkedin_fetch_desc,
    "microsoft": ms_fetch_desc,
    "qualcomm": ms_fetch_desc,
    "apple": apple_fetch_desc,
    "greenhouse:": greenhouse_fetch_desc,
    "smartrecruiters:": smartrecruiters_fetch_desc,
}


def _filter(job: JobRecord) -> bool:
    return (
        passes_location(job.location)
        and passes_title_seniority(job.title)
        and (not job.description or passes_description_seniority(job.description))
        and (not job.description or passes_clearance(job.description, job.location))
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_all(client: HttpClient, settings, known_urls=frozenset()) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    for cfg in WORKDAY_COMPANIES:
        try:
            jobs.extend(workday_fetch(client, cfg, known_urls))
        except Exception as exc:
            log.error("workday/%s: %s", cfg.company, exc)
    for fn in (ms_fetch, apple_fetch, jobify_fetch, meta_fetch, greenhouse_fetch,
               smartrecruiters_fetch, amazon_fetch, comeet_fetch):
        try:
            jobs.extend(fn(client))
        except Exception as exc:
            log.error("%s: %s", fn.__module__, exc)
    try:
        with GmailImap(settings.gmail_address, settings.gmail_app_password,
                       settings.gmail_alerts_label) as imap:
            for alert in imap.recent_alerts():
                for li in alert.jobs:
                    jobs.append(JobRecord(
                        source="linkedin_email",
                        company=li.company or "",
                        title=li.title,
                        location=li.location or "Israel",
                        url=li.url,
                    ))
    except Exception as exc:
        log.error("linkedin_alerts: %s", exc)
    return jobs


def _fetch_description(client: HttpClient, job: JobRecord) -> str:
    for prefix, fetch in _DESCRIPTION_FETCHERS.items():
        if job.source.startswith(prefix):
            return fetch(client, job)
    return ""


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

async def _score_queue(settings, db: JobDb, tracker: SheetTracker, client: HttpClient,
                       use_llm: bool) -> None:
    """Score every queued job. Raises LLMUnavailable; the rest stay queued for next cycle."""
    today = date.today()
    for job in db.retryable_new():
        if len(job.description) < _MIN_DESCRIPTION:
            full = await asyncio.to_thread(_fetch_description, client, job)
            if full:
                job.description = full
                db.update_job(job.url, description=full)
        if not job.description:
            # Scoring a title alone gives confident nonsense, so wait for the text.
            log.info("no description yet, will retry: %s", job.url)
            continue
        if not _filter(job) or tracker.is_duplicate(job.company, job.title, job_number(job.url)):
            db.update_job(job.url, status=STATUS_FILTERED_OUT)
            continue
        if tracker.apps_in_last_30_days(job.company, today) >= settings.max_apps_per_company_30d:
            log.info("company cap: %s", job.company)
            db.update_job(job.url, status=STATUS_FILTERED_OUT)
            continue

        if use_llm:
            result = await asyncio.to_thread(score_job, job, settings.anthropic_api_key)
        else:
            result = keyword_score_job(job)

        if result is None:
            log.warning("scoring failed: %s", job.url)
            db.update_job(job.url, status=STATUS_SCORE_FAILED)
            continue
        db.update_job(
            job.url, status=STATUS_SCORED, score=result.score, variant=result.variant,
            score_json=json.dumps(dataclasses.asdict(result), ensure_ascii=False),
            scored_by=SCORER_LLM if use_llm else SCORER_KEYWORD,
        )


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def _job_from_row(row: Row) -> JobRecord:
    return JobRecord(
        source=row["source"], company=row["company"], title=row["title"],
        location=row["location"], url=row["url"], description=row["description"],
        posted_date=row["posted_date"],
    )


def _score_from_row(row: Row) -> ScoreResult:
    return ScoreResult(**json.loads(row["score_json"]))


def _prep_notes(result: ScoreResult) -> str:
    if not result.needs_review:
        return ""
    return "Review before interview: " + ", ".join(result.needs_review)


def _card_from_row(row: Row, pdf: Path | None = None, email: str | None = None) -> JobCard:
    result = _score_from_row(row)
    email = row["recruiter_email"] if email is None else email
    pdf_path = str(pdf) if pdf else row["pdf_path"]
    return JobCard(
        job_id=row["id"],
        company=row["company"],
        role=row["title"],
        match_pct=result.score,
        reasoning=result.reason,
        variant=result.variant,
        prep_notes=_prep_notes(result),
        apply_url=row["url"],
        has_recruiter_email=bool(email),
        recruiter_email=email,
        pdf_path=pdf_path,
        score_label="Match" if row["scored_by"] == SCORER_LLM else "Keyword match",
        job_number=job_number(row["url"]),
    )


async def _send_card(settings, db: JobDb, bot: TelegramBot, row: Row, use_llm: bool) -> bool:
    job, result = _job_from_row(row), _score_from_row(row)
    if use_llm:
        summary = await asyncio.to_thread(
            tailor_summary, job, result, settings.anthropic_api_key)
    else:
        summary = base_summary(result.variant)

    # One folder per job: PDFs share a file name, and the attachment on a later
    # Approve must be the CV tailored for this job, not the last one rendered.
    pdf = await asyncio.to_thread(render_cv, job, result, summary, OUT_DIR / str(row["id"]))
    email = recruiter_email(job.source, job.description)
    try:
        message_id = await bot.send_job_card(_card_from_row(row, pdf, email))
    except Exception as exc:
        log.error("card failed, will retry next cycle: %s: %s", job.url, exc)
        return False
    db.update_job(
        job.url, status=STATUS_SENT, sent_at=datetime.now(timezone.utc).isoformat(),
        pdf_path=str(pdf) if pdf else None, recruiter_email=email,
        card_message_id=str(message_id) if message_id else None,
    )
    log.info("card sent: %s/%s score=%d variant=%s",
             job.company, job.title, result.score, result.variant)
    return True


async def _send_cards(settings, db: JobDb, tracker: SheetTracker, bot: TelegramBot,
                      threshold: int, use_llm: bool) -> None:
    """Send the best waiting jobs, within the daily cap and the per-company limit.

    The cap counts today's cards in the DB rather than per run, so two fetches a
    day can't send double. Whatever doesn't fit stays queued for the next slot.
    """
    today = date.today()
    cap = 5 if settings.dry_run else settings.daily_card_cap
    slots = cap - db.count_sent_today()
    for row in db.pending_cards(threshold):
        if slots <= 0:
            break
        company = row["company"]
        taken = tracker.apps_in_last_30_days(company, today) + db.count_open_cards(company)
        if taken >= settings.max_apps_per_company_30d:
            continue
        if await _send_card(settings, db, bot, row, use_llm):
            slots -= 1


async def run_fetch_cycle(settings, db: JobDb, tracker: SheetTracker, bot: TelegramBot) -> None:
    """Full pipeline: fetch → queue → enrich, filter, score → send the best cards."""
    use_llm = bool(settings.anthropic_api_key)
    threshold = settings.match_threshold if use_llm else _NO_LLM_THRESHOLD
    if use_llm:
        db.requeue_not_scored_by(SCORER_LLM)

    with HttpClient() as client:
        jobs = await asyncio.to_thread(_fetch_all, client, settings, db.known_urls())
        _record_health(jobs)
        # Free checks first: only jobs that pass reach a page fetch or the LLM.
        queued = sum(
            1 for job in jobs
            if passes_location(job.location) and passes_title_seniority(job.title)
            and db.insert_if_new(job)
        )
        log.info("fetch cycle: %d fetched, %d new (use_llm=%s, dry_run=%s)",
                 len(jobs), queued, use_llm, settings.dry_run)
        db.set_meta("last_fetch_summary", f"({len(jobs)} jobs, {queued} new)")
        try:
            await _score_queue(settings, db, tracker, client, use_llm)
        except LLMUnavailable as exc:
            log.error("scoring paused, jobs stay queued: %s", exc)
            await bot.send_message(
                f"Scoring paused, jobs stay queued for the next cycle: {esc(exc)}")

    await _send_cards(settings, db, tracker, bot, threshold, use_llm)


# ---------------------------------------------------------------------------
# Replies and digest
# ---------------------------------------------------------------------------

def _sheet_link(settings) -> str:
    return f"https://docs.google.com/spreadsheets/d/{settings.tracker_sheet_id}"


def _sheet_row(tracker: SheetTracker, company: str, role: str):
    company, role = company.lower(), role.lower()
    return next((r for r in tracker.read_all()
                 if r.company.lower() == company and r.role.lower() == role), None)


async def run_reply_scan(settings, db: JobDb, tracker: SheetTracker, notifier: Notifier) -> None:
    """Scan the inbox for replies about applications; ask on Telegram before touching the sheet.

    Every row still in progress is watched, so a row follows its application from
    stage to stage. Only rejected and withdrawn rows are closed.
    """
    applied = [(r.company, r.role) for r in tracker.read_all() if r.status not in CLOSED_STATUSES]
    replies = await asyncio.to_thread(scan_for_replies, settings, applied, db.seen_reply_ids())

    for reply in replies:
        proposal_id = db.add_reply_proposal(
            reply.message_id, reply.company, reply.role, reply.proposed_status)
        row = _sheet_row(tracker, reply.company, reply.role)
        current = row.status if row else ""
        mail = (f"From: {esc(reply.sender)}\nSubject: {esc(reply.subject)}\n"
                f"<blockquote>{esc(reply.snippet)}</blockquote>")
        mail_text = f"From: {reply.sender}\nSubject: {reply.subject}\n\n{reply.snippet}\n"
        if row and current == reply.proposed_status:
            # e.g. a second interview round: worth knowing, but there's nothing to change
            db.claim_reply_proposal(proposal_id, REPLY_INFO)
            prep = db.prep_path_for(row.url) if row.url and current in PREP_STATUSES else None
            await notifier.event(
                url=row.url, company=reply.company, role=reply.role, attachment=prep,
                tg_text=(f"<b>Another email from {esc(reply.company)}</b> re: {esc(reply.role)}\n{mail}\n"
                         f"Status stays <b>{esc(current)}</b>." + ("\nInterview prep attached." if prep else "")),
                email_body=(f"Event: recruiter reply detected (status unchanged)\n"
                            f"Company: {reply.company}\nRole: {reply.role}\nStatus: {current}\n\n"
                            f"{mail_text}\nSheet: {_sheet_link(settings)}"),
            )
            continue
        await notifier.event(
            url=row.url if row else "", company=reply.company, role=reply.role,
            proposal_id=proposal_id, proposed=reply.proposed_status, current=current,
            tg_text=(f"<b>Email from {esc(reply.company)}</b> re: {esc(reply.role)}\n{mail}\n"
                     f"Status now: {esc(current or '?')}. Looks like: <b>{esc(reply.proposed_status)}</b>.\n"
                     "Tap the stage to set it in the sheet:"),
            email_body=(f"Event: recruiter reply detected\n"
                        f"Company: {reply.company}\nRole: {reply.role}\n"
                        f"Status now: {current}\nProposed status: {reply.proposed_status}\n\n"
                        f"{mail_text}\nSheet: {_sheet_link(settings)}"),
        )


async def run_daily_digest(settings, db: JobDb, tracker: SheetTracker, notifier: Notifier) -> None:
    """Send a daily summary digest on Telegram and email, and clear out old files."""
    pruned = await asyncio.to_thread(prune_outputs, db)
    counts = Counter(r.status for r in tracker.read_all())
    stages = " | ".join(f"{s}: {counts[s]}" for s in STATUSES if counts[s]) or "no applications yet"
    new_today = db.count_today()
    sheet_url = _sheet_link(settings)
    health_tg, health_mail = _health_lines()
    await notifier.event(
        url="",
        company="jobagent",
        role="Daily Digest",
        tg_text=(
            f"<b>Daily digest</b>\n"
            f"New jobs seen today: {new_today}\n"
            f"{esc(stages)}\n"
            f"{health_tg}\n"
            f"Agent up {duration(datetime.now(timezone.utc) - _STARTED_AT)} · {esc(power_line())}\n"
            f'<a href="{esc(sheet_url)}">Open sheet</a>'
        ),
        email_body=(
            f"Daily digest\nNew today: {new_today}\n{stages}\n\n"
            f"Sources (last fetch):\n{health_mail}\n"
            + (f"\nRemoved files of {pruned} old undecided jobs.\n" if pruned else "")
            + f"\n{sheet_url}"
        ),
    )


# ---------------------------------------------------------------------------
# Source health and housekeeping
# ---------------------------------------------------------------------------

# label -> jobs the source returned in the last fetch. In memory: a fresh process
# fetches on start (see main), so the digest after a restart still has numbers.
_SOURCE_HEALTH: dict[str, int] = {}
# Sources where zero jobs can be normal. LinkedIn alerts aren't one: one arrives every
# day and a week of them is read, so zero from them is a fault (it hid one, 2026-11-11).
_ZERO_IS_NORMAL: set[str] = set()
OUTPUT_KEEP_DAYS = 60


def _expected_sources() -> dict[str, str]:
    """Every configured source tag -> a label for Daniel. A missing tag means 0 jobs."""
    tags = {f"workday:{c.tenant}": c.company for c in WORKDAY_COMPANIES}
    tags.update({f"greenhouse:{b.token}": b.company for b in GREENHOUSE_BOARDS})
    tags.update({f"smartrecruiters:{c.company_id}": c.name for c in SMARTRECRUITERS_COMPANIES})
    tags.update({f"comeet:{b.slug}": b.company for b in COMEET_BOARDS})
    tags.update({s.source: s.company for s in EIGHTFOLD_SITES})
    tags.update({"apple": "Apple", "jobify": "Jobify", "meta": "Meta", "amazon": "Amazon",
                 "linkedin_email": "LinkedIn alerts"})
    return tags


def _record_health(jobs: list[JobRecord]) -> None:
    counts = Counter(job.source for job in jobs)
    _SOURCE_HEALTH.clear()
    _SOURCE_HEALTH.update({label: counts.get(tag, 0) for tag, label in _expected_sources().items()})


def _health_lines() -> tuple[str, str]:
    """(Telegram line, email list). Telegram names only the sources that look broken."""
    if not _SOURCE_HEALTH:
        return "Sources: no fetch since the agent started.", "no fetch since the agent started"
    zero = [label for label, n in _SOURCE_HEALTH.items() if n == 0 and label not in _ZERO_IS_NORMAL]
    ok = len(_SOURCE_HEALTH) - len(zero)
    line = f"Sources: {ok}/{len(_SOURCE_HEALTH)} OK, {sum(_SOURCE_HEALTH.values())} jobs"
    if zero:
        line += f"\n⚠ No jobs from: {esc(', '.join(zero))} (site may have changed)"
    mail = "\n".join(f"  {label}: {n}" for label, n in sorted(_SOURCE_HEALTH.items()))
    return line, mail


# ---------------------------------------------------------------------------
# Bumps: a nudge for cards nobody tapped yet
# ---------------------------------------------------------------------------

BUMP_EVERY = timedelta(hours=4)
MAX_BUMPS = 3
BUMP_HOURS = range(9, 22)  # Israel time: no job jokes at 3 am
_BUMP_LINES = [
    "This card has been blocked in wait() for {hours} hours. Approve or Skip to signal it.",
    "Friendly ping: this {score}% match won't std::move itself into your career.",
    "Unanswered cards leak memory. Please free() this one: Approve or Skip.",
    "{company} is still hiring and this card is still pending. No segfault, it's just waiting for your input.",
    "Knock knock. Who's there? A {score}% match. A {score}% match who? A {score}% match still waiting for you.",
    "You and the other applicants are in a race condition here. Want to take the lock?",
]


def _bump_text(row: Row, n: int) -> str:
    result = _score_from_row(row)
    sent = datetime.fromisoformat(row["sent_at"])
    hours = max(1, int((datetime.now(timezone.utc) - sent).total_seconds() // 3600))
    joke = _BUMP_LINES[(int(row["id"]) + n) % len(_BUMP_LINES)].format(
        hours=hours, score=result.score, company=row["company"])
    tail = "Last reminder, then it stays quiet." if n >= MAX_BUMPS else f"Bump {n}/{MAX_BUMPS}."
    return (f"⏰ <b>{esc(row['company'])} — {esc(row['title'])}</b> ({result.score}%)\n"
            f"{esc(joke)}\n<i>{tail}</i>")


async def run_bumps(settings, db: JobDb, tracker: SheetTracker, bot: TelegramBot,
                    now: datetime | None = None) -> int:
    """Nudge cards that are still waiting for Approve or Skip. Costs no API calls.

    Every BUMP_EVERY at most, MAX_BUMPS times per card, only in waking hours. A
    job that meanwhile got into the sheet (added by hand) is closed, not bumped.
    """
    now = now or datetime.now(timezone.utc)
    if now.astimezone(ISRAEL_TZ).hour not in BUMP_HOURS:
        return 0
    bumped = 0
    for row in db.due_bumps(now - BUMP_EVERY, MAX_BUMPS):
        job = _job_from_row(row)
        if tracker.is_duplicate(job.company, job.title, job_number(job.url)):
            db.claim(row["id"], STATUS_SENT, STATUS_SKIPPED)
            continue
        try:
            await bot.send_bump(_card_from_row(row), _bump_text(row, row["bumps"] + 1),
                                int(row["card_message_id"]))
        except Exception as exc:  # try again next hour
            log.warning("bump failed for %s: %s", job.url, exc)
            continue
        db.note_bump(row["url"], now)
        bumped += 1
    return bumped


def fetch_overdue(db: JobDb, now: datetime | None = None) -> bool:
    """True if a fetch slot was missed. A restart soon after a fetch isn't, so a crash
    loop can't hammer the sites."""
    now = now or datetime.now(timezone.utc)
    last = db.get_meta("last_fetch_at")
    return not last or datetime.fromisoformat(last) < now - CATCH_UP_AFTER


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(ISRAEL_TZ).strftime("%d/%m %H:%M")


def _next_fetch(settings, now: datetime) -> str:
    local = now.astimezone(ISRAEL_TZ)
    slots = sorted(tuple(int(x) for x in t.strip().split(":")) for t in settings.fetch_times.split(","))
    for h, m in slots:
        if local.replace(hour=h, minute=m, second=0, microsecond=0) > local:
            return f"{h:02d}:{m:02d} today"
    return f"{slots[0][0]:02d}:{slots[0][1]:02d} tomorrow"


def status_text(settings, db: JobDb, now: datetime | None = None) -> str:
    """The /status reply: everything that says the agent is fine, read locally."""
    now = now or datetime.now(timezone.utc)
    last = db.get_meta("last_fetch_at")
    lines = [
        f"✅ <b>Agent running</b> (up {duration(now - _STARTED_AT)})" + (" [DRY RUN]" if settings.dry_run else ""),
        esc(power_line()),
        f"Last fetch: {_local(last)} {esc(db.get_meta('last_fetch_summary') or '')}".rstrip() if last
        else "Last fetch: none yet",
        f"Next fetch: {_next_fetch(settings, now)}",
        f"Cards today: {db.count_sent_today()}/{settings.daily_card_cap} · waiting for you: {db.count_waiting_cards()}",
    ]
    if _SOURCE_HEALTH:
        ok = sum(1 for label, n in _SOURCE_HEALTH.items() if n or label in _ZERO_IS_NORMAL)
        lines.append(f"Sources: {ok}/{len(_SOURCE_HEALTH)} OK")
    lines.append(f"Disk: {disk_free_gb(DATA_DIR):.0f} GB free")
    return "\n".join(lines)


async def run_heartbeat(settings, db: JobDb, notifier: Notifier, start_fetch,
                        now: datetime | None = None) -> None:
    """Every HEARTBEAT_EVERY, and once at start. Free: no model calls.

    A gap since the last beat means the laptop slept or was off: say so, and catch
    up on a missed fetch. Also tells Daniel when the charger is unplugged or back,
    and once when the battery runs low.
    """
    now = now or datetime.now(timezone.utc)
    last = db.get_meta("last_alive")
    db.set_meta("last_alive", now.isoformat())
    overdue = fetch_overdue(db, now)
    if last and now - datetime.fromisoformat(last) > OFFLINE_ALERT_AFTER:
        await notifier.telegram("\n".join(filter(None, (
            f"👋 Back online after {duration(now - datetime.fromisoformat(last))} "
            "(the laptop was asleep or off).",
            esc(power_line()),
            "Catching up on the missed fetch now." if overdue else "",
        ))))
    if overdue:
        start_fetch()

    on_ac, cap = power_state()
    state = {True: "ac", False: "battery"}.get(on_ac)
    if state is None:
        return
    previous = db.get_meta("power")
    db.set_meta("power", state)
    if previous and previous != state:
        await notifier.telegram(
            f"🔌 Charger connected ({cap}%)." if state == "ac" else
            f"🔋 Charger unplugged ({cap}%). On battery the laptop sleeps when idle or folded, "
            "and the agent with it.")
    if state == "ac":
        db.set_meta("low_battery_warned", "0")
    elif cap is not None and cap <= LOW_BATTERY and db.get_meta("low_battery_warned") != "1":
        db.set_meta("low_battery_warned", "1")
        await notifier.telegram(f"🪫 Battery at {cap}% and not charging: plug in to keep the agent running.")


def prune_outputs(db: JobDb, keep_days: int = OUTPUT_KEEP_DAYS) -> int:
    """Delete the CV folders of cards older than keep_days that were never applied to.

    Applied jobs keep theirs: the CV that was sent and the prep sheet matter later.
    """
    removed = 0
    for row in db.stale_outputs(keep_days):
        shutil.rmtree(OUT_DIR / str(int(row["id"])), ignore_errors=True)
        db.update_job(row["url"], pdf_path="")
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Button handlers
# ---------------------------------------------------------------------------

async def _record_application(settings, db: JobDb, tracker: SheetTracker, notifier: Notifier,
                              job: JobRecord, result: ScoreResult, channel: str) -> None:
    """Write the sheet row and tell Daniel on both channels."""
    today_str = date.today().strftime("%d/%m/%Y")
    number = job_number(job.url)
    notes = " ".join(n for n in (result.reason, _prep_notes(result)) if n)
    sheet_note = ""
    try:
        tracker.append_application(NewApplication(
            company=job.company,
            role=job.title,
            domain=result.variant,
            location_source=job.location,
            match_pct=result.score,
            cv_variant=result.variant,
            channel=channel,
            url=job.url,
            notes=notes,
            date=today_str,
            job_number=number,
        ))
    except Exception as exc:  # the application went out; losing the alert would be worse
        log.error("sheet write failed for %s: %s", job.url, exc)
        sheet_note = "SHEET WRITE FAILED, add this row by hand."
    db.update_job(job.url, status=STATUS_APPLIED)

    sheet_url = _sheet_link(settings)
    await notifier.event(
        url=job.url,
        company=job.company,
        role=job.title,
        tg_text="\n".join(filter(None, (
            f"Applied: <b>{esc(job.company)} — {esc(job.title)}</b> ({esc(channel)})",
            esc(sheet_note),
        ))),
        email_body="\n".join(filter(None, (
            f"Event: applied ({channel})",
            f"Company: {job.company}\nRole: {job.title}",
            f"Job number: {number}" if number else "",
            f"Date: {today_str}\nStatus: {STATUS_SUBMITTED}",
            f"Match: {result.score}%\nVariant: {result.variant}",
            f"Apply link: {job.url}\nSheet: {sheet_url}",
            sheet_note,
        ))),
    )
    _start_prep(settings, db, notifier, db.id_for(job.url))


# ---------------------------------------------------------------------------
# Interview prep
# ---------------------------------------------------------------------------

PREP_MAX_ATTEMPTS = 3
_prep_in_flight: set[str] = set()
_background: set[asyncio.Task] = set()


async def _send_prep(settings, db: JobDb, notifier: Notifier, job_id: int) -> bool:
    """Write the prep sheet for an applied job and send it on both channels.

    Safe to call twice: a job that's done or already being written is skipped. A
    failure leaves prep_path empty, so the next fetch cycle tries again, up to
    PREP_MAX_ATTEMPTS. Needs the API key: keyword mode has no model to write it.
    """
    row = db.get_by_id(job_id)
    if row is None or row["prep_path"] or not row["score_json"] or not settings.anthropic_api_key:
        return False
    url = row["url"]
    if url in _prep_in_flight:
        return False
    _prep_in_flight.add(url)
    try:
        job, result = _job_from_row(row), _score_from_row(row)
        attempt = db.note_prep_attempt(url)
        try:
            pdf = await asyncio.to_thread(
                write_prep, job, result, settings.anthropic_api_key, OUT_DIR / str(job_id))
        except Exception as exc:  # any failure means "retry later", whatever the cause
            log.error("prep sheet failed for %s (try %d): %s", url, attempt, exc)
            later = "will retry at the next fetch" if attempt < PREP_MAX_ATTEMPTS else "giving up"
            await notifier.telegram(
                f"Couldn't write the interview prep for <b>{esc(job.company)} — {esc(job.title)}</b> "
                f"(try {attempt}/{PREP_MAX_ATTEMPTS}, {later}): {esc(exc)}")
            return False
        db.update_job(url, prep_path=str(pdf))
        number = job_number(url)
        await notifier.event(
            url=url,
            company=job.company,
            role=job.title,
            tg_text=(f"Interview prep: <b>{esc(job.company)} — {esc(job.title)}</b>\n"
                     "Topics to review and likely questions."),
            email_body="\n".join(filter(None, (
                "Event: interview prep sheet",
                f"Company: {job.company}\nRole: {job.title}",
                f"Job number: {number}" if number else "",
                f"Date: {date.today():%d/%m/%Y}",
                "Topics to review and likely questions are attached.",
                f"Apply link: {url}\nSheet: {_sheet_link(settings)}",
            ))),
            attachment=str(pdf),
        )
        log.info("prep sheet sent: %s/%s", job.company, job.title)
        return True
    finally:
        _prep_in_flight.discard(url)


async def _prep_task(settings, db: JobDb, notifier: Notifier, job_id: int) -> None:
    try:
        await _send_prep(settings, db, notifier, job_id)
    except Exception:  # nobody awaits this task, so log here or the error is lost
        log.exception("prep task crashed for job %d", job_id)


def _start_prep(settings, db: JobDb, notifier: Notifier, job_id: int | None) -> None:
    """Write the prep sheet in the background and return at once.

    Called from button handlers. The bot handles taps one at a time, so awaiting
    a model call of a minute or so here would freeze every other button.
    """
    if job_id is None:
        return
    task = asyncio.create_task(_prep_task(settings, db, notifier, job_id))
    _background.add(task)  # the event loop keeps only a weak reference to tasks
    task.add_done_callback(_background.discard)


async def run_prep_retries(settings, db: JobDb, notifier: Notifier) -> None:
    """Retry prep sheets that failed, or were cut off by a restart."""
    for row in db.pending_prep(PREP_MAX_ATTEMPTS):
        await _send_prep(settings, db, notifier, row["id"])


def _register_handlers(bot: TelegramBot, settings, db: JobDb, tracker: SheetTracker,
                       notifier: Notifier) -> None:
    async def on_approve(job_id: int) -> None:
        row = db.get_by_id(job_id)
        if row is None:
            return
        job, result = _job_from_row(row), _score_from_row(row)
        if settings.dry_run:
            log.info("[dry-run] would approve %s/%s", job.company, job.title)
            await bot.send_message(
                f"[dry-run] Would approve <b>{esc(job.company)} — {esc(job.title)}</b>.")
            return
        if not db.claim(job_id, STATUS_SENT, STATUS_APPROVED):
            log.warning("approve ignored, job %d is %s", job_id, row["status"])
            return
        # Last check before anything goes out: the sheet may have changed since
        # the card was sent, for example a row Daniel added by hand.
        if tracker.is_duplicate(job.company, job.title, job_number(job.url), fresh=True):
            db.claim(job_id, STATUS_APPROVED, STATUS_SKIPPED)
            await bot.send_message(
                f"Already in the sheet, not applying again: "
                f"<b>{esc(job.company)} — {esc(job.title)}</b>")
            return
        log.info("approved: %s/%s", job.company, job.title)

        pdf = Path(row["pdf_path"]) if row["pdf_path"] else None
        if not row["recruiter_email"] or pdf is None or not pdf.exists():
            await bot.send_approved_portal(_card_from_row(row))
            return
        subject, body = cover_note(job)
        try:
            await asyncio.to_thread(
                apply_by_email, settings.gmail_address, settings.gmail_app_password,
                row["recruiter_email"], subject, body, pdf)
        except Exception as exc:  # fall back to applying by hand; the job stays approved
            log.error("application email failed for %s: %s", job.url, exc)
            await bot.send_message(
                f"Email to {esc(row['recruiter_email'])} failed, apply by hand instead.")
            await bot.send_approved_portal(_card_from_row(row))
            return
        await _record_application(settings, db, tracker, notifier, job, result, "email")

    async def on_skip(job_id: int) -> None:
        if db.claim(job_id, STATUS_SENT, STATUS_SKIPPED):
            log.info("skipped job %d", job_id)

    async def on_submitted(job_id: int) -> None:
        row = db.get_by_id(job_id)
        if row is None:
            return
        job, result = _job_from_row(row), _score_from_row(row)
        if settings.dry_run:
            log.info("[dry-run] would record %s/%s as submitted", job.company, job.title)
            return
        if not db.claim(job_id, STATUS_APPROVED, STATUS_APPLIED):
            log.warning("submitted ignored, job %d is %s", job_id, row["status"])
            return
        if tracker.is_duplicate(job.company, job.title, job_number(job.url), fresh=True):
            await bot.send_message(
                f"Already in the sheet, no second row added: "
                f"<b>{esc(job.company)} — {esc(job.title)}</b>")
            return
        channel = "LinkedIn" if job.source.startswith("linkedin") else "portal"
        await _record_application(settings, db, tracker, notifier, job, result, channel)

    async def on_status(proposal_id: int, status: str | None) -> None:
        """Daniel tapped a stage (or Ignore, status=None) under a reply."""
        proposal = db.get_reply_proposal(proposal_id)
        if proposal is None:
            return
        if status is None:
            db.claim_reply_proposal(proposal_id, REPLY_IGNORED)
            return
        if status not in VALID_STATUSES:
            log.warning("status tap ignored, unknown stage %r", status)
            return
        company, role = proposal["company"], proposal["role"]
        if settings.dry_run:
            log.info("[dry-run] would set %s/%s to %s", company, role, status)
            await bot.send_message(f"[dry-run] Would update {esc(company)} — {esc(role)}.")
            return
        if not db.claim_reply_proposal(proposal_id, REPLY_CONFIRMED):
            return
        row_num = tracker.find_row(company, role)
        if row_num is None:
            await notifier.telegram(f"No sheet row for {esc(company)} — {esc(role)}, not updated.")
            return
        tracker.update_status(row_num, status)
        sheet_row = _sheet_row(tracker, company, role)
        url = sheet_row.url if sheet_row else ""
        # A reply usually means an interview is coming: put the prep sheet back on top.
        prep = db.prep_path_for(url) if url and status in PREP_STATUSES else None
        await notifier.event(
            url=url,
            company=company,
            role=role,
            attachment=prep,
            tg_text=f"Status updated: <b>{esc(company)} — {esc(role)}</b>: {esc(status)}"
                    + ("\nInterview prep attached again." if prep else ""),
            email_body=(
                f"Event: status changed\nCompany: {company}\nRole: {role}\n"
                f"Status: {status}\nDate: {date.today().strftime('%d/%m/%Y')}\n"
                f"Sheet: {_sheet_link(settings)}"
            ),
        )

    def load_card(job_id: int) -> JobCard | None:
        row = db.get_by_id(job_id)
        return _card_from_row(row) if row is not None and row["score_json"] else None

    bot.on_approve(on_approve)
    bot.on_skip(on_skip)
    bot.on_submitted(on_submitted)
    bot.on_status(on_status)
    bot.set_card_loader(load_card)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram's URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    DATA_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(exist_ok=True)

    db = JobDb(DATA_DIR / "jobs.db")
    tracker = SheetTracker(settings.tracker_sheet_id, sa_file=settings.google_service_account_file)
    tracker.ensure_agent_headers()
    tracker.ensure_status_options()
    bot = TelegramBot(settings.telegram_bot_token, settings.telegram_chat_id)
    notifier = Notifier(settings, db, bot)
    _register_handlers(bot, settings, db, tracker, notifier)

    scheduler = AsyncIOScheduler(
        timezone="Asia/Jerusalem", job_defaults={"misfire_grace_time": 3600})

    async def fetch_task() -> None:
        db.set_meta("last_fetch_at", datetime.now(timezone.utc).isoformat())
        await notifier.flush()
        try:
            await run_prep_retries(settings, db, notifier)
            await run_fetch_cycle(settings, db, tracker, bot)
            await run_reply_scan(settings, db, tracker, notifier)
        except Exception as exc:  # unattended: a failed cycle must reach Daniel, not just a log
            log.exception("fetch cycle failed")
            await notifier.telegram(f"Fetch cycle failed: {esc(exc)}")

    async def bump_task() -> None:
        try:
            await run_bumps(settings, db, tracker, bot)
        except Exception:
            log.exception("bump run failed")

    def start_fetch() -> None:
        scheduler.add_job(fetch_task)  # no trigger: runs once, right away

    async def heartbeat_task() -> None:
        try:
            await run_heartbeat(settings, db, notifier, start_fetch)
        except Exception:
            log.exception("heartbeat failed")

    async def status_reply() -> str:
        return status_text(settings, db)

    bot.on_command("status", "Is the agent running?", status_reply)

    async def digest_task() -> None:
        await run_daily_digest(settings, db, tracker, notifier)

    for time_str in settings.fetch_times.split(","):
        h, m = time_str.strip().split(":")
        scheduler.add_job(fetch_task, CronTrigger(hour=int(h), minute=int(m)))

    h, m = settings.digest_time.strip().split(":")
    scheduler.add_job(digest_task, CronTrigger(hour=int(h), minute=int(m)))

    scheduler.add_job(bump_task, CronTrigger(minute=30))  # hourly, clear of the :00 fetches

    scheduler.add_job(heartbeat_task, "interval", minutes=int(HEARTBEAT_EVERY.total_seconds() // 60))
    # Once at start too: says "back online" after a reboot or a long stop, and fetches
    # right away if a slot was missed (or on the very first start).
    scheduler.add_job(heartbeat_task)

    async def _run() -> None:
        scheduler.start()
        log.info(
            "scheduler started — fetch at %s, digest at %s (Asia/Jerusalem) "
            "[use_llm=%s, dry_run=%s]",
            settings.fetch_times, settings.digest_time,
            bool(settings.anthropic_api_key), settings.dry_run,
        )
        await bot.start_polling(drop_pending_updates=False)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        await stop_event.wait()
        log.info("shutting down…")
        scheduler.shutdown(wait=False)
        await bot.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
