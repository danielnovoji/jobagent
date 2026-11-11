"""Tailor the CV summary for a specific job and render the PDF."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import anthropic

from .cv_render import allowed_facts_text, load_master, render
from .models import JobRecord
from .scorer import MAX_TOKENS, MODEL, ScoreResult, response_text

log = logging.getLogger(__name__)

_TAILOR_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "tailor_summary.md"
_COVER_NOTE = Path(__file__).resolve().parent.parent / "prompts" / "cover_note.txt"

_EN_EM = re.compile(r"[–—]")
_FIRST_PERSON = re.compile(r"\bI\b(?!/O)|\b[Mm]y\b")  # "I/O" is not first person
_FORBIDDEN = re.compile(
    r"\b(production|shipped|deployed|senior|expert|years of experience|embedded hardware|firmware)\b",
    re.I,
)
# He graduated and isn't enrolled. The model copies "student" from student-role postings,
# and the term check can't catch it because "student intern" is a real fact. So only
# "student intern" / "student internship" (Lcontrol) may use the word.
_STUDENT = re.compile(r"\b(?:students?\b(?!\s+intern)|undergraduate|enrolled|studying)\b", re.I)
# A word plus the symbols that make it a tech name: C++, C#, Node.js. "/" and "-"
# split on purpose, so "TCP/IP" is two terms and "Linux-based" contains "Linux".
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[+#]+|(?:\.[A-Za-z0-9]+)+)?")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?:;])\s+|\n+")


def _terms(text: str) -> set[str]:
    return {t.lower() for t in _TERM.findall(text)}


def _names(text: str) -> set[str]:
    """Words used as names: a capital mid-sentence, or a digit/symbol anywhere.

    The first word of a sentence is skipped, since its capital means nothing.
    """
    names: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text):
        for i, m in enumerate(_TERM.finditer(sentence)):
            word = m.group()
            if re.search(r"[0-9+#.]", word) or (i > 0 and re.search(r"[A-Z]", word)):
                names.add(word.lower())
    return names


def _invented_term(text: str, known: str, job_text: str) -> str | None:
    """Return a technology in the summary that isn't in the facts, if any.

    This can reject a harmless word, but a rejection only means the base summary
    goes out instead. A false pass would put a skill on the CV Daniel doesn't have.
    """
    # The job's proper nouns catch "docker" and a sentence-opening "Kubernetes".
    # Words it also writes in lowercase are ordinary English ("Team" in a heading).
    job_nouns = _names(job_text) - set(re.findall(r"\b[a-z][a-z0-9]*\b", job_text))
    suspects = _names(text) | (_terms(text) & job_nouns)
    unknown = suspects - _terms(known)
    return min(unknown) if unknown else None


def _validate(text: str, facts: str, *, base: str = "", job_text: str = "") -> str | None:
    """Return an error description, or None if the summary passes all checks."""
    if len(text) > 600:
        return f"too long ({len(text)} chars)"
    if _EN_EM.search(text):
        return "contains en/em dash"
    if _FIRST_PERSON.search(text):
        return "contains first person (I/my)"
    m = _FORBIDDEN.search(text)
    if m:
        return f"forbidden word: {m.group()!r}"
    m = _STUDENT.search(text)
    if m:
        return f"describes him as a current student: {m.group()!r}"
    # Compare whole numbers: as substrings, the "5" in a date would allow any "5".
    known_numbers = set(re.findall(r"\d+", facts))
    for n in re.findall(r"\d+", text):
        if n not in known_numbers:
            return f"number {n!r} not found in facts"
    term = _invented_term(text, f"{facts}\n{base}", job_text)
    if term:
        return f"term {term!r} not found in facts"
    return None


def tailor_summary(job: JobRecord, score: ScoreResult, api_key: str) -> str:
    """Return a tailored summary; falls back to the variant's base summary on any failure."""
    master = load_master()
    base = master["variants"][score.variant]["summary"]
    facts = allowed_facts_text(master, score.variant)

    template = _TAILOR_PROMPT.read_text(encoding="utf-8")
    parts = template.split("---", 1)
    prompt = parts[1].strip() if len(parts) > 1 else template.strip()
    job_text = (
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n{job.description}"
    )
    prompt = (
        prompt.replace("{facts}", facts)
              .replace("{base_summary}", base)
              .replace("{job}", job_text)
              .replace("{matched}", ", ".join(score.matched) if score.matched else "none")
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": prompt}],
        )
        candidate = response_text(msg)
        err = _validate(candidate, facts, base=base, job_text=job_text)
        if err:
            log.warning("tailor: %s for %s — falling back to base summary", err, job.url)
            return base
        return candidate
    except Exception as exc:
        log.error("tailor: LLM call failed for %s: %s", job.url, exc)
        return base


def render_cv(job: JobRecord, score: ScoreResult, summary: str, out_dir: Path) -> Path | None:
    """Render the tailored CV to PDF. Returns the PDF path, or None on failure."""
    try:
        stem = f"Daniel_Novojilovsky_CV_{score.variant}"
        result = render(score.variant, out_dir, stem=stem, summary=summary, pdf=True)
        return result.pdf
    except Exception as exc:
        log.error("render_cv: failed for %s: %s", job.url, exc)
        return None


def base_summary(variant: str) -> str:
    """Return the variant's base summary unchanged (used in no-LLM mode)."""
    return load_master()["variants"][variant]["summary"]


def cover_note(job: JobRecord) -> tuple[str, str]:
    """Return (subject, body) for this job. The template's first line is the subject."""
    template = _COVER_NOTE.read_text(encoding="utf-8")
    text = template.replace("{role}", job.title).replace("{company}", job.company)
    first_line, _, body = text.partition("\n")
    return first_line.removeprefix("Subject:").strip(), body.strip()
