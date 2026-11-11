"""
Meta careers scraper — metacareers.com GraphQL API.

Meta uses persisted Relay queries identified by a `doc_id` that changes
with each frontend deployment. The current value was extracted from the
locale JS bundle (module CareersJobSearchResultsDataQuery_candidate_portalRelayOperation).

If the scraper returns empty results, open https://www.metacareers.com/jobs/ in
a browser, filter for Israel, then capture the POST to /api/graphql in DevTools
Network → look for the doc_id and update META_DOC_ID below.
"""
import json
import re
from typing import Iterator

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord

_PAGE_URL = "https://www.metacareers.com/jobs/?q=&location%5B0%5D=Israel"
_GRAPHQL_URL = "https://www.metacareers.com/api/graphql"
# Extracted 2026-11 from CareersJobSearchResultsDataQuery_candidate_portalRelayOperation
META_DOC_ID = "27506805582236862"
_PAGE_SIZE = 20
_COMPANY = "Meta"
_SOURCE = "meta"
_JOB_URL = "https://www.metacareers.com/jobs/{job_id}/"


def fetch(client: HttpClient) -> list[JobRecord]:
    return list(_paginate(client))


def _paginate(client: HttpClient) -> Iterator[JobRecord]:
    # Seed session cookies and grab the lsd CSRF token from the page HTML
    page_resp = client.get(_PAGE_URL)
    lsd_match = re.search(r'"lsd"\s*,\s*"([^"]+)"', page_resp.text)
    lsd = lsd_match.group(1) if lsd_match else ""

    page = 1
    while True:
        variables = {
            "search_input": {
                "q": "",
                "locations": ["Israel"],
                "teams": [],
                "is_leadership": False,
                "is_remote_only": False,
                "sort_by_new": False,
                "page": page,
                "results_per_page": _PAGE_SIZE,
            }
        }
        resp = client.post(
            _GRAPHQL_URL,
            data={
                "doc_id": META_DOC_ID,
                "variables": json.dumps(variables),
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery",
                "server_timestamps": "true",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "x-fb-friendly-name": "CareersJobSearchResultsDataQuery",
                "x-fb-lsd": lsd,
                "Origin": "https://www.metacareers.com",
                "Referer": _PAGE_URL,
            },
        ).json()
        results = (
            resp.get("data", {})
            .get("job_search", {})
            .get("results") or []
        )
        if not results:
            break
        for item in results:
            job = parse_result(item)
            if job:
                yield job
        if len(results) < _PAGE_SIZE:
            break
        page += 1


def parse_result(item: dict) -> JobRecord | None:
    title = item.get("title", "").strip()
    job_id = item.get("id") or ""
    if not title or not job_id:
        return None
    location = item.get("location") or item.get("location_info", {}).get("city", "")
    apply_link = item.get("apply_link") or _JOB_URL.format(job_id=job_id)
    return JobRecord(
        source=_SOURCE,
        company=_COMPANY,
        title=title,
        location=location,
        url=apply_link,
    )
