"""
Greenhouse job boards (public JSON API, no login), one config line per company.

The list call returns every job worldwide with its location; the free location
filter drops the non-Israel ones before anything else happens. The description
comes from a per-job call, so it's fetched only for jobs that pass the filters.
Board tokens were found by probing boards-api.greenhouse.io (2026-11-11).
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
# The hosted page works for every board and ends in the job id, which job_number()
# reads. Company careers URLs (absolute_url) vary and often hide the id in a query.
_JOB_URL = "https://job-boards.greenhouse.io/{token}/jobs/{job_id}"


@dataclass(frozen=True)
class Board:
    company: str
    token: str


BOARDS: list[Board] = [
    Board("JFrog", "jfrog"),
    Board("SentinelOne", "sentinellabs"),
    Board("Wiz", "wizinc"),
    Board("Cato Networks", "catonetworks"),
    Board("Taboola", "taboola"),
    Board("Payoneer", "payoneer"),
    Board("Riskified", "riskified"),
    Board("Similarweb", "similarweb"),
]


def fetch(client: HttpClient) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    for board in BOARDS:
        try:
            data = client.get(_API.format(token=board.token)).json()
        except Exception as exc:  # one broken board mustn't hide the others
            log.error("greenhouse/%s: %s", board.token, exc)
            continue
        jobs.extend(j for j in (parse_job(board, item) for item in data.get("jobs") or []) if j)
    return jobs


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the full posting text, or "" on any error."""
    try:
        token, _, job_id = urlparse(job.url).path.strip("/").split("/")[:3]  # {token}/jobs/{id}
        data = client.get(f"{_API.format(token=token)}/{job_id}").json()
        return parse_content(data.get("content") or "")
    except Exception as exc:
        log.warning("greenhouse: description failed for %s: %s", job.url, exc)
        return ""


def parse_job(board: Board, item: dict) -> JobRecord | None:
    title = (item.get("title") or "").strip()
    job_id = item.get("id")
    if not title or not job_id:
        return None
    return JobRecord(
        source=f"greenhouse:{board.token}",
        company=board.company,
        title=title,
        location=((item.get("location") or {}).get("name") or "").strip(),
        url=_JOB_URL.format(token=board.token, job_id=job_id),
        posted_date=(item.get("first_published") or item.get("updated_at") or "")[:10],
    )


def parse_content(content: str) -> str:
    """Greenhouse sends the HTML entity-escaped ("&lt;p&gt;"): unescape, then strip tags."""
    text = BeautifulSoup(html.unescape(content), "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()
