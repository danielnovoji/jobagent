"""Score a job posting against candidate CV variants using Claude haiku."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .cv_render import allowed_facts_text, load_master
from .models import JobRecord

log = logging.getLogger(__name__)

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "score.md"
MODEL = "claude-haiku-5-5"
# Thinking counts towards max_tokens on Haiku 5.5, so this leaves room well above
# the ~200-token answer. A cap sized for the answer alone can return no text at all.
MAX_TOKENS = 2048

# Passed as output_config.format: the API then guarantees JSON of this shape, so
# there's no markdown-fence stripping. Range limits are enforced in _score_one.
_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "seniority_ok": {"type": "boolean"},
        "hard_blockers": {"type": "array", "items": {"type": "string"}},
        "matched": {"type": "array", "items": {"type": "string"}},
        "missing": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["score", "seniority_ok", "hard_blockers", "matched",
                 "missing", "needs_review", "reason"],
    "additionalProperties": False,
}


class LLMUnavailable(Exception):
    """The API can't serve anyone right now: bad key, no credit, rate limit, outage.

    Separate from "this one job failed" because the right response differs: stop
    the cycle and leave jobs queued, rather than mark them all as failed.
    """


def response_text(msg) -> str:
    """Return the model's text. Raises ValueError if there isn't a complete answer.

    Reads by block type because a Haiku 5.5 reply can start with a thinking block.
    """
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise ValueError(f"no usable answer: stop_reason={msg.stop_reason}")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if text is None:
        raise ValueError("no text block in response")
    return text.strip()

# Variant selection — checked in priority order: hft > embedded_linux > fullstack > systems > ambiguous
_HFT = re.compile(r"\b(hft|high[- ]freq(?:uency)?|low[- ]latency|trading|quant|market[- ]mak)", re.I)
_EMBEDDED = re.compile(r"\b(firmware|rtos|mcu|microcontroller|bsp|bare[- ]metal)\b", re.I)
_FULLSTACK = re.compile(r"\b(react|angular|vue|node\.?js|typescript|javascript|frontend|full[- ]?stack)\b", re.I)
_SYSTEMS = re.compile(r"c\+\+|\b(systems?|linux|kernel|network(?:ing)?|infra(?:structure)?|driver)\b", re.I)


@dataclass
class ScoreResult:
    score: int
    variant: str
    seniority_ok: bool
    hard_blockers: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    reason: str = ""


def select_variants(job: JobRecord) -> list[str]:
    """Return 1–2 variants to score.

    Distinctive signals win outright. When both systems and fullstack signals
    appear together (or neither does), score both and keep the higher.
    """
    text = f"{job.title} {job.description}"
    if _HFT.search(text):
        return ["hft"]
    if _EMBEDDED.search(text):
        return ["embedded_linux"]
    fs = bool(_FULLSTACK.search(text))
    sys_ = bool(_SYSTEMS.search(text))
    if fs and not sys_:
        return ["fullstack"]
    if sys_ and not fs:
        return ["systems"]
    return ["systems", "fullstack"]


def _prompt_body(template: str) -> str:
    """Return only the LLM-facing part (after the --- separator in the .md file)."""
    parts = template.split("---", 1)
    return parts[1].strip() if len(parts) > 1 else template.strip()


def _score_one(
    client: anthropic.Anthropic,
    template: str,
    master: dict,
    variant: str,
    job: JobRecord,
) -> ScoreResult | None:
    facts = allowed_facts_text(master, variant)
    job_text = (
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n{job.description}"
    )
    prompt = _prompt_body(template).replace("{facts}", facts).replace("{job}", job_text)

    for attempt in range(2):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _SCORE_SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )
            raw = json.loads(response_text(msg))
            score = max(0, min(100, int(raw["score"])))
            seniority_ok = bool(raw["seniority_ok"])
            hard_blockers = [str(b) for b in raw.get("hard_blockers", [])][:6]
            # Enforce caps from the prompt spec
            if not seniority_ok:
                score = min(score, 30)
            elif hard_blockers:
                score = min(score, 45)
            return ScoreResult(
                score=score,
                variant=variant,
                seniority_ok=seniority_ok,
                hard_blockers=hard_blockers,
                matched=[str(m) for m in raw.get("matched", [])][:6],
                missing=[str(m) for m in raw.get("missing", [])][:6],
                needs_review=[str(m) for m in raw.get("needs_review", [])],
                reason=str(raw.get("reason", "")),
            )
        except anthropic.APIError as exc:
            # The SDK has already retried 429s and 5xx by the time this lands.
            raise LLMUnavailable(str(exc)) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if attempt == 0:
                log.warning("scorer: parse failed for %s/%s, retrying: %s", job.url, variant, exc)
            else:
                log.error("scorer: gave up on %s/%s: %s", job.url, variant, exc)
    return None


def score_job(job: JobRecord, api_key: str) -> ScoreResult | None:
    """Score the job, picking the best matching variant.

    Returns None if the model gave no usable answer for this job.
    Raises LLMUnavailable if the API itself is failing.
    """
    template = _PROMPT.read_text(encoding="utf-8")
    master = load_master()
    client = anthropic.Anthropic(api_key=api_key)
    best: ScoreResult | None = None
    for variant in select_variants(job):
        result = _score_one(client, template, master, variant, job)
        if result is not None and (best is None or result.score > best.score):
            best = result
    return best
