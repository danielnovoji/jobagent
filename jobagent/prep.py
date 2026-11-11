"""Interview prep sheet for a job Daniel applied to: what to review and likely questions.

Written once per application, not per card: it's only worth the call for jobs he
actually applied to. Uses the same small model as scoring. Compared side by side on
a real job (2026-11-10), Haiku's sheet was as accurate as Opus's at about 1/50 of
the cost (~$0.003 a sheet), so Daniel chose Haiku.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import anthropic
from docx import Document
from docx.shared import Inches

from .cv_render import _add_text, _bullet, _heading, _plain, _tight, _to_pdf, allowed_facts_text, load_master
from .models import JobRecord, job_number
from .scorer import ScoreResult, _prompt_body, response_text

log = logging.getLogger(__name__)

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "interview_prep.md"
PREP_MODEL = "claude-haiku-5-5"
# Thinking counts towards max_tokens. The answer itself is ~5-7k tokens; this
# leaves room for thinking while staying under the SDK's non-streaming limit.
PREP_MAX_TOKENS = 16000
# If the model declines, re-run the request on another model rather than return
# a refusal. Haiku has no server-side fallback, so on Haiku this does nothing and
# the request runs alone. It's kept so switching PREP_MODEL to Opus or Sonnet stays
# a one-line change with the fallback included.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

MAX_TOPICS, MAX_QUESTIONS, MAX_POINTS = 10, 12, 6
KINDS = {"technical": "technical", "cv": "about your CV", "behavioral": "behavioral"}

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "role_focus": {"type": "string"},
        "topics": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "why": {"type": "string"},
                           "points": _STR_LIST},
            "required": ["name", "why", "points"],
            "additionalProperties": False,
        }},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "kind": {"type": "string", "enum": list(KINDS)},
                           "answer_points": _STR_LIST},
            "required": ["question", "kind", "answer_points"],
            "additionalProperties": False,
        }},
    },
    "required": ["role_focus", "topics", "questions"],
    "additionalProperties": False,
}


@dataclass
class Topic:
    name: str
    why: str
    points: list[str]


@dataclass
class Question:
    question: str
    kind: str
    answer_points: list[str]


@dataclass
class Prep:
    role_focus: str
    topics: list[Topic]
    questions: list[Question]


def _strings(items, cap: int) -> list[str]:
    return [s for s in (str(i).strip() for i in items) if s][:cap]


def parse_prep(raw: dict) -> Prep:
    """Build a Prep from the model's JSON, applying the caps. Raises ValueError if it's empty."""
    topics = [Topic(str(t["name"]).strip(), str(t["why"]).strip(), _strings(t["points"], MAX_POINTS))
              for t in raw["topics"]][:MAX_TOPICS]
    questions = [Question(str(q["question"]).strip(), q["kind"] if q["kind"] in KINDS else "technical",
                          _strings(q["answer_points"], MAX_POINTS))
                 for q in raw["questions"]][:MAX_QUESTIONS]
    if not topics or not questions:
        raise ValueError(f"prep has {len(topics)} topics and {len(questions)} questions")
    return Prep(str(raw["role_focus"]).strip(), topics, questions)


def generate_prep(job: JobRecord, score: ScoreResult, api_key: str) -> Prep:
    """Ask the model for the sheet's content. Raises on any failure."""
    facts = allowed_facts_text(load_master(), score.variant)
    job_text = f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n{job.description}"
    prompt = (
        _prompt_body(_PROMPT.read_text(encoding="utf-8"))
        .replace("{facts}", facts)
        .replace("{job}", job_text)
        .replace("{matched}", ", ".join(score.matched) or "none listed")
        .replace("{missing}", ", ".join(score.missing) or "none listed")
        .replace("{flagged}", ", ".join(score.needs_review) or "none")
    )
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.beta.messages.create(
        model=PREP_MODEL,
        max_tokens=PREP_MAX_TOKENS,
        betas=[_FALLBACK_BETA],
        fallbacks="default",
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": _PREP_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_prep(json.loads(response_text(msg)))


def render_prep(prep: Prep, job: JobRecord, score: ScoreResult, out_dir: Path) -> Path:
    """Write the sheet as .docx and convert to PDF. Returns the PDF path."""
    doc = Document()
    section = doc.sections[0]
    section.left_margin = section.right_margin = Inches(0.7)
    section.top_margin = section.bottom_margin = Inches(0.6)

    _plain(doc, f"Interview prep: {job.company} - {job.title}", size=14, bold=True, after=2)
    number = job_number(job.url)
    meta = [f"Job number: {number}" if number else "", f"Match: {score.score}%",
            f"CV variant: {score.variant}", f"Prepared {date.today():%d/%m/%Y}"]
    _plain(doc, " | ".join(m for m in meta if m), size=9)
    _plain(doc, job.url, size=9, after=4)

    _heading(doc, "What this role is about")
    _plain(doc, prep.role_focus, after=2)

    _heading(doc, "Topics to review")
    for topic in prep.topics:
        p = _tight(doc.add_paragraph(), before=5, after=1)
        _add_text(p, topic.name, bold=True)
        if topic.why:
            _add_text(p, f". {topic.why}", italic=True)
        for point in topic.points:
            _bullet(doc, point)

    if score.missing:
        _heading(doc, "Not on your CV")
        _plain(doc, "If it comes up, say you haven't used it yet and how you would pick it up.", after=1)
        for gap in score.missing:
            _bullet(doc, gap)

    _heading(doc, "Likely questions")
    for i, q in enumerate(prep.questions, 1):
        p = _tight(doc.add_paragraph(), before=5, after=1)
        _add_text(p, f"{i}. {q.question}", bold=True)
        _add_text(p, f" ({KINDS[q.kind]})", italic=True, size=9)
        for point in q.answer_points:
            _bullet(doc, point)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "Interview_prep_" + re.sub(r"[^\w.-]+", "_", job.company).strip("_")
    docx_path = out_dir / f"{stem}.docx"
    doc.save(docx_path)
    return _to_pdf(docx_path)


def write_prep(job: JobRecord, score: ScoreResult, api_key: str, out_dir: Path) -> Path:
    """Generate and render the sheet. Raises on any failure; the caller decides about retries."""
    return render_prep(generate_prep(job, score, api_key), job, score, out_dir)
