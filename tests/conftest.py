"""Shared test guards."""
import imaplib
import smtplib

import pytest

from jobagent import cv_render

# CV tests run on the committed fictional example, so a fresh public clone passes, and on the
# real gitignored cv/master.yaml when it exists, so the real CV keeps its checks locally.
CV_FILES = [pytest.param(cv_render.EXAMPLE_MASTER, id="example-cv")]
if cv_render.MASTER.exists():
    CV_FILES.append(pytest.param(cv_render.MASTER, id="real-cv"))


@pytest.fixture(scope="module", params=CV_FILES)
def master_path(request):
    return request.param


def _no_network(*args, **kwargs):
    raise RuntimeError("tests must not open real mail connections: patch smtplib/imaplib")


@pytest.fixture(autouse=True)
def no_real_mail(monkeypatch):
    """A test that forgets a patch would otherwise log in to Gmail with mock settings.

    A test's own @patch of these names still wins inside that test.
    """
    monkeypatch.setattr(smtplib, "SMTP_SSL", _no_network)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _no_network)
