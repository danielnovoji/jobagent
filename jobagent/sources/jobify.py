"""
Jobify (jobify360.co.il) scraper — category page listings.

Jobify is an Israeli job aggregator that organises roles by title slug.
URL pattern: https://jobify360.co.il/jobs/{slug}
Returns up to 10 jobs per page; no pagination parameter exposed publicly.
Each <li class="job-item"> has title, city, company in the text parts.
"""
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobagent.http_client import HttpClient
from jobagent.models import JobRecord
from jobagent.sources.jsonld import job_description, page_text

_BASE_URL = "https://jobify360.co.il"
_SOURCE = "jobify"

# Tech-relevant Jobify category slugs confirmed to exist (404s omitted).
_SLUGS = [
    "software-engineer",
    "software-developer",
    "senior-software-engineer",
    "backend-developer",
    "backend-engineer",
    "c-developer",
    "embedded-software-engineer",
    "linux-engineer",
    "systems-engineer",
    "devops-engineer",
    "network-engineer",
    "security-researcher",
]


def fetch(client: HttpClient) -> list[JobRecord]:
    seen: dict[str, JobRecord] = {}
    for slug in _SLUGS:
        url = f"{_BASE_URL}/jobs/{slug}"
        try:
            html = client.get(url).text
        except Exception:
            continue
        for job in parse_page(html, url):
            if job.url not in seen:
                seen[job.url] = job
    return list(seen.values())


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Return the full description (usually Hebrew), or "" on any error."""
    try:
        return job_description(page_text(client.get(job.url)))
    except Exception:
        return ""


def parse_page(html: str, page_url: str = "") -> list[JobRecord]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[JobRecord] = []
    for li in soup.find_all("li", class_="job-item"):
        job = _parse_item(li, page_url)
        if job:
            jobs.append(job)
    return jobs


def _parse_item(li, page_url: str) -> JobRecord | None:
    title_el = li.find(class_="title")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)
    if not title:
        return None

    # The job URL is in the <a class="w-100"> href
    link = li.find("a", class_="w-100")
    if not link or not link.get("href"):
        return None
    url = link["href"]
    if not url.startswith("http"):
        url = urljoin(_BASE_URL, url)

    # Text parts order: title | city | company
    parts = [p.strip() for p in li.get_text("|", strip=True).split("|") if p.strip()]
    # Remove the title itself from parts
    remaining = [p for p in parts if p != title and len(p) > 1]
    # Jobify only lists Israeli jobs, and its cities are in Hebrew. Stamp the
    # country here so the location filter never has to know Hebrew city names.
    city = remaining[0] if len(remaining) > 0 else ""
    location = f"{city}, Israel" if city else "Israel"
    company = remaining[1] if len(remaining) > 1 else ""

    return JobRecord(
        source=_SOURCE,
        company=company or "Unknown",
        title=title,
        location=location,
        url=url,
    )
