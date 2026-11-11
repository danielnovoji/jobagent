"""Tests for jobagent/tailor.py — uses mocked LLM and render calls."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jobagent.cv_render import allowed_facts_text, load_master
from jobagent.models import JobRecord
from jobagent.scorer import ScoreResult
from jobagent.tailor import _validate, cover_note, tailor_summary, render_cv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_job(title="SW Engineer", company="TestCo", url="https://example.com/job/1",
             description=""):
    return JobRecord(source="test", company=company, title=title,
                     location="Israel", url=url, description=description)


def make_score(variant="systems", score=75, matched=None, needs_review=None):
    return ScoreResult(
        score=score,
        variant=variant,
        seniority_ok=True,
        matched=matched or ["C++", "Linux"],
        needs_review=needs_review or [],
        reason="Good match.",
    )


FACTS = "Skills: C++, Python\nExperience: Intern at Lcontrol (2024)\nEducation: BSc CS 2022-2025"
BASE_SUMMARY = "Systems base summary for this variant."
MASTER = {
    "variants": {
        "systems": {
            "title": "Software Engineer",
            "summary": BASE_SUMMARY,
            "skills": ["lang"],
            "projects": [],
        }
    },
    "skill_groups": {"lang": {"label": "Languages", "items": ["C++"]}},
    "contact": {"name": "D", "email": "d@t.com", "phone": "050",
                "location": "IL", "linkedin": "li", "github": "gh"},
    "experience": [{"role": "Intern", "org": "Co", "years": "2024", "bullets": ["built X"]}],
    "projects": {},
    "education": [{"degree": "BSc CS", "org": "Uni", "years": "2024",
                   "coursework": {"systems": ["OS"]}, "grades": "90"}],
}


def make_mock_llm(response_text: str):
    content = MagicMock(type="text", text=response_text)
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[content], stop_reason="end_turn")
    return client


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------

def test_validate_ok():
    text = "CS graduate with strong C++ and multithreading skills."
    assert _validate(text, FACTS) is None


def test_validate_too_long():
    text = "x" * 601
    err = _validate(text, FACTS)
    assert err is not None
    assert "too long" in err


def test_validate_en_dash():
    text = "Good C++ – Linux developer."
    err = _validate(text, FACTS)
    assert err is not None
    assert "en/em dash" in err


def test_validate_em_dash():
    text = "Good C++—Linux developer."
    err = _validate(text, FACTS)
    assert err is not None
    assert "en/em dash" in err


def test_validate_first_person_I():
    text = "I developed a C++ system."
    err = _validate(text, FACTS)
    assert err is not None
    assert "first person" in err


def test_validate_first_person_my():
    text = "Highlights include my work on Linux."
    err = _validate(text, FACTS)
    assert err is not None
    assert "first person" in err


def test_validate_forbidden_word_deployed():
    text = "Deployed a C++ service at Lcontrol."
    err = _validate(text, FACTS)
    assert err is not None
    assert "deployed" in err.lower()


def test_validate_forbidden_word_production():
    text = "Built production C++ systems."
    err = _validate(text, FACTS)
    assert err is not None
    assert "production" in err


def test_validate_number_in_facts():
    text = "Graduated in 2024 with a BSc."
    # "2024" appears in FACTS → should pass
    assert _validate(text, FACTS) is None


def test_validate_number_not_in_facts():
    text = "Worked on 500 microservices."
    # "500" not in FACTS → should fail
    err = _validate(text, FACTS)
    assert err is not None
    assert "500" in err


# ---------------------------------------------------------------------------
# tailor_summary
# ---------------------------------------------------------------------------

@patch("jobagent.tailor.anthropic.Anthropic")
@patch("jobagent.tailor.allowed_facts_text", return_value=FACTS)
@patch("jobagent.tailor.load_master", return_value=MASTER)
def test_tailor_summary_valid_response(mock_master, mock_facts, mock_cls):
    valid = "CS graduate with strong C++ and multithreading skills."
    mock_cls.return_value = make_mock_llm(valid)
    result = tailor_summary(make_job(), make_score(), api_key="test")
    assert result == valid


@patch("jobagent.tailor.anthropic.Anthropic")
@patch("jobagent.tailor.allowed_facts_text", return_value=FACTS)
@patch("jobagent.tailor.load_master", return_value=MASTER)
def test_tailor_summary_falls_back_on_invalid(mock_master, mock_facts, mock_cls):
    # Summary with a forbidden word → should fall back to base
    bad_summary = "Deployed production C++ at Lcontrol."
    mock_cls.return_value = make_mock_llm(bad_summary)
    result = tailor_summary(make_job(), make_score(), api_key="test")
    assert result == BASE_SUMMARY


@patch("jobagent.tailor.anthropic.Anthropic")
@patch("jobagent.tailor.allowed_facts_text", return_value=FACTS)
@patch("jobagent.tailor.load_master", return_value=MASTER)
def test_tailor_summary_falls_back_on_exception(mock_master, mock_facts, mock_cls):
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("network error")
    mock_cls.return_value = client
    result = tailor_summary(make_job(), make_score(), api_key="test")
    assert result == BASE_SUMMARY


# ---------------------------------------------------------------------------
# render_cv
# ---------------------------------------------------------------------------

@patch("jobagent.tailor.render")
def test_render_cv_returns_pdf_path(mock_render, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.touch()
    mock_render.return_value = MagicMock(pdf=pdf)
    result = render_cv(make_job(), make_score(), "Good summary.", tmp_path)
    assert result == pdf
    mock_render.assert_called_once()


@patch("jobagent.tailor.render")
def test_render_cv_returns_none_on_failure(mock_render, tmp_path):
    mock_render.side_effect = RuntimeError("LibreOffice not found")
    result = render_cv(make_job(), make_score(), "Good summary.", tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# cover_note
# ---------------------------------------------------------------------------

def test_cover_note_substitutes_role_and_company():
    subject, body = cover_note(make_job(title="C++ Developer", company="Nvidia"))
    for text in (subject, body):
        assert "C++ Developer" in text
        assert "Nvidia" in text
        assert "{role}" not in text
        assert "{company}" not in text


def test_cover_note_subject_stays_out_of_body():
    subject, body = cover_note(make_job())
    assert not subject.startswith("Subject:")
    assert "Subject:" not in body


def test_cover_note_contains_links():
    _, body = cover_note(make_job())
    assert "linkedin.com/in/daniel-novojilovsky" in body
    assert "github.com/danielnovoji" in body


# ---------------------------------------------------------------------------
# Honesty: nothing outside the facts reaches the CV
# ---------------------------------------------------------------------------

FULL_FACTS = "Skills / Languages: C++, Python, Linux\nEducation: BSc Computer Science"
JOB_TEXT = "We use Kubernetes and Docker on AWS. Strong C++ required. Our team loves Linux."


@pytest.mark.parametrize("summary", [
    "Graduate with C++ and Kubernetes experience.",      # mid-sentence
    "Kubernetes and C++ developer.",                     # sentence opener
    "Graduate with C++ and docker experience.",          # lowercased
    "Graduate who built systems on AWS with C++.",
])
def test_validate_rejects_skill_not_in_facts(summary):
    assert "not found in facts" in _validate(summary, FULL_FACTS, job_text=JOB_TEXT)


@pytest.mark.parametrize("summary", [
    "Computer Science graduate focused on C++ and Linux.",
    "Strong C++ and Linux background.",                  # capital only because it opens
    "Graduate with C/C++ experience.",
    "Worked on Linux-based tools in C++.",
])
def test_validate_accepts_honest_rewording(summary):
    assert _validate(summary, FULL_FACTS + "\nC/C++", job_text=JOB_TEXT) is None


def test_validate_numbers_compare_whole_not_substring():
    # "5" appears inside "2025" but isn't itself a fact.
    assert _validate("Worked for 5 years.", "Graduated 2025") is not None


def test_validate_io_is_not_first_person():
    assert _validate("Built non-blocking I/O in C++.", "C++ non-blocking I/O") is None


def test_validate_rejects_capitalised_first_person():
    assert _validate("My background is C++.", "C++ background") is not None


@pytest.mark.parametrize("variant", ["systems", "hft", "embedded_linux", "fullstack"])
def test_every_base_summary_passes_its_own_validation(variant, master_path):
    """Base summaries are the fallback, so they have to be valid (example CV + real one if present)."""
    master = load_master(master_path)
    base = master["variants"][variant]["summary"]
    facts = allowed_facts_text(master, variant)
    assert _validate(base, facts, base=base) is None


@patch("jobagent.tailor.anthropic.Anthropic")
@patch("jobagent.tailor.allowed_facts_text", return_value=FACTS)
@patch("jobagent.tailor.load_master", return_value=MASTER)
def test_tailor_summary_falls_back_on_invented_skill(mock_master, mock_facts, mock_cls):
    mock_cls.return_value = make_mock_llm("CS graduate with C++ and Kubernetes skills.")
    assert tailor_summary(make_job(), make_score(), api_key="test") == BASE_SUMMARY


@patch("jobagent.tailor.anthropic.Anthropic")
@patch("jobagent.tailor.allowed_facts_text", return_value=FACTS)
@patch("jobagent.tailor.load_master", return_value=MASTER)
def test_tailor_summary_falls_back_on_refusal(mock_master, mock_facts, mock_cls):
    client = make_mock_llm("anything")
    client.messages.create.return_value.stop_reason = "refusal"
    mock_cls.return_value = client
    assert tailor_summary(make_job(), make_score(), api_key="test") == BASE_SUMMARY


# He graduated: the model copied "student" from a student-role posting title (2026-11-10).
@pytest.mark.parametrize("summary", [
    "BSc Computer Science student with C++ systems projects.",
    "Computer Science undergraduate with C++ skills.",
    "Currently studying Computer Science, with C++ projects.",
    "Students-focused C++ developer.",
])
def test_validate_rejects_calling_him_a_student(summary):
    assert "current student" in _validate(summary, FACTS + "\nComputer Science BSc systems projects")


@pytest.mark.parametrize("summary", [
    "CS graduate who developed C++ tools during a student internship.",
    "CS graduate and former student intern with C++ skills.",
])
def test_validate_allows_student_internship(summary):
    assert _validate(summary, FACTS + "\nCS graduate developed tools former") is None
