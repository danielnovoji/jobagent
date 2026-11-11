"""Find a recruiter's email in a posting, for the apply-by-email path."""
import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

# Addresses postings mention that are not where a CV should go.
_NOT_RECRUITER_RE = re.compile(
    r"no-?reply|do-?not-?reply|privacy|accommodat|eeo|equal|support|legal|compliance"
    r"|dpo|security|abuse|fraud|ethics|webmaster|press|media|sales|investor|billing",
    re.IGNORECASE,
)

# First-party portals: an email in these postings is HR boilerplate, not a way to
# apply, so these always go through the portal flow.
_PORTAL_SOURCES = ("workday:", "apple", "meta", "microsoft")


def recruiter_email(source: str, description: str) -> str:
    """Return the address to apply to, or "" if the job goes through a portal.

    Daniel sees this address on the card before Approve sends anything.
    """
    if source.startswith(_PORTAL_SOURCES):
        return ""
    for address in _EMAIL_RE.findall(description or ""):
        if not _NOT_RECRUITER_RE.search(address.split("@")[0]):
            return address.lower()
    return ""
