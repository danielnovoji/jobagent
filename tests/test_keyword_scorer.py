"""Tests for keyword_scorer.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jobagent.keyword_scorer import keyword_score_job, _tokens, _normalize, _skill_tokens
from jobagent.models import JobRecord

# Minimal master.yaml structure for tests.
# Variants must have a `skills` list referencing skill_group keys.
_MASTER = {
    "skill_groups": {
        "cpp_skills": {"label": "Languages", "items": ["C++", "Linux", "Python", "Git", "CMake", "GoogleTest"]},
        "sys_skills": {"label": "Systems", "items": ["Multithreading", "POSIX threads", "sockets", "epoll", "IPC"]},
        "web_skills": {"label": "Web", "items": ["React", "TypeScript", "Node.js", "JavaScript", "REST APIs", "Git"]},
    },
    "variants": {
        "systems": {
            "summary": "C++ systems engineer.",
            "skills": ["cpp_skills", "sys_skills"],
        },
        "hft": {
            "summary": "Low-latency C++ engineer.",
            "skills": ["cpp_skills", "sys_skills"],
        },
        "embedded_linux": {
            "summary": "Embedded Linux engineer.",
            "skills": ["cpp_skills", "sys_skills"],
        },
        "fullstack": {
            "summary": "Full-stack developer.",
            "skills": ["web_skills"],
        },
    },
    "needs_review": ["Spring Boot", "Java"],
}

# Skill sets extracted from the mock master above
_CPP_SKILL_TOKS = _tokens("C++ Linux Python Git CMake GoogleTest Multithreading sockets epoll IPC")
_WEB_SKILL_TOKS = _tokens("React TypeScript nodejs JavaScript REST APIs Git")


def _job(title: str, description: str = "", company: str = "TestCo") -> JobRecord:
    return JobRecord(
        source="test",
        company=company,
        title=title,
        location="Tel Aviv, Israel",
        url=f"https://example.com/{title.replace(' ', '_')}",
        description=description,
    )


# ------------------------------------------------------------------
# _normalize / _tokens helpers
# ------------------------------------------------------------------

def test_normalize_cpp():
    assert "cpp" in _normalize("C++ developer")
    assert "cpp" in _normalize("c++ linux")


def test_normalize_dotnet():
    assert "dotnet" in _normalize(".NET developer")
    assert "dotnet" in _normalize("ASP.NET framework")


def test_normalize_nodejs():
    assert "nodejs" in _normalize("Node.js backend")
    assert "nodejs" in _normalize("NodeJS runtime")


def test_tokens_stopword_removed():
    toks = _tokens("experience with the framework")
    assert "the" not in toks
    assert "experience" not in toks  # in stopwords
    assert "framework" in toks


def test_tokens_short_words_excluded():
    toks = _tokens("an AI model")
    assert "an" not in toks
    # "AI" is 2 chars, should be excluded
    assert "ai" not in toks


# ------------------------------------------------------------------
# _skill_tokens
# ------------------------------------------------------------------

def test_skill_tokens_returns_set():
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER):
        toks = _skill_tokens(_MASTER, "systems")
    assert "cpp" in toks
    assert "linux" in toks
    assert "multithreading" in toks


def test_skill_tokens_fullstack_different():
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER):
        sys_toks = _skill_tokens(_MASTER, "systems")
        web_toks = _skill_tokens(_MASTER, "fullstack")
    assert "cpp" in sys_toks
    assert "cpp" not in web_toks
    assert "react" in web_toks


# ------------------------------------------------------------------
# keyword_score_job
# ------------------------------------------------------------------

def test_cpp_job_scores_above_threshold():
    """A C++ systems job should score >= 40 against the systems skill tokens."""
    job = _job("Software Engineer C++",
               description="C++ Linux multithreading sockets cmake googletest networking Python")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert result.score >= 40
    assert result.variant == "systems"


def test_java_only_job_scores_below_threshold():
    """A Java-only job with no C++/Linux terms should score below 40."""
    job = _job("Java Backend Developer",
               description="Spring Boot Java Hibernate Maven microservices Kafka SQL database")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert result.score < 40


def test_three_years_penalty_applied():
    """3+ years requirement applies a -20 penalty."""
    desc_with = "C++ Linux multithreading 3+ years experience required"
    desc_without = "C++ Linux multithreading great opportunity"
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result_with = keyword_score_job(_job("C++ Engineer", description=desc_with))
        result_without = keyword_score_job(_job("C++ Engineer", description=desc_without))
    assert result_with is not None and result_without is not None
    assert result_with.score <= result_without.score - 20


def test_four_years_penalty_applied():
    desc_pen = "C++ Linux multithreading 4 years experience required"
    desc_clean = "C++ Linux multithreading great entry level role"
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        r_pen = keyword_score_job(_job("C++ Engineer", description=desc_pen))
        r_clean = keyword_score_job(_job("C++ Engineer", description=desc_clean))
    assert r_pen is not None and r_clean is not None
    assert r_pen.score <= r_clean.score - 20


def test_hardware_penalty_applied():
    desc_hw = "C++ Linux RTOS microcontroller embedded firmware drivers"
    desc_sw = "C++ Linux multithreading networking cmake build systems"
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        r_hw = keyword_score_job(_job("Embedded Engineer", description=desc_hw))
        r_sw = keyword_score_job(_job("Systems Engineer", description=desc_sw))
    assert r_hw is not None and r_sw is not None
    assert r_hw.score <= r_sw.score - 20


def test_needs_review_detected():
    """Java in description → needs_review populated."""
    job = _job("Backend Developer", description="C++ Linux Spring Boot Java microservices")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert "Java" in result.needs_review or "Spring Boot" in result.needs_review


def test_best_variant_selected():
    """When multiple variants are tested, the one with the highest score wins."""
    job = _job("React Developer",
               description="React TypeScript nodejs REST APIs JavaScript Git full-stack")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems", "fullstack"]):
        result = keyword_score_job(job)
    assert result is not None
    assert result.variant == "fullstack"


def test_score_capped_at_100():
    """Score never exceeds 100 even with perfect overlap."""
    job = _job("Engineer", description="cpp linux multithreading sockets epoll ipc cmake googletest python git")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert result.score <= 100


def test_empty_description_still_scores():
    """Job with no description should still produce a result (title-only scoring)."""
    job = _job("C++ Software Engineer", description="")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert isinstance(result.score, int)


def test_irrelevant_job_scores_low():
    """A manufacturing/facilities job with no technical terms should score near 0."""
    job = _job("Manufacturing Technician",
               description="operate production equipment ensuring quality standards assembly inspection")
    with patch("jobagent.keyword_scorer.load_master", return_value=_MASTER), \
         patch("jobagent.keyword_scorer.select_variants", return_value=["systems"]):
        result = keyword_score_job(job)
    assert result is not None
    assert result.score < 15
