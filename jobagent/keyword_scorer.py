"""Keyword-overlap scorer for no-LLM mode.

Computes a score 0-100 based on token overlap between the job posting and the
CV facts for a variant. No API calls.
"""
from __future__ import annotations

import re

from .cv_render import load_master
from .models import JobRecord
from .scorer import ScoreResult, select_variants

_STOPWORDS = frozenset({
    "the", "and", "for", "are", "that", "have", "has", "had", "this", "with",
    "from", "will", "they", "their", "were", "also", "about", "such", "into",
    "more", "over", "when", "than", "your", "well", "what", "you", "our",
    "not", "but", "can", "all", "any", "one", "new", "how", "who", "get",
    "use", "was", "his", "her", "its", "which", "been", "some",
    # job-posting boilerplate
    "work", "team", "join", "need", "able", "good", "strong", "looking",
    "seeking", "candidate", "position", "company", "within", "ability",
    "include", "required", "preferred", "bonus", "plus", "role", "using",
    "background", "experience", "help", "other", "across",
    # CV boilerplate
    "skills", "education", "project", "languages",
    "engineer", "software", "developer", "development", "engineering",
})

# 3-4 years mentioned → -20 penalty
_THREE_FOUR_YEARS_RE = re.compile(
    r'\b[34]\+?\s*years?'
    r'|\b3\s*[-–]\s*4\s*years?'
    r'|\bthree\s+(?:to\s+)?four\s+years?',
    re.I,
)

# Embedded/hardware signals → -20 penalty
_HARDWARE_RE = re.compile(
    r'\b(rtos|mcu|microcontroller|firmware|embedded\s+hardware)\b', re.I
)


def _normalize(text: str) -> str:
    """Map special technical tokens to plain ASCII before splitting."""
    text = re.sub(r"[Cc]\+\+", "cpp", text)
    text = re.sub(r"\.?[Nn][Ee][Tt]\b", "dotnet", text)
    text = re.sub(r"[Nn]ode\.?[Jj][Ss]", "nodejs", text)
    return text


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-zA-Z]{3,}", _normalize(text).lower())
        if w not in _STOPWORDS
    }


def _skill_tokens(master: dict, variant: str) -> set[str]:
    """Return tokens for the skill items listed in the variant's skill groups.

    Using skill items instead of the full CV narrative avoids matching on generic
    English words (e.g. 'ensuring', 'dynamic', 'processes') that appear in every
    job description regardless of relevance.
    """
    skill_groups = master.get("skill_groups", {})
    variant_skills = master.get("variants", {}).get(variant, {}).get("skills", [])
    items: list[str] = []
    for group_key in variant_skills:
        group = skill_groups.get(group_key, {})
        items.extend(group.get("items", []))
    return _tokens(" ".join(items))


def keyword_score_job(job: JobRecord) -> ScoreResult | None:
    """Score job by keyword overlap with CV skill items. Returns None if no variants apply."""
    master = load_master()
    desc = job.description or ""
    job_text = f"{job.title} {desc} {job.company}"

    best: ScoreResult | None = None
    for variant in select_variants(job):
        skill_toks = _skill_tokens(master, variant)
        if not skill_toks:
            continue

        job_toks = _tokens(job_text)
        overlap = len(job_toks & skill_toks)
        # Each matching skill token is worth ~3 points (33 skills hit = 100%).
        # Skill token set is ~40 items; a well-matched job hits 10-15 of them → 30-45%.
        score = min(100, round(overlap / len(skill_toks) * 300))

        if _THREE_FOUR_YEARS_RE.search(desc):
            score = max(0, score - 20)
        if _HARDWARE_RE.search(desc):
            score = max(0, score - 20)

        nr_skills = master.get("needs_review", [])
        needs_review = [s for s in nr_skills if s.lower() in job_text.lower()]

        result = ScoreResult(
            score=score,
            variant=variant,
            seniority_ok=True,
            needs_review=needs_review,
            reason="keyword match",
        )
        if best is None or score > best.score:
            best = result

    return best
