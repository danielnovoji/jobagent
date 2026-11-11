import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass
class JobRecord:
    source: str       # e.g. "workday:nvidia", "microsoft", "apple", "jobify", "meta"
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    posted_date: str = ""  # ISO-8601 date string, or empty


# Legal-form and filler words that differ between sources for the same employer.
_COMPANY_NOISE = re.compile(r"\b(?:ltd|inc|llc|corp|corporation|co|software|technologies|technology|israel)\b|[^\w\s]",
                            re.IGNORECASE)


def company_key(name: str) -> str:
    """The company name used for matching across sources and the sheet.

    LinkedIn says "Check Point Software Technologies Ltd.", SmartRecruiters says
    "Check Point", LinkedIn also says "Applied Materials - Israel". Without this,
    one job arriving from two sources gets two cards, and the 2-per-company limit
    counts one employer as several.
    """
    key = " ".join(_COMPANY_NOISE.sub(" ", name.lower()).split())
    return key or name.strip().lower()


def job_number(url: str) -> str:
    """The posting's own id, read from its URL. "" if the URL has none.

    Workday "JR2025751" or "2638486-2", LinkedIn "4485055993", Jobify "2776094-jk".
    It goes in the sheet so a job is recognised as already applied to even when
    its title changes. Every source puts the id in the last path segment that
    has a digit; Workday appends it to a title slug after "_". A source whose
    link only works with an internal id (Microsoft) adds the public number as
    ?job_number=, which wins.
    """
    parsed = urlparse(url)
    explicit = parse_qs(parsed.query).get("job_number")
    if explicit and explicit[0].strip():
        return explicit[0].strip()
    workday = parsed.netloc.endswith("myworkdayjobs.com")
    for segment in reversed([s for s in parsed.path.split("/") if s]):
        if workday:
            segment = segment.rsplit("_", 1)[-1]
        if re.search(r"\d", segment):
            return segment
    return ""
