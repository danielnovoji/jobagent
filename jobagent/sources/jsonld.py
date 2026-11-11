"""Read job descriptions out of the schema.org JobPosting block on a page.

Workday, Jobify and LinkedIn's public job pages all embed one, so this is the
one parser they share instead of three sets of CSS selectors.
"""
import html
import json
import re

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def clean_html(text: str) -> str:
    """Strip tags and entities. Unescape first: some sites double-encode (&lt;p&gt;)."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def page_text(resp) -> str:
    """Decode a response as UTF-8.

    `requests` assumes ISO-8859-1 when a server sends no charset, which turns
    "it's" into "itâ\\x80\\x99s" and wrecks Hebrew. These sites are all UTF-8.
    """
    return resp.content.decode("utf-8", errors="replace")


def job_description(page: str) -> str:
    """Return the JobPosting description from a page, or "" if there is none."""
    for block in _LD_RE.findall(page):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return clean_html(item.get("description", ""))
    return ""
