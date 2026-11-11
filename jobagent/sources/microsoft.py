"""
Careers sites run on Eightfold ("pcsx" API): Microsoft and Qualcomm, Israel only.

<host>/api/pcsx/search answers without a login (Microsoft's older apply/v2
endpoint needed a browser session). 10 results per page; the description needs
a second call per job (position_details). Qualcomm found on the same API, 2026-11-11.

The job page URL only works with Eightfold's internal id, but recruiters and the
posting itself use Microsoft's job number (displayJobId). The URL carries both:
the internal id in the path for the link, the public number as ?job_number= for
the sheet and dedupe (see models.job_number).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_PAGE_SIZE = 10
_MAX_PAGES = 30  # a guard against looping if the API stops honouring start=


@dataclass(frozen=True)
class Site:
    company: str
    host: str
    domain: str   # the API's tenant parameter
    source: str

    def api(self, path: str) -> str:
        return f"https://{self.host}/api/pcsx/{path}"

    def job_url(self, position_id) -> str:
        return f"https://{self.host}/careers/job/{position_id}"


SITES: list[Site] = [
    Site("Microsoft", "apply.careers.microsoft.com", "microsoft.com", "microsoft"),
    Site("Qualcomm", "careers.qualcomm.com", "qualcomm.com", "qualcomm"),
]


def fetch(client: HttpClient) -> list[JobRecord]:
    jobs: list[JobRecord] = []
    for site in SITES:
        try:
            jobs.extend(_paginate(client, site))
        except Exception as exc:  # one broken site mustn't hide the others
            log.error("eightfold/%s: %s", site.source, exc)
    return jobs


def _paginate(client: HttpClient, site: Site) -> Iterator[JobRecord]:
    for page in range(_MAX_PAGES):
        data = client.get(site.api("search"), params={
            "domain": site.domain, "hl": "en", "query": "", "location": "Israel",
            "start": page * _PAGE_SIZE,
        }).json()["data"]
        positions = data.get("positions") or []
        for item in positions:
            job = parse_position(item, site)
            if job:
                yield job
        if not positions or (page + 1) * _PAGE_SIZE >= int(data.get("count") or 0):
            return
    log.warning("eightfold/%s: stopped after %d pages", site.source, _MAX_PAGES)


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the full posting text, or "" on any error."""
    try:
        parsed = urlparse(job.url)
        site = next(s for s in SITES if s.host == parsed.netloc)
        position_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        data = client.get(site.api("position_details"), params={
            "domain": site.domain, "hl": "en", "position_id": position_id}).json()["data"]
        return parse_description(data)
    except Exception as exc:
        log.warning("microsoft: description failed for %s: %s", job.url, exc)
        return ""


def parse_position(item: dict, site: Site = SITES[0]) -> JobRecord | None:
    title = (item.get("name") or "").strip()
    position_id = item.get("id")
    if not title or not position_id:
        return None
    url = site.job_url(position_id)
    if item.get("displayJobId"):
        url += f"?job_number={item['displayJobId']}"
    return JobRecord(
        source=site.source,
        company=site.company,
        title=title,
        # "Israel, Tel Aviv, Herzliya". Not standardizedLocations: that says "IL",
        # which the location filter rightly rejects (it's also Illinois).
        location="; ".join(item.get("locations") or []),
        url=url,
        posted_date=_date(item.get("postedTs")),
    )


def parse_description(data: dict) -> str:
    html = data.get("jobDescription") or ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _date(ts) -> str:
    """Unix seconds to an ISO date, "" if missing."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""
