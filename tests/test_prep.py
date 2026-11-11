"""Tests for jobagent/prep.py: the model call is mocked; the .docx is real."""
import json
import shutil
from unittest.mock import MagicMock, patch

import pytest
from docx import Document

from jobagent import cv_render
from jobagent.models import JobRecord
from jobagent.prep import (
    MAX_POINTS, MAX_QUESTIONS, MAX_TOPICS, PREP_MODEL, Prep, generate_prep, parse_prep, render_prep,
)
from jobagent.scorer import ScoreResult

JOB = JobRecord(source="workday:nvidia", company="NVIDIA", title="Junior C++ Engineer", location="Israel",
                url="https://nvidia.wd5.myworkdayjobs.com/x/job/Yokneam/Junior-C---Engineer_JR2001234",
                description="C++, Linux, multithreading, networking. Nice to have: CUDA.")
SCORE = ScoreResult(score=82, variant="systems", seniority_ok=True, matched=["C++", "Linux"],
                    missing=["CUDA"], needs_review=["sockets"], reason="Good fit.")
RAW = {
    "role_focus": "Systems C++ on Linux.",
    "topics": [{"name": "Threads", "why": "The role is concurrent.",
                "points": ["mutex vs condition variable", "deadlock"]}],
    "questions": [{"question": "How does your thread pool shut down?", "kind": "cv",
                   "answer_points": ["stop flag", "join workers"]}],
}


def _mock_client(payload: dict):
    block = MagicMock(type="text", text=json.dumps(payload))
    client = MagicMock()
    client.beta.messages.create.return_value = MagicMock(content=[block], stop_reason="end_turn")
    return client


def test_parse_prep_reads_the_schema():
    prep = parse_prep(RAW)
    assert prep.topics[0].name == "Threads"
    assert prep.questions[0].kind == "cv"


def test_parse_prep_applies_caps():
    raw = {
        "role_focus": "x",
        "topics": [{"name": f"t{i}", "why": "", "points": ["p"] * 20} for i in range(30)],
        "questions": [{"question": f"q{i}", "kind": "technical", "answer_points": ["a"] * 20}
                      for i in range(30)],
    }
    prep = parse_prep(raw)
    assert len(prep.topics) == MAX_TOPICS and len(prep.questions) == MAX_QUESTIONS
    assert len(prep.topics[0].points) == MAX_POINTS


def test_parse_prep_rejects_an_empty_sheet():
    with pytest.raises(ValueError):
        parse_prep({"role_focus": "x", "topics": [], "questions": []})


@patch("jobagent.prep.load_master", lambda: cv_render.load_master(cv_render.EXAMPLE_MASTER))
@patch("jobagent.prep.anthropic.Anthropic")
def test_generate_prep_request(mock_cls):
    mock_cls.return_value = client = _mock_client(RAW)
    prep = generate_prep(JOB, SCORE, "key")
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == PREP_MODEL
    assert kwargs["fallbacks"] == "default"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    prompt = kwargs["messages"][0]["content"]
    assert "CUDA" in prompt and "sockets" in prompt and "Junior C++ Engineer" in prompt
    assert "{facts}" not in prompt and "{missing}" not in prompt
    assert isinstance(prep, Prep)


@patch("jobagent.prep.anthropic.Anthropic")
def test_generate_prep_raises_on_refusal(mock_cls):
    client = _mock_client(RAW)
    client.beta.messages.create.return_value.stop_reason = "refusal"
    mock_cls.return_value = client
    with pytest.raises(ValueError):
        generate_prep(JOB, SCORE, "key")


@patch("jobagent.prep._to_pdf", side_effect=lambda p: p)
def test_render_prep_content(mock_pdf, tmp_path):
    path = render_prep(parse_prep(RAW), JOB, SCORE, tmp_path)
    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert "Interview prep: NVIDIA - Junior C++ Engineer" in text
    assert "JR2001234" in text and "Match: 82%" in text
    assert "1. How does your thread pool shut down?" in text
    assert "CUDA" in text  # gaps section comes from scoring, not the model
    assert path.name == "Interview_prep_NVIDIA.docx"


@pytest.mark.skipif(not shutil.which("soffice"), reason="needs LibreOffice")
def test_render_prep_makes_a_real_pdf(tmp_path):
    pdf = render_prep(parse_prep(RAW), JOB, SCORE, tmp_path)
    assert pdf.suffix == ".pdf" and pdf.stat().st_size > 1000
