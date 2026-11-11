import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from jobagent.filters import is_israel
from jobagent.models import JobRecord, company_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT    NOT NULL,
    company           TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    location          TEXT    NOT NULL,
    url               TEXT    NOT NULL,
    normalized_key    TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    posted_date       TEXT    NOT NULL DEFAULT '',
    fetched_at        TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'new',
    score             INTEGER,
    variant           TEXT    NOT NULL DEFAULT '',
    thread_message_id TEXT,
    score_json        TEXT,
    scored_by         TEXT,
    sent_at           TEXT,
    pdf_path          TEXT,
    recruiter_email   TEXT    NOT NULL DEFAULT '',
    prep_path         TEXT,
    prep_attempts     INTEGER NOT NULL DEFAULT 0,
    card_message_id   TEXT,
    bumps             INTEGER NOT NULL DEFAULT 0,
    last_bump_at      TEXT,
    UNIQUE(url),
    UNIQUE(normalized_key)
);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT    NOT NULL UNIQUE,
    company         TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    proposed_status TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    created_at      TEXT    NOT NULL
);
"""

# Columns added after the first release. Older databases get them on open.
_MIGRATIONS = (
    "ALTER TABLE jobs ADD COLUMN thread_message_id TEXT",
    "ALTER TABLE jobs ADD COLUMN score_json TEXT",
    "ALTER TABLE jobs ADD COLUMN scored_by TEXT",
    "ALTER TABLE jobs ADD COLUMN sent_at TEXT",
    "ALTER TABLE jobs ADD COLUMN pdf_path TEXT",
    "ALTER TABLE jobs ADD COLUMN recruiter_email TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN prep_path TEXT",
    "ALTER TABLE jobs ADD COLUMN prep_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN card_message_id TEXT",
    "ALTER TABLE jobs ADD COLUMN bumps INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN last_bump_at TEXT",
)

# Valid status values used by the pipeline
STATUS_NEW          = "new"           # queued; retried until scored or aged out
STATUS_FILTERED_OUT = "filtered_out"
STATUS_SCORE_FAILED = "score_failed"  # model gave no usable answer; not retried
STATUS_SCORED       = "scored"        # above threshold and unsent = waiting for a card slot
STATUS_SENT         = "sent"          # card on Telegram, waiting for a tap
STATUS_APPROVED     = "approved"      # portal job: Approve tapped, waiting for Submitted
STATUS_SKIPPED      = "skipped"
STATUS_APPLIED      = "applied"

REPLY_PENDING   = "pending"
REPLY_CONFIRMED = "confirmed"
REPLY_IGNORED   = "ignored"
REPLY_INFO      = "info"       # same status as the sheet already has: told Daniel, nothing to ask


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobDb:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        # The same matching in SQL as in Python, for the per-company card count.
        self._conn.create_function("company_key", 1, company_key, deterministic=True)
        self._conn.executescript(_SCHEMA)
        for statement in _MIGRATIONS:
            try:
                self._conn.execute(statement)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert_if_new(self, job: JobRecord) -> bool:
        """Insert job; return True if inserted, False if duplicate."""
        key = _make_key(job.company, job.title, job.location)
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """INSERT INTO jobs
                   (source, company, title, location, url, normalized_key,
                    description, posted_date, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.source, job.company, job.title, job.location, job.url,
                 key, job.description, job.posted_date, now),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_job(
        self,
        url: str,
        *,
        status: Optional[str] = None,
        score: Optional[int] = None,
        variant: Optional[str] = None,
        description: Optional[str] = None,
        thread_message_id: Optional[str] = None,
        score_json: Optional[str] = None,
        scored_by: Optional[str] = None,
        sent_at: Optional[str] = None,
        pdf_path: Optional[str] = None,
        recruiter_email: Optional[str] = None,
        prep_path: Optional[str] = None,
        card_message_id: Optional[str] = None,
    ) -> None:
        updates = {
            "status": status, "score": score, "variant": variant,
            "description": description, "thread_message_id": thread_message_id,
            "score_json": score_json, "scored_by": scored_by, "sent_at": sent_at,
            "pdf_path": pdf_path, "recruiter_email": recruiter_email,
            "prep_path": prep_path, "card_message_id": card_message_id,
        }
        fields = {k: v for k, v in updates.items() if v is not None}
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._conn.execute(
            f"UPDATE jobs SET {assignments} WHERE url = ?", [*fields.values(), url]
        )
        self._conn.commit()

    def claim(self, job_id: int, from_status: str, to_status: str) -> bool:
        """Move a job between statuses; False if it wasn't in from_status.

        This is what makes Approve/Submitted safe to tap twice: the check and the
        update are one statement, so only the first tap gets True.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ? AND status = ?",
            (to_status, job_id, from_status),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def due_bumps(self, before: datetime, max_bumps: int) -> list[sqlite3.Row]:
        """Cards still waiting for a tap whose last card or bump went out before `before`.

        Only cards with a known Telegram message: older cards (and test ones) are never bumped.
        """
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND card_message_id IS NOT NULL AND bumps < ? "
            "AND COALESCE(last_bump_at, sent_at) < ?",
            (STATUS_SENT, max_bumps, before.isoformat()),
        ).fetchall()

    def note_bump(self, url: str, at: datetime) -> None:
        self._conn.execute("UPDATE jobs SET bumps = bumps + 1, last_bump_at = ? WHERE url = ?",
                           (at.isoformat(), url))
        self._conn.commit()

    def count_waiting_cards(self) -> int:
        """Cards on Telegram still waiting for Approve or Skip."""
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (STATUS_SENT,)).fetchone()[0]

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def stale_outputs(self, days: int) -> list[sqlite3.Row]:
        """Jobs with a rendered CV whose card is older than `days` and was never applied to."""
        cutoff = (_utcnow() - timedelta(days=days)).isoformat()
        return self._conn.execute(
            "SELECT id, url FROM jobs WHERE status != ? AND pdf_path IS NOT NULL AND pdf_path != '' "
            "AND sent_at < ?", (STATUS_APPLIED, cutoff),
        ).fetchall()

    def note_prep_attempt(self, url: str) -> int:
        """Count one try at the interview prep sheet. Returns the new count."""
        self._conn.execute(
            "UPDATE jobs SET prep_attempts = prep_attempts + 1 WHERE url = ?", (url,))
        self._conn.commit()
        row = self._conn.execute("SELECT prep_attempts FROM jobs WHERE url = ?", (url,)).fetchone()
        return row["prep_attempts"] if row else 0

    def pending_prep(self, max_attempts: int) -> list[sqlite3.Row]:
        """Applied jobs whose prep sheet hasn't been sent yet and still has tries left."""
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND prep_path IS NULL AND prep_attempts < ?",
            (STATUS_APPLIED, max_attempts),
        ).fetchall()

    def prep_path_for(self, url: str) -> Optional[str]:
        row = self._conn.execute("SELECT prep_path FROM jobs WHERE url = ?", (url,)).fetchone()
        return row["prep_path"] if row else None

    def requeue_not_scored_by(self, scorer: str) -> int:
        """Send unsent jobs scored by another scorer back through scoring.

        Means adding ANTHROPIC_API_KEY later re-judges jobs the keyword scorer saw.
        fetched_at is reset so they fall inside the retry window.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, score = NULL, fetched_at = ? "
            "WHERE status = ? AND COALESCE(scored_by, '') != ?",
            (STATUS_NEW, _utcnow().isoformat(), STATUS_SCORED, scorer),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_status(self, status: str) -> list[JobRecord]:
        cur = self._conn.execute(
            "SELECT source, company, title, location, url, description, posted_date "
            "FROM jobs WHERE status = ?",
            (status,),
        )
        return [JobRecord(**dict(row)) for row in cur.fetchall()]

    def exists(self, url: str) -> bool:
        return bool(
            self._conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone()
        )

    def known_urls(self) -> set[str]:
        return {r["url"] for r in self._conn.execute("SELECT url FROM jobs")}

    def get_by_id(self, job_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def id_for(self, url: str) -> Optional[int]:
        row = self._conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
        return row["id"] if row else None

    def retryable_new(self, max_age_days: int = 3) -> list[JobRecord]:
        """Jobs still waiting for scoring, oldest first.

        The age limit stops a job whose description never loads retrying forever.
        """
        cutoff = (_utcnow() - timedelta(days=max_age_days)).isoformat()
        cur = self._conn.execute(
            "SELECT source, company, title, location, url, description, posted_date "
            "FROM jobs WHERE status = ? AND fetched_at >= ? ORDER BY id",
            (STATUS_NEW, cutoff),
        )
        return [JobRecord(**dict(row)) for row in cur.fetchall()]

    def pending_cards(self, threshold: int, max_age_days: int = 14) -> list[sqlite3.Row]:
        """Good jobs with no card yet, best first.

        Includes jobs that lost out to the daily cap on an earlier cycle.
        """
        cutoff = (_utcnow() - timedelta(days=max_age_days)).isoformat()
        return self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND score >= ? AND fetched_at >= ? "
            "ORDER BY score DESC, id",
            (STATUS_SCORED, threshold, cutoff),
        ).fetchall()

    def count_sent_today(self) -> int:
        today = _utcnow().date().isoformat()
        return self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE sent_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]

    def count_open_cards(self, company: str, days: int = 30) -> int:
        """Cards for this company still awaiting a decision.

        Applied jobs are counted from the sheet; skipped ones free their slot.
        """
        cutoff = (_utcnow() - timedelta(days=days)).isoformat()
        return self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_key(company) = company_key(?) "
            "AND status IN (?, ?) AND sent_at >= ?",
            (company, STATUS_SENT, STATUS_APPROVED, cutoff),
        ).fetchone()[0]

    def get_thread_message_id(self, url: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT thread_message_id FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        return row["thread_message_id"] if row else None

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def count_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        return self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE fetched_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Outbox: notifications waiting for a retry
    # ------------------------------------------------------------------

    def outbox_add(self, channel: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO outbox (channel, payload, created_at) VALUES (?, ?, ?)",
            (channel, json.dumps(payload, ensure_ascii=False), _utcnow().isoformat()),
        )
        self._conn.commit()

    def outbox_items(self) -> list[tuple[int, str, dict]]:
        cur = self._conn.execute("SELECT id, channel, payload FROM outbox ORDER BY id")
        return [(r["id"], r["channel"], json.loads(r["payload"])) for r in cur.fetchall()]

    def outbox_done(self, item_id: int) -> None:
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (item_id,))
        self._conn.commit()

    def outbox_failed(self, item_id: int) -> None:
        self._conn.execute(
            "UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (item_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Recruiter-reply proposals
    # ------------------------------------------------------------------

    def seen_reply_ids(self) -> set[str]:
        cur = self._conn.execute("SELECT message_id FROM reply_proposals")
        return {r["message_id"] for r in cur.fetchall()}

    def add_reply_proposal(
        self, message_id: str, company: str, role: str, proposed_status: str
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO reply_proposals "
            "(message_id, company, role, proposed_status, created_at) VALUES (?, ?, ?, ?, ?)",
            (message_id, company, role, proposed_status, _utcnow().isoformat()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_reply_proposal(self, proposal_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM reply_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()

    def claim_reply_proposal(self, proposal_id: int, state: str) -> bool:
        """Resolve a pending proposal; False if it was already resolved."""
        cur = self._conn.execute(
            "UPDATE reply_proposals SET state = ? WHERE id = ? AND state = ?",
            (state, proposal_id, REPLY_PENDING),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _norm(s: str) -> str:
    return s.strip().lower()


def _normalize_title(title: str) -> str:
    return re.sub(r'\s+', ' ', _norm(title))


def _make_key(company: str, title: str, location: str) -> str:
    # Sources spell Israeli locations differently ("Israel, Haifa" vs Jobify's
    # Hebrew city). Collapsing them is what catches a job listed on both.
    place = "israel" if is_israel(location) else _norm(location)
    return f"{company_key(company)}|{_normalize_title(title)}|{place}"
