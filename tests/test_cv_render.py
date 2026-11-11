"""Regression tests for the CV renderer. PDF tests need LibreOffice + poppler.

`master_path` (conftest) runs each test on the example CV and, when present, the real one.
"""
import shutil

import pytest

from jobagent import cv_render as cv

HAS_PDF_TOOLS = bool(shutil.which("soffice") and shutil.which("pdftotext"))


@pytest.fixture(scope="module")
def master(master_path):
    return cv.load_master(master_path)


def test_unverified_projects_never_appear(master, tmp_path):
    master["projects"]["thread_pool"]["verified"] = False
    c = cv.resolve(master, "systems")
    assert all(p["name"] != "Thread Pool" for p in c["projects"])
    assert "Thread Pool" not in cv.allowed_facts_text(master, "systems")
    master["projects"]["thread_pool"].pop("verified")


@pytest.mark.parametrize("bad", ["Senior engineer — C++", "Built – things"])
def test_dashes_rejected(master, bad):
    with pytest.raises(cv.CVError):
        cv.resolve(master, "systems", summary=bad)


def test_summary_length_cap(master):
    with pytest.raises(cv.CVError):
        cv.resolve(master, "systems", summary="x" * (cv.SUMMARY_MAX_CHARS + 1))


def test_facts_text_has_no_forbidden_claims(master):
    for variant in master["variants"]:
        text = cv.allowed_facts_text(master, variant).lower()
        for word in ("production", "shipped", "deployed", "allocator"):
            assert word not in text, f"{variant}: {word}"


def test_missing_master_is_an_error_not_a_fallback(tmp_path):
    with pytest.raises(cv.CVError, match="master.example.yaml"):
        cv.load_master(tmp_path / "master.yaml")


@pytest.mark.skipif(not HAS_PDF_TOOLS, reason="needs soffice + pdftotext")
@pytest.mark.parametrize("variant", ["systems", "fullstack", "hft", "embedded_linux"])
def test_renders_one_page_with_intact_hyphens(variant, master_path, tmp_path):
    r = cv.render(variant, tmp_path, master_path=master_path)
    assert r.pages == 1
    flat = " ".join(r.text.split())
    for word in ("fault-tolerance", "real-time", "full-stack"):
        if word in flat.lower() or word.replace("-", "") in flat.lower():
            assert word.replace("-", "") not in flat.lower(), f"broken hyphen: {word}"
