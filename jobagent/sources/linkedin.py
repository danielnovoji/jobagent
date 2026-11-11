"""Fetch descriptions for jobs found in LinkedIn alert emails.

Only reads the public guest job page for a URL LinkedIn already emailed us.
No search scraping and no login (see CLAUDE.md). The rate limit lives in
http_client.HOST_DELAYS.
"""
from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord
from jobagent.sources.jsonld import clean_html, job_description, page_text


def parse_description(page: str) -> str:
    desc = job_description(page)
    if desc:
        return desc
    # Guest pages don't always carry JSON-LD; fall back to the visible block.
    el = BeautifulSoup(page, "html.parser").select_one(".show-more-less-html__markup")
    return clean_html(el.get_text(" ", strip=True)) if el else ""


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the description, or "" on any error (the job is retried next cycle)."""
    try:
        return parse_description(page_text(client.get(job.url)))
    except Exception:
        return ""
