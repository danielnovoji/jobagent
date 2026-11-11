"""
Comeet careers pages (common in Israel), one config line per company.

A company's hosted page (comeet.com/jobs/<slug>/<uid>) carries the public token
Comeet's own careers widgets use. With it, one call to the careers API returns
every position with its description. The token is read from the page on every
fetch, so a rotated token doesn't break the source.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_PAGE_URL = "https://www.comeet.com/jobs/{slug}/{uid}"
_API = "https://www.comeet.co/careers-api/2.0/company/{uid}/positions"
_TOKEN_RE = re.compile(r'"?token"?\s*[:=]\s*"([A-Za-z0-9]{8,})"')
_COUNTRIES = {"IL": "Israel"}


@dataclass(frozen=True)
class Board:
    company: str
    slug: str
    uid: str


BOARDS: list[Board] = [
    Board("Cardo", "cardosystems", "F2.003"),  # Daniel, 2026-11-11
]


def fetch(client: HttpClient) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    for board in BOARDS:
        try:
            page = client.get(_PAGE_URL.format(slug=board.slug, uid=board.uid)).text
            m = _TOKEN_RE.search(page)
            if not m:
                raise ValueError("no careers token on the page")
            positions = client.get(_API.format(uid=board.uid),
                                   params={"token": m.group(1), "details": "true"}).json()
        except Exception as exc:  # one broken board mustn't hide the others
            log.error("comeet/%s: %s", board.slug, exc)
            continue
        jobs.extend(j for j in (parse_position(board, p) for p in positions or []) if j)
    return jobs


def parse_position(board: Board, item: dict) -> JobRecord | None:
    title = (item.get("name") or "").strip()
    url = item.get("url_comeet_hosted_page") or ""
    if not title or not url:
        return None
    return JobRecord(
        source=f"comeet:{board.slug}",
        company=board.company,
        title=title,
        location=_location(item.get("location") or {}),
        url=url,  # ends in the position uid ("E8.37B"), which job_number() reads
        description=_description(item.get("details") or []),
        posted_date=(item.get("time_updated") or "")[:10],
    )


def _location(loc: dict) -> str:
    """"Ra'anana, Israel". Comeet gives the country as a code, which the filter can't read."""
    city = (loc.get("city") or loc.get("name") or "").strip()
    code = loc.get("country") or ""
    country = _COUNTRIES.get(code, code)
    return f"{city}, {country}" if city and country and city != country else city or country


def _description(details: list[dict]) -> str:
    parts = []
    for section in details:
        text = BeautifulSoup(section.get("value") or "", "html.parser").get_text("\n")
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        if text:
            parts.append(f"{section.get('name') or ''}\n{text}".strip())
    return "\n\n".join(parts)
