import re

# No bare "IL": "Chicago, IL" is Illinois. Word boundaries stop "lod" matching Lodz.
# "ISR" is Workday's country code ("Rehovot,ISR"). [\s-] because sites write
# both "Petah Tikva" and "Petah-Tikva".
_ISRAEL_RE = re.compile(
    r"\b(israel|tel[\s-]?aviv|haifa|jerusalem|rehovot|netanya|herzl?iya|herzlia"
    r"|petah?[\s-]?tikva|petach[\s-]?tikva|ra'?anana|ramat[\s-]?gan|be'?er[\s-]?sheva"
    r"|mig(?:dal)?[\s-]?ha'?emek|rishon|caesarea|yokne'?am|yavne|kiryat[\s-]gat"
    r"|kfar[\s-]?saba|hod[\s-]?hasharon|modi'?in|holon|ashdod|bnei[\s-]?brak"
    r"|rosh[\s-]?ha'?ayin|or[\s-]?yehuda|lod|giv'?atayim|ramat[\s-]?hasharon"
    r"|ne?ss?[\s-]?ziona|yehud|karmiel|beit[\s-]?shemesh|airport[\s-]?city"
    r"|be'?er[\s-]?ya'?akov|kadima|isr)\b"
    r"|ישראל",
    re.IGNORECASE,
)
# Words that may appear in a location with no country attached. Anything else
# ("Virtual/Remote USA", "JPN-Japan-Remote-KLA") means remote from somewhere else.
_REMOTE = {"remote", "virtual", "worldwide", "anywhere", "global", "hybrid"}
_REMOTE_FILLER = {"emea", "work", "from", "home", "only"}

_SENIOR_RE = re.compile(
    r'\b(senior|sr\.?|staff|principal|lead|manager|director'
    r'|vp|vice\s+president|head\s+of|chief|architect)\b',
    re.IGNORECASE,
)

# Match as substrings: \b would miss suffixed forms like "בכירה".
_SENIOR_HE_RE = re.compile(r"בכיר|ראש\s+צוות|מנהל|ארכיטקט")
# Sales, marketing, bookkeeping, recruiting, HR.
_NON_SW_HE_RE = re.compile(r"מכירות|שיווק|הנהלת\s+חשבונות|גיוס|משאבי\s+אנוש")

# Student-only positions need current enrollment; Daniel graduated (2026-11-10).
# Hebrew as a substring so סטודנטית / סטודנטים match too.
_STUDENT_RE = re.compile(r"\bstudents?\b|סטודנט", re.IGNORECASE)

# Job types that are never software roles regardless of description keywords
_NON_SW_TITLE_RE = re.compile(
    r'\b(technician|techniker|physicist|chemist|biologist|supply\s+chain'
    r'|electrician|mechanic|operator|außendienst|field[\s\-]service'
    r'|buyer|procurement|hr\s|recruiter|talent\s+acquisition'
    r'|financial\s+analyst|accountant|bookkeeper|payroll'
    r'|account\s+executive|sales|marketing|business\s+development|customer\s+success'
    r'|legal|counsel|paralegal|designer|copywriter|people\s+partner)\b',
    re.IGNORECASE,
)

_YEARS_RE = re.compile(
    r"(?<![\d.\-–])(\d{1,2})"           # lower bound: the "3" in "3-6 years"
    r"(\s*(?:-|–|to)\s*\d{1,2})?"        # optional upper bound
    r"(\+|\s+or\s+more)?"                # "5+" / "5 or more" is always a requirement
    r"\s*(?:years?|yrs?|שנים|שנות)(?![a-z])",
    re.IGNORECASE,
)
_REQUIREMENT_RE = re.compile(r"experience|ניסיון|נסיון|minimum|at\s+least", re.IGNORECASE)
_MIN_SENIOR_YEARS = 5
_MAX_PLAUSIBLE_YEARS = 20  # above this it's company history: "for over 40 years"

_CLEARANCE_RE = re.compile(
    r'\b(security\s+clearance|secret\s+clearance|top\s+secret|ts/sci'
    r'|clearance\s+required|security[- ]cleared|classified)\b',
    re.IGNORECASE,
)


def is_israel(location: str) -> bool:
    return bool(_ISRAEL_RE.search(location or ""))


def passes_location(location: str) -> bool:
    """True if location is Israel, genuinely remote, or unknown (empty)."""
    if not location or is_israel(location):
        return True
    words = set(re.findall(r"[a-z]+", location.lower()))
    return bool(words & _REMOTE) and words <= _REMOTE | _REMOTE_FILLER


def passes_title_seniority(title: str) -> bool:
    """True if the title does NOT indicate a senior/lead level, a student-only role, or a non-SW role type."""
    return not (_SENIOR_RE.search(title) or _SENIOR_HE_RE.search(title)
                or _STUDENT_RE.search(title) or _NON_SW_TITLE_RE.search(title)
                or _NON_SW_HE_RE.search(title))


def passes_description_seniority(description: str) -> bool:
    """True if the description does NOT require 5+ years.

    Ranges use their lower bound: CLAUDE.md scores "3-6 years" as a stretch.
    A bare number needs requirement wording nearby, so "for over 15 years, KLA
    has..." doesn't silently drop a good job. Dropping is invisible to Daniel
    while a false keep just gets a low score, so this leans towards keeping.
    """
    for m in _YEARS_RE.finditer(description):
        if not _MIN_SENIOR_YEARS <= int(m.group(1)) <= _MAX_PLAUSIBLE_YEARS:
            continue
        nearby = description[max(0, m.start() - 30):m.end() + 40]
        if m.group(3) or _REQUIREMENT_RE.search(nearby):
            return False
    return True


def passes_clearance(description: str, location: str = "") -> bool:
    """True unless the job needs a clearance Daniel can't get.

    An Israeli clearance (סיווג ביטחוני) is not a blocker: he's an Israeli citizen
    and IDF veteran who can obtain it (Daniel, 2026-11-11). So only a job outside
    Israel, such as a remote US role needing TS/SCI, is dropped for it.
    """
    if is_israel(location):
        return True
    return not _CLEARANCE_RE.search(description or "")


def should_fetch(title: str, location: str) -> bool:
    """Quick pre-filter before expensive description fetch."""
    return passes_location(location) and passes_title_seniority(title)
