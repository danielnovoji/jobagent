"""
Generic Workday scraper.

Searches each company's Workday tenant using the undocumented CXS jobs API.
We use `searchText` to narrow results to Israel/remote rather than paginating
thousands of global jobs — much faster and stays polite.

Full job descriptions are NOT included in the search payload — Workday only
gives sparse bullet fields there. Call `fetch_description(client, job)` for
jobs that pass the pre-filter to get the full JD from the JSON-LD on the
public job page.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Container, Iterator

from jobagent.filters import passes_title_seniority
from jobagent.http_client import HttpClient
from jobagent.models import JobRecord
from jobagent.sources.jsonld import clean_html, job_description, page_text

# Stay polite on broad terms: "Remote" can match thousands of global postings.
MAX_RESULTS_PER_TERM = 600


@dataclass
class WorkdayCompany:
    company: str
    tenant: str
    host: str
    site: str
    # We issue one search per term and union the results.
    location_terms: list[str] = field(default_factory=lambda: ["Israel", "Remote"])

    @property
    def api_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def job_url(self, external_path: str) -> str:
        return f"https://{self.host}/en-US/{self.site}{external_path}"

    def detail_url(self, job_url: str) -> str:
        return job_url.replace(
            f"/en-US/{self.site}", f"/wday/cxs/{self.tenant}/{self.site}", 1
        )


# Per-company config. Tenants after KLA were found by probing (2026-11-11). They
# search "Israel" only: on a global tenant a "Remote" search pages through
# hundreds of foreign jobs that the location filter then drops.
COMPANIES: list[WorkdayCompany] = [
    WorkdayCompany(
        company="Nvidia",
        tenant="nvidia",
        host="nvidia.wd5.myworkdayjobs.com",
        site="NVIDIAExternalCareerSite",
    ),
    WorkdayCompany(
        company="Intel",
        tenant="intel",
        host="intel.wd1.myworkdayjobs.com",
        site="External",
    ),
    WorkdayCompany(
        company="KLA",
        tenant="kla",
        host="kla.wd1.myworkdayjobs.com",
        site="Search",
    ),
    WorkdayCompany("Palo Alto Networks", "paloaltonetworks", "paloaltonetworks.wd5.myworkdayjobs.com",
                   "panwexternalcareers", ["Israel"]),  # CyberArk's roles too, since the acquisition
    WorkdayCompany("Applied Materials", "amat", "amat.wd1.myworkdayjobs.com", "External", ["Israel"]),
    WorkdayCompany("Marvell", "marvell", "marvell.wd1.myworkdayjobs.com", "MarvellCareers", ["Israel"]),
    WorkdayCompany("Unity", "unitytech", "unitytech.wd1.myworkdayjobs.com", "Unity", ["Israel"]),
]


def fetch_company(
    client: HttpClient, config: WorkdayCompany, known_urls: Container[str] = ()
) -> list[JobRecord]:
    """Fetch every posting for the company's search terms.

    `known_urls` skips the per-job detail call for jobs already in the DB.
    """
    seen: dict[str, JobRecord] = {}
    for term in config.location_terms:
        for job in _paginate(client, config, term):
            if job.url not in seen:
                seen[job.url] = job
    for job in seen.values():
        if (_location_unresolved(job) and job.url not in known_urls
                and passes_title_seniority(job.title)):
            _resolve_details(client, config, job)
    return list(seen.values())


_MULTI_LOC_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)


def _location_unresolved(job: JobRecord) -> bool:
    return not job.location or bool(_MULTI_LOC_RE.match(job.location))


def _resolve_details(client: HttpClient, config: WorkdayCompany, job: JobRecord) -> None:
    """Replace a "2 Locations" placeholder with the real offices.

    Search text matches anywhere in a posting, so a "Remote" hit tells us nothing
    about where the job is. If this call fails the placeholder stays, the location
    filter drops the job, and the next cycle tries again.
    """
    try:
        info = client.get(config.detail_url(job.url)).json()["jobPostingInfo"]
    except Exception:
        return
    places = [info.get("location", "")] + (info.get("additionalLocations") or [])
    job.location = "; ".join(p for p in places if p) or job.location
    job.description = clean_html(info.get("jobDescription", "")) or job.description


def _paginate(
    client: HttpClient, config: WorkdayCompany, search_text: str
) -> Iterator[JobRecord]:
    offset = 0
    limit = 20
    total = None
    while True:
        data = client.post(
            config.api_url,
            json={
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": search_text,
            },
        ).json()
        postings = data.get("jobPostings", [])
        for posting in postings:
            job = parse_posting(posting, config)
            if job:
                yield job
        # Workday only sends the real total on the first page; later pages say 0.
        if total is None:
            total = min(data.get("total", 0), MAX_RESULTS_PER_TERM)
        offset += limit
        if not postings or offset >= total:
            break


def parse_posting(posting: dict, config: WorkdayCompany) -> JobRecord | None:
    title = posting.get("title", "").strip()
    external_path = posting.get("externalPath", "")
    if not title or not external_path:
        return None
    url = config.job_url(external_path)
    location = posting.get("locationsText", "")
    bullets = posting.get("bulletFields") or []
    description = "\n".join(str(b) for b in bullets if b)
    posted_date = _parse_posted_date(posting.get("postedOn", ""))
    return JobRecord(
        source=f"workday:{config.tenant}",
        company=config.company,
        title=title,
        location=location,
        url=url,
        description=description,
        posted_date=posted_date,
    )


def fetch_description(client: HttpClient, job: JobRecord) -> str:
    """Fetch the full job description from the public Workday job page (JSON-LD).

    Returns empty string on any error so callers can treat it as optional.
    Only call this for jobs that pass the pre-filter — one HTTP request per job.
    """
    try:
        return job_description(page_text(client.get(job.url)))
    except Exception:
        return ""


def _parse_posted_date(text: str) -> str:
    m = re.match(r"Posted (\d+) Days? Ago", text, re.IGNORECASE)
    if m:
        return (date.today() - timedelta(days=int(m.group(1)))).isoformat()
    if re.search(r"\btoday\b", text, re.IGNORECASE):
        return date.today().isoformat()
    return ""
