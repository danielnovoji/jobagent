"""
Amazon jobs in Israel (Amazon, AWS, Annapurna Labs), from amazon.jobs/en/search.json.

Public JSON with the description and qualifications in the search results, so
no per-job call is needed.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterator

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
# Redirects to the slugged page; ending in the id lets job_number() read it.
_JOB_URL = "https://www.amazon.jobs/en/jobs/{job_id}"
_PAGE_SIZE = 100
_MAX_PAGES = 20  # a guard against looping if the API stops honouring offset
_SOURCE = "amazon"
_SECTIONS = (("description", ""), ("basic_qualifications", "Basic Qualifications"),
             ("preferred_qualifications", "Preferred Qualifications"))


def fetch(client: HttpClient) -> list[JobRecord]:
    return list(_paginate(client))


def _paginate(client: HttpClient) -> Iterator[JobRecord]:
    for page in range(_MAX_PAGES):
        data = client.get(_SEARCH_URL, params={
            "loc_query": "Israel", "country": "ISR",
            "result_limit": _PAGE_SIZE, "offset": page * _PAGE_SIZE,
        }).json()
        jobs = data.get("jobs") or []
        for item in jobs:
            job = parse_job(item)
            if job:
                yield job
        if not jobs or (page + 1) * _PAGE_SIZE >= int(data.get("hits") or 0):
            return
    log.warning("amazon: stopped after %d pages", _MAX_PAGES)


def parse_job(item: dict) -> JobRecord | None:
    title = (item.get("title") or "").strip()
    job_id = item.get("id_icims")
    if not title or not job_id:
        return None
    return JobRecord(
        source=_SOURCE,
        company=_company(item.get("company_name") or ""),
        title=title,
        location=(item.get("location") or item.get("normalized_location") or "").strip(),
        url=_JOB_URL.format(job_id=job_id),
        description=_description(item),
        posted_date=_date(item.get("posted_date") or ""),
    )


def _company(legal_name: str) -> str:
    """Annapurna Labs hires separately from the rest of Amazon, so it keeps its name."""
    return "Annapurna Labs" if "annapurna" in legal_name.lower() else "Amazon"


def _description(item: dict) -> str:
    parts = []
    for key, heading in _SECTIONS:
        text = BeautifulSoup(item.get(key) or "", "html.parser").get_text("\n")
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        if text:
            parts.append(f"{heading}\n{text}" if heading else text)
    return "\n\n".join(parts)


def _date(raw: str) -> str:
    """"November 10, 2026" to "2026-11-10", "" if it doesn't parse."""
    try:
        return datetime.strptime(raw.strip(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""
