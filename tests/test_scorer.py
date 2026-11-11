"""Tests for jobagent/scorer.py — uses mocked LLM responses, no real API calls."""
import json
from dataclasses import replace
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest

from jobagent.models import JobRecord
from jobagent.scorer import (
    MODEL,
    LLMUnavailable,
    ScoreResult,
    _score_one,
    response_text,
    score_job,
    select_variants,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_job(title="Software Engineer", description="", company="ACME", location="Israel",
             url="https://example.com/job/1"):
    return JobRecord(source="test", company=company, title=title, location=location,
                     url=url, description=description)


def make_llm_response(score=75, seniority_ok=True, hard_blockers=None, matched=None,
                      missing=None, needs_review=None, reason="Good match."):
    """Return a JSON string matching the expected scorer output."""
    return json.dumps({
        "score": score,
        "seniority_ok": seniority_ok,
        "hard_blockers": hard_blockers or [],
        "matched": matched or ["C++"],
        "missing": missing or [],
        "needs_review": needs_review or [],
        "reason": reason,
    })


def make_mock_client(*response_texts):
    """Return a mock anthropic.Anthropic client yielding these responses in order."""
    client = MagicMock()
    side_effects = []
    for text in response_texts:
        content = MagicMock(type="text", text=text)
        side_effects.append(MagicMock(content=[content], stop_reason="end_turn"))
    client.messages.create.side_effect = side_effects
    return client


# ---------------------------------------------------------------------------
# select_variants
# ---------------------------------------------------------------------------

def test_select_variants_hft():
    job = make_job(title="Low-Latency C++ Developer", description="HFT trading signals")
    assert select_variants(job) == ["hft"]


def test_select_variants_embedded():
    job = make_job(title="Embedded SW Engineer", description="RTOS firmware MCU development")
    assert select_variants(job) == ["embedded_linux"]


def test_select_variants_fullstack_only():
    job = make_job(title="React Developer", description="TypeScript Node.js frontend")
    assert select_variants(job) == ["fullstack"]


def test_select_variants_systems_only():
    job = make_job(title="Linux Kernel Engineer", description="C++ networking driver")
    assert select_variants(job) == ["systems"]


def test_select_variants_ambiguous_both():
    job = make_job(title="Backend Developer", description="C++ and React")
    assert select_variants(job) == ["systems", "fullstack"]


def test_select_variants_ambiguous_neither():
    job = make_job(title="Software Engineer", description="Join our team")
    assert select_variants(job) == ["systems", "fullstack"]


def test_select_variants_cpp_triggers_systems():
    job = make_job(title="C++ Engineer", description="")
    assert select_variants(job) == ["systems"]


def test_select_variants_hft_takes_priority_over_embedded():
    job = make_job(title="HFT Engineer", description="low-latency firmware RTOS")
    assert select_variants(job) == ["hft"]


# ---------------------------------------------------------------------------
# _score_one
# ---------------------------------------------------------------------------

MINIMAL_MASTER = {
    "contact": {"name": "D", "email": "d@t.com", "phone": "050", "location": "IL",
                "linkedin": "li", "github": "gh"},
    "variants": {
        "systems": {
            "title": "Engineer",
            "summary": "Systems base summary.",
            "skills": ["lang"],
            "projects": [],
        }
    },
    "skill_groups": {"lang": {"label": "Languages", "items": ["C++"]}},
    "experience": [{"role": "Intern", "org": "Co", "years": "2024", "bullets": ["built X"]}],
    "projects": {},
    "education": [{"degree": "BSc CS", "org": "Uni", "years": "2024",
                   "coursework": {"systems": ["OS"]}, "grades": "90"}],
}


@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++\nExperience: Intern")
def test_score_one_parses_response(mock_facts):
    client = make_mock_client(make_llm_response(score=75, matched=["C++", "Linux"]))
    result = _score_one(client, "header\n---\n{facts}\n{job}", MINIMAL_MASTER, "systems",
                        make_job())
    assert result is not None
    assert result.score == 75
    assert result.variant == "systems"
    assert result.seniority_ok is True
    assert "C++" in result.matched


@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_one_caps_score_on_seniority_false(mock_facts):
    client = make_mock_client(make_llm_response(score=50, seniority_ok=False))
    result = _score_one(client, "---\n{facts}\n{job}", MINIMAL_MASTER, "systems", make_job())
    assert result is not None
    assert result.score == 30
    assert result.seniority_ok is False


@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_one_caps_score_on_hard_blockers(mock_facts):
    client = make_mock_client(make_llm_response(score=65, hard_blockers=["5+ years required"]))
    result = _score_one(client, "---\n{facts}\n{job}", MINIMAL_MASTER, "systems", make_job())
    assert result is not None
    assert result.score == 45
    assert "5+ years required" in result.hard_blockers


@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_one_retries_on_bad_json(mock_facts):
    client = make_mock_client("not json", make_llm_response(score=70))
    result = _score_one(client, "---\n{facts}\n{job}", MINIMAL_MASTER, "systems", make_job())
    assert result is not None
    assert result.score == 70
    assert client.messages.create.call_count == 2


@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_one_returns_none_after_two_failures(mock_facts):
    client = make_mock_client("bad json", "also bad")
    result = _score_one(client, "---\n{facts}\n{job}", MINIMAL_MASTER, "systems", make_job())
    assert result is None
    assert client.messages.create.call_count == 2


# ---------------------------------------------------------------------------
# score_job (integration of variant selection + _score_one)
# ---------------------------------------------------------------------------

@patch("jobagent.scorer.anthropic.Anthropic")
@patch("jobagent.scorer.load_master", return_value=MINIMAL_MASTER)
@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_job_returns_best_variant(mock_facts, mock_master, mock_cls):
    # Ambiguous job → scores systems=50, fullstack=70 → picks fullstack
    systems_response = make_llm_response(score=50, reason="Partial match")
    fullstack_response = make_llm_response(score=70, reason="Good match")
    client = make_mock_client(systems_response, fullstack_response)
    mock_cls.return_value = client

    job = make_job(title="Backend Developer", description="C++ and React")  # ambiguous
    result = score_job(job, api_key="test-key")
    assert result is not None
    assert result.score == 70


@patch("jobagent.scorer.anthropic.Anthropic")
@patch("jobagent.scorer.load_master", return_value=MINIMAL_MASTER)
@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_job_returns_none_when_all_fail(mock_facts, mock_master, mock_cls):
    client = make_mock_client("bad", "bad", "bad", "bad")  # 2 variants × 2 attempts
    mock_cls.return_value = client
    job = make_job(title="Backend Developer", description="C++ and React")
    result = score_job(job, api_key="test-key")
    assert result is None


@patch("jobagent.scorer.anthropic.Anthropic")
@patch("jobagent.scorer.load_master", return_value=MINIMAL_MASTER)
@patch("jobagent.scorer.allowed_facts_text", return_value="Skills: C++")
def test_score_job_single_variant(mock_facts, mock_master, mock_cls):
    client = make_mock_client(make_llm_response(score=80))
    mock_cls.return_value = client
    job = make_job(title="C++ Linux Engineer", description="kernel systems networking")
    result = score_job(job, api_key="test-key")
    assert result is not None
    assert result.score == 80
    assert client.messages.create.call_count == 1  # only one variant


# ---------------------------------------------------------------------------
# Haiku 5.5 response handling
# ---------------------------------------------------------------------------

def _reply(*blocks, stop_reason="end_turn"):
    return MagicMock(content=list(blocks), stop_reason=stop_reason)


def test_response_text_skips_leading_thinking_block():
    thinking = MagicMock(type="thinking", spec=["type"])
    text = MagicMock(type="text", text=" {} ")
    assert response_text(_reply(thinking, text)) == "{}"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_response_text_rejects_incomplete_answer(stop_reason):
    with pytest.raises(ValueError, match=stop_reason):
        response_text(_reply(MagicMock(type="text", text="{"), stop_reason=stop_reason))


def test_response_text_rejects_reply_with_no_text():
    with pytest.raises(ValueError):
        response_text(_reply(MagicMock(type="thinking", spec=["type"])))


def test_score_one_requests_schema_output_on_current_haiku():
    client = make_mock_client(make_llm_response())
    with patch("jobagent.scorer.allowed_facts_text", return_value="facts"):
        _score_one(client, "x---y {facts} {job}", {}, "systems", make_job())
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == MODEL == "claude-haiku-5-5"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["effort"] == "low"
    # Thinking counts towards max_tokens, so a cap sized for the answer returns nothing.
    assert kwargs["max_tokens"] >= 1024


def test_score_one_clamps_out_of_range_score():
    client = make_mock_client(make_llm_response(score=140))
    with patch("jobagent.scorer.allowed_facts_text", return_value="facts"):
        result = _score_one(client, "x---y {facts} {job}", {}, "systems", make_job())
    assert result.score == 100


def test_score_job_raises_when_api_is_down():
    """A dead API must stop the cycle, not mark every job as failed."""
    client = MagicMock()
    client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())
    with patch("jobagent.scorer.anthropic.Anthropic", return_value=client), \
            patch("jobagent.scorer.load_master", return_value={}), \
            patch("jobagent.scorer.allowed_facts_text", return_value="facts"):
        with pytest.raises(LLMUnavailable):
            score_job(make_job(title="C++ Engineer"), "key")


