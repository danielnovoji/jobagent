"""
Apple jobs in Israel, from jobs.apple.com.

The search page is server-rendered: results sit in the page as JSON
(`window.__staticRouterHydrationData = JSON.parse("...")`), 20 per page. That
works from a server IP, where the old /api/v1/search endpoint returned nothing.
The search result has only a short summary; the full posting comes from the
detail page, which carries the same kind of JSON.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

log = logging.getLogger(__name__)

_BASE_URL = "https://jobs.apple.com"
_SEARCH_URL = f"{_BASE_URL}/en-il/search"
# Redirects to the slugged page. Keeping the id last lets job_number() read it.
_JOB_URL = f"{_BASE_URL}/en-il/details/{{role_id}}"
_PAGE_SIZE = 20
_MAX_PAGES = 30  # a guard against looping if the site stops honouring ?page=
_COMPANY = "Apple"
_SOURCE = "apple"

# A JS string literal, escapes included, so a '");' inside the data can't end the match.
_HYDRATION_RE = re.compile(r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)')

_DETAIL_SECTIONS = (
    ("jobSummary", ""),
    ("description", "Description"),
    ("responsibilities", "Responsibilities"),
    ("minimumQualifications", "Minimum Qualifications"),
    ("preferredQualifications", "Preferred Qualifications"),
)


def fetch(client: HttpClient) -> list[JobRecord]:
    return list(_paginate(client))


def _paginate(client: HttpClient) -> Iterator[JobRecord]:
    for page in range(1, _MAX_PAGES + 1):
        html = client.get(_SEARCH_URL, params={"location": "israel-ISR", "page": page}).text
        search = loader_data(html)["search"]
        results = search.get("searchResults") or []
        for item in results:
            job = parse_result(item)
            if job:
                yield job
        if not results or page * _PAGE_SIZE >= int(search.get("totalRecords") or 0):
            return
    log.warning("apple: stopped after %d pages", _MAX_PAGES)


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the full posting text, or "" on any error."""
    try:
        return parse_detail(client.get(job.url).text)
    except Exception as exc:
        log.warning("apple: description failed for %s: %s", job.url, exc)
        return ""


def loader_data(html: str) -> dict:
    """The page's embedded JSON. Raises ValueError if the page has none."""
    m = _HYDRATION_RE.search(html)
    if not m:
        raise ValueError("apple: no hydration data in page")
    # The captured group is a JS string literal whose content is JSON: decode twice.
    return json.loads(json.loads(m.group(1)))["loaderData"]


def parse_result(item: dict) -> JobRecord | None:
    title = (item.get("postingTitle") or "").strip()
    role_id = item.get("id") or item.get("reqId") or ""
    if not title or not role_id:
        return None
    return JobRecord(
        source=_SOURCE,
        company=_COMPANY,
        title=title,
        location=_location(item.get("locations") or []),
        url=_JOB_URL.format(role_id=role_id),
        description="",  # only a summary here; fetch_description gets the posting
        posted_date=(item.get("postDateInGMT") or "")[:10],
    )


def parse_detail(html: str) -> str:
    data = loader_data(html)["jobDetails"]["jobsData"]
    parts = []
    for key, heading in _DETAIL_SECTIONS:
        text = _plain(data.get(key) or "")
        if text:
            parts.append(f"{heading}\n{text}" if heading else text)
    return "\n\n".join(parts)


def _location(locations: list[dict]) -> str:
    """"Herzliya, Israel"; several places joined with "; "."""
    places = []
    for loc in locations:
        name, country = (loc.get("name") or "").strip(), (loc.get("countryName") or "").strip()
        place = f"{name}, {country}" if name and country and name != country else name or country
        if place and place not in places:
            places.append(place)
    return "; ".join(places)


def _plain(text: str) -> str:
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n", text).strip()
