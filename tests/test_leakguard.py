"""Leak guard (scripts/leakguard.py). Fake secrets are assembled at runtime so this file
itself never contains a string the guard would block."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "leakguard.py"
_spec = importlib.util.spec_from_file_location("leakguard", SCRIPT)
lg = importlib.util.module_from_spec(_spec)
sys.modules["leakguard"] = lg  # @dataclass looks its module up here while the file executes
_spec.loader.exec_module(lg)

NOREPLY = "1+tester" + "@users.noreply.github.com"
FAKE_TOKEN = "123456789:" + "A" * 35
FAKE_ANTHROPIC = "sk-" + "ant-" + "x" * 30
FAKE_EMAIL = "someone.private" + "@gmail.com"
FAKE_SHEET = "1Zz" + "q" * 40


def rules(findings):
    return [f.rule for f in findings]


# --------------------------------------------------------------------------- paths

@pytest.mark.parametrize("path", [
    ".env", ".env.local", "secrets/sa.json", "secrets/gmail_oauth_client.json", "deploy/server.pem",
    "cv/master.yaml", "CLAUDE.local.md", ".leakguard-denylist", "data/jobagent.db", "out/x/cv.pdf",
    "notes.sqlite",
])
def test_blocked_paths(path):
    assert lg.path_findings(path)


@pytest.mark.parametrize("path", [
    ".env.example", "secrets/.gitkeep", "cv/master.example.yaml", "jobagent/cv_render.py",
    "tests/fixtures/linkedin_alert.eml", "docker-compose.yml",
])
def test_allowed_paths(path):
    assert lg.path_findings(path) == []


# --------------------------------------------------------------------------- patterns

@pytest.mark.parametrize("text,rule", [
    (f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC}", "Anthropic API key"),
    (f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage", "Telegram bot token"),
    ("-----BEGIN " + "PRIVATE KEY-----\nabc", "private key"),
    ('{"type": "service' + '_account", "project_id": "p"}', "Google service account key file"),
    ("secret: GOCSPX-" + "a" * 28, "Google OAuth client secret"),
    ("see docs.google.com/spreadsheets/d/" + FAKE_SHEET + "/edit", "Google Sheet link (contains the sheet id)"),
])
def test_secret_patterns(text, rule):
    assert rule in rules(lg.scan("x.py", text.encode(), {}))


def test_allow_mark_skips_patterns_on_that_line_only():
    text = f"x = '{FAKE_ANTHROPIC}'  # leakguard: allow\ny = '{FAKE_ANTHROPIC}'\n"
    found = lg.scan("x.py", text.encode(), {})
    assert [f.where for f in found] == ["x.py:2"]


def test_clean_code_has_no_findings():
    text = 'sheet = f"https://docs.google.com/spreadsheets/d/{sheet_id}"\nTOKEN = os.environ["TELEGRAM_BOT_TOKEN"]\n'
    assert lg.scan("jobagent/x.py", text.encode(), {}) == []


# --------------------------------------------------------------------------- real local values

@pytest.fixture
def root(tmp_path):
    (tmp_path / ".env").write_text(
        f"ANTHROPIC_API_KEY=\nTELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=987654321\n"
        f"GMAIL_ADDRESS={FAKE_EMAIL}\nGMAIL_APP_PASSWORD='abcd efgh ijkl mnop'\nTRACKER_SHEET_ID={FAKE_SHEET}\n"
        "MATCH_THRESHOLD=60\nGOOGLE_SERVICE_ACCOUNT_FILE=secrets/sa.json\nDRY_RUN=false\n")
    (tmp_path / "secrets").mkdir()
    key = "-----BEGIN " + "PRIVATE KEY-----\nMIIEline1\nQUJDline2key\nREVGline3key\n-----END " + "PRIVATE KEY-----\n"
    (tmp_path / "secrets" / "sa.json").write_text(json.dumps({
        "type": "service" + "_account", "private_key": key, "private_key_id": "f" * 40,
        "client_email": "bot@proj-123.iam.gserviceaccount.com", "token_uri": "https://oauth2.googleapis.com/token",
    }))
    (tmp_path / "cv").mkdir()
    (tmp_path / "cv" / "master.yaml").write_text(
        'contact:\n  name: X\n  phone: "(+972) 52-123-4567"\n  email: ' + FAKE_EMAIL + "\n")
    (tmp_path / ".leakguard-denylist").write_text("# comment\nSecret Company Ltd\n")
    return tmp_path


def test_literals_cover_secrets_but_not_settings(root):
    names = set(lg.load_literals(root))
    for expected in (".env TELEGRAM_BOT_TOKEN", ".env TELEGRAM_CHAT_ID", ".env GMAIL_ADDRESS",
                     ".env GMAIL_APP_PASSWORD", ".env TRACKER_SHEET_ID", "secrets/sa.json private_key_id",
                     "secrets/sa.json client_email", "cv/master.yaml contact phone",
                     "cv/master.yaml contact email", ".leakguard-denylist line 2"):
        assert expected in names
    assert not any(k in n for n in names for k in ("MATCH_THRESHOLD", "GOOGLE_SERVICE_ACCOUNT_FILE", "DRY_RUN",
                                                    "ANTHROPIC_API_KEY", "token_uri", " type"))


@pytest.mark.parametrize("text", [
    "call 052-123-4567", "call 0521234567", "+972 52 123 4567", "(+972) 52-123-4567",
])
def test_phone_found_in_any_format(root, text):
    assert "real value from cv/master.yaml contact phone" in rules(lg.scan("a.md", text.encode(), lg.load_literals(root)))


def test_email_found_case_insensitive_and_chat_id_not_inside_longer_numbers(root):
    lits = lg.load_literals(root)
    assert rules(lg.scan("a.md", FAKE_EMAIL.upper().encode(), lits))
    assert lg.scan("a.json", b'{"id": 19876543210}', lits) == []
    assert "real value from .env TELEGRAM_CHAT_ID" in rules(lg.scan("a.py", b"CHAT = 987654321", lits))


def test_private_key_body_found_even_without_header(root):
    found = lg.scan("notes.txt", b"pasted: QUJDline2key", lg.load_literals(root))
    assert any("private_key" in r for r in rules(found))


def test_no_local_files_means_patterns_only(tmp_path):
    assert lg.load_literals(tmp_path) == {}


# --------------------------------------------------------------------------- end to end with real git

def _git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout


def _run(cwd, *args, stdin=""):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, input=stdin,
                          capture_output=True, text=True)


@pytest.fixture
def repo(root):
    if not shutil.which("git"):  # the Docker image has no git; the hooks only run on a dev machine
        pytest.skip("needs git")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Tester")
    _git(root, "config", "user.email", NOREPLY)
    (root / ".gitignore").write_text(".env\nsecrets/\ncv/master.yaml\n.leakguard-denylist\n")
    (root / "app.py").write_text("print('hi')\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "clean")
    return root


def test_staged_blocks_a_real_value_and_passes_clean(repo):
    assert _run(repo, "staged").returncode == 0
    (repo / "config.py").write_text(f"EMAIL = '{FAKE_EMAIL}'\n")
    _git(repo, "add", "config.py")
    r = _run(repo, "staged")
    assert r.returncode == 1 and "GMAIL_ADDRESS" in r.stderr
    assert FAKE_EMAIL not in r.stderr  # findings never echo the value


def test_force_added_ignored_file_is_still_blocked(repo):
    _git(repo, "add", "-f", ".env")
    r = _run(repo, "staged")
    assert r.returncode == 1 and ".env: blocked path" in r.stderr


def test_push_blocks_personal_author_email(repo):
    sha = _git(repo, "rev-parse", "HEAD").strip()
    refs = f"refs/heads/main {sha} refs/heads/main {lg.ZERO_SHA}\n"
    assert _run(repo, "push", "origin", stdin=refs).returncode == 0
    (repo / "b.py").write_text("x = 1\n")
    _git(repo, "add", "b.py")
    _git(repo, "-c", f"user.email={FAKE_EMAIL}", "commit", "-q", "-m", "oops")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    r = _run(repo, "push", "origin", stdin=f"refs/heads/main {sha} refs/heads/main {lg.ZERO_SHA}\n")
    assert r.returncode == 1 and "author email is not a GitHub noreply address" in r.stderr


def test_real_pre_push_hook_refuses_a_secret_buried_in_history(repo, tmp_path_factory):
    """Secret added then deleted: the tip is clean but the push would still publish it."""
    remote = tmp_path_factory.mktemp("remote")
    _git(remote, "init", "-q", "--bare")
    _git(repo, "remote", "add", "origin", str(remote))
    hooks = repo / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" push "$1"\n')
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", ".githooks")
    (repo / "c.py").write_text(f"TOKEN = '{FAKE_TOKEN}'\n")
    _git(repo, "add", "c.py")
    _git(repo, "commit", "-q", "-m", "add")
    _git(repo, "rm", "-q", "c.py")
    _git(repo, "commit", "-q", "-m", "remove")
    r = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0 and "Telegram bot token" in r.stderr
    assert _git(remote, "branch", "--list") == ""  # nothing reached the remote