# ---------------------------------------------------------------------------
# Real SDK against a fake server. MagicMock accepts any kwargs and any attribute,
# so these are the tests that would notice a request the API rejects or a
# response shape the code misreads.
# ---------------------------------------------------------------------------

def _sdk_client(handler):
    transport = httpx2.MockTransport(handler)
    return anthropic.Anthropic(
        api_key="test", max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=transport)
    )


def _api_message(*content, stop_reason="end_turn"):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": list(content), "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _score_with_sdk(handler):
    with patch("jobagent.scorer.anthropic.Anthropic", return_value=_sdk_client(handler)), \
            patch("jobagent.scorer.load_master", return_value={}), \
            patch("jobagent.scorer.allowed_facts_text", return_value="C++"):
        return score_job(make_job(title="C++ Engineer"), "key")


def test_sdk_sends_expected_request_and_parses_thinking_reply():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx2.Response(200, json=_api_message(
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": make_llm_response(score=72)},
        ))

    result = _score_with_sdk(handler)
    assert result.score == 72
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "temperature" not in seen  # rejected by Haiku 5.5


def test_sdk_refusal_is_a_failed_job_not_an_outage():
    def handler(request):
        return httpx2.Response(200, json=_api_message(stop_reason="refusal"))

    assert _score_with_sdk(handler) is None


@pytest.mark.parametrize("status,error_type,message", [
    (401, "authentication_error", "invalid x-api-key"),
    (400, "invalid_request_error", "Your credit balance is too low"),
    (429, "rate_limit_error", "rate limited"),
    (529, "overloaded_error", "overloaded"),
])
def test_sdk_api_errors_become_llm_unavailable(status, error_type, message):
    def handler(request):
        return httpx2.Response(status, json={
            "type": "error", "error": {"type": error_type, "message": message},
        })

    with pytest.raises(LLMUnavailable, match=message):
        _score_with_sdk(handler)
