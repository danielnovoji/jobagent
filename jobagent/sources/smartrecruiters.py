"""
SmartRecruiters (public postings API, no login), one config line per company.

The list call takes a country filter, so only Israeli postings come back. The
description comes from a per-posting call, fetched only for jobs that pass the
free filters. Company ids were found by probing api.smartrecruiters.com (2026-11-11).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_API = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
# Redirects to the slugged page; ending in the posting id lets job_number() read it.
_JOB_URL = "https://jobs.smartrecruiters.com/{company_id}/{posting_id}"
_PAGE_SIZE = 100  # the API's maximum
_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


@dataclass(frozen=True)
class Company:
    name: str
    company_id: str


COMPANIES: list[Company] = [
    Company("Check Point", "CheckPointSoftwareTechnologies2"),
]


def fetch(client: HttpClient) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    for company in COMPANIES:
        try:
            jobs.extend(_fetch_company(client, company))
        except Exception as exc:  # one broken company mustn't hide the others
            log.error("smartrecruiters/%s: %s", company.company_id, exc)
    return jobs


def _fetch_company(client: HttpClient, company: Company) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    offset = 0
    while True:
        data = client.get(_API.format(company_id=company.company_id),
                          params={"country": "il", "limit": _PAGE_SIZE, "offset": offset}).json()
        items = data.get("content") or []
        jobs.extend(j for j in (parse_posting(company, item) for item in items) if j)
        offset += len(items)
        if not items or offset >= int(data.get("totalFound") or 0):
            return jobs


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the full posting text, or "" on any error."""
    try:
        company_id, posting_id = urlparse(job.url).path.strip("/").split("/")[:2]
        data = client.get(f"{_API.format(company_id=company_id)}/{posting_id}").json()
        return parse_job_ad(data)
    except Exception as exc:
        log.warning("smartrecruiters: description failed for %s: %s", job.url, exc)
        return ""


def parse_posting(company: Company, item: dict) -> JobRecord | None:
    title = (item.get("name") or "").strip()
    posting_id = item.get("id")
    if not title or not posting_id:
        return None
    loc = item.get("location") or {}
    location = loc.get("fullLocation") or ", ".join(
        p for p in (loc.get("city"), loc.get("country")) if p)
    return JobRecord(
        source=f"smartrecruiters:{company.company_id}",
        company=company.name,
        title=title,
        location=location,
        url=_JOB_URL.format(company_id=company.company_id, posting_id=posting_id),
        posted_date=(item.get("releasedDate") or "")[:10],
    )


def parse_job_ad(data: dict) -> str:
    sections = ((data.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in _SECTIONS:
        section = sections.get(key) or {}
        text = BeautifulSoup(section.get("text") or "", "html.parser").get_text("\n")
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        if text:
            parts.append(f"{section.get('title') or ''}\n{text}".strip())
    return "\n\n".join(parts)
