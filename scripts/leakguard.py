#!/usr/bin/env python3
"""Leak guard: keep secrets and personal data out of git history and off GitHub.

Modes:
  staged         pre-commit hook: the files staged for this commit
  push <remote>  pre-push hook: every file in every commit being pushed, plus commit emails
  tree           everything that would be committed right now (tracked + untracked, not ignored)
  history        every file in all local history, plus commit emails

Three layers, so one miss isn't a leak:
  1. paths that must never be committed (.env, secrets/, the real CV, the SQLite DB, PDFs)
  2. known secret formats (API keys, bot tokens, private keys, service account files)
  3. the actual values from the local .env, secrets/*.json, the real CV's phone + email, and
     .leakguard-denylist. This catches a secret whose format no regex knows.
Findings name the rule and the location, never the matched value, so the output is safe to paste.

Stdlib only: the hook runs with whatever python3 is on the machine, outside the venv.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40
NOREPLY = "@users.noreply.github.com"
ALLOW_MARK = "leakguard: allow"  # on a line: skip the pattern checks (layer 2) for that line only

BLOCKED_PATHS = [
    (r"(^|/)\.env(\.[^/]+)?$", "env file"),
    (r"(^|/)secrets/(?!\.gitkeep$)", "secrets/ directory"),
    (r"\.(pem|key|p12|pfx)$", "key file"),
    (r"(^|/)cv/master\.yaml$", "real CV (commit cv/master.example.yaml instead)"),
    (r"(^|/)CLAUDE\.local\.md$", "private notes"),
    (r"(^|/)\.leakguard-denylist$", "leak guard denylist"),
    (r"(^|/)(data|out)/", "runtime data"),
    (r"\.(db|sqlite3?|pdf|docx)$", "runtime data / rendered CV"),
]
ALLOWED_PATHS = [r"(^|/)\.env\.example$"]

PATTERNS = [(re.compile(rx), name) for rx, name in [
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "Anthropic API key"),
    # No \b at the start: in an API URL the token follows "bot" directly (.../bot123:AA.../sendMessage).
    (r"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])", "Telegram bot token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r'"type"\s*:\s*"service_account"', "Google service account key file"),
    (r"GOCSPX-[A-Za-z0-9_-]{20,}", "Google OAuth client secret"),
    (r"AIza[0-9A-Za-z_-]{35}", "Google API key"),
    (r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})", "GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{25,}", "Google Sheet link (contains the sheet id)"),
]]

# .env keys whose values are settings, not secrets. Any key NOT listed here is treated as secret,
# so a credential added to .env later is covered without touching this file.
NON_SECRET_ENV_KEYS = {
    "GOOGLE_SERVICE_ACCOUNT_FILE", "GMAIL_ALERTS_LABEL", "MATCH_THRESHOLD", "DAILY_CARD_CAP",
    "MAX_APPS_PER_COMPANY_30D", "DRY_RUN", "FETCH_TIMES", "DIGEST_TIME",
}
# JSON keys in Google key/token files that hold public, generic values.
NON_SECRET_JSON_KEYS = {
    "type", "universe_domain", "auth_uri", "token_uri", "auth_provider_x509_cert_url",
    "client_x509_cert_url", "redirect_uris", "scopes", "expiry",
}


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str


# --------------------------------------------------------------------------- what to look for

def _literal(value: str) -> re.Pattern:
    if value.isdigit():  # chat ids, client ids: don't match inside a longer number
        return re.compile(rf"(?<!\d){value}(?!\d)")
    return re.compile(re.escape(value), re.I if "@" in value else 0)


def _phone(value: str) -> re.Pattern | None:
    """Match the number in any formatting: +972 54..., 054-..., 054 ... ."""
    digits = re.sub(r"\D", "", value)[-9:]
    if len(digits) < 7:
        return None
    return re.compile(r"[\s().-]*".join(digits) + r"(?!\d)")


def _json_strings(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _json_strings(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _json_strings(v, key)
    elif isinstance(node, str):
        yield key, node


def load_literals(root: Path) -> dict[str, re.Pattern]:
    """Real secret / personal values from local, gitignored files -> pattern. Missing files are fine."""
    lits: dict[str, re.Pattern] = {}

    env = root / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*)$", line)
            if not m or m.group(1) in NON_SECRET_ENV_KEYS:
                continue
            value = m.group(2).strip().strip("'\"")
            if len(value) >= 6:
                lits[f".env {m.group(1)}"] = _literal(value)

    for f in sorted((root / "secrets").glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, value in _json_strings(data):
            if key in NON_SECRET_JSON_KEYS or len(value) < 8 or value.startswith(("http://", "https://")):
                continue
            if "PRIVATE KEY" in value:  # the key may be re-wrapped or escaped: match body lines too
                body = [l for l in value.splitlines() if l and "-----" not in l]
                for i, chunk in enumerate(body[1:4]):
                    lits[f"secrets/{f.name} {key} (line {i + 2})"] = _literal(chunk)
                continue
            lits[f"secrets/{f.name} {key}"] = _literal(value)

    cv = root / "cv" / "master.yaml"
    if cv.is_file():
        text = cv.read_text(encoding="utf-8", errors="ignore")
        for field in ("phone", "email"):
            m = re.search(rf"^\s*{field}:\s*[\"']?([^\"'\n#]+?)[\"']?\s*$", text, re.M)
            if not m:
                continue
            rx = _phone(m.group(1)) if field == "phone" else _literal(m.group(1).strip())
            if rx:
                lits[f"cv/master.yaml contact {field}"] = rx

    deny = root / ".leakguard-denylist"
    if deny.is_file():
        for i, line in enumerate(deny.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if len(line) >= 4 and not line.startswith("#"):
                lits[f".leakguard-denylist line {i}"] = _literal(line)
    return lits


# --------------------------------------------------------------------------- checks

def path_findings(path: str) -> list[Finding]:
    if any(re.search(rx, path) for rx in ALLOWED_PATHS):
        return []
    return [Finding(path, f"blocked path: {why}") for rx, why in BLOCKED_PATHS if re.search(rx, path)]


def scan(path: str, data: bytes, literals: dict[str, re.Pattern]) -> list[Finding]:
    out = path_findings(path)
    text = data.decode("utf-8", errors="ignore")
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        out += [Finding(f"{path}:{n}", name) for rx, name in PATTERNS if rx.search(line)]
    for name, rx in literals.items():  # never skippable: a real value never belongs in git
        m = rx.search(text)
        if m:
            out.append(Finding(f"{path}:{text.count(chr(10), 0, m.start()) + 1}", f"real value from {name}"))
    return out


def email_findings(commits: list[str], root: Path) -> list[Finding]:
    if not commits:
        return []
    log = _git(root, "log", "--no-walk", "--format=%H %ae %ce", *commits)
    out = []
    for line in log.splitlines():
        sha, author, committer = line.split(" ", 2)
        for who, email in (("author", author), ("committer", committer)):
            if not email.lower().endswith(NOREPLY):
                out.append(Finding(f"commit {sha[:10]}", f"{who} email is not a GitHub noreply address"))
    return out


# --------------------------------------------------------------------------- git plumbing

def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True,
                          text=True, check=True).stdout


def _read_blobs(root: Path, shas: list[str]):
    """Yield (sha, bytes) via one `git cat-file --batch` process instead of one process per file."""
    if not shas:
        return
    p = subprocess.Popen(["git", "cat-file", "--batch"], cwd=root,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha in shas:
            p.stdin.write(sha.encode() + b"\n")
            p.stdin.flush()
            header = p.stdout.readline().split()
            if len(header) < 3 or header[1] == b"missing":
                continue
            data = p.stdout.read(int(header[2]))
            p.stdout.read(1)  # trailing newline
            yield sha, data
    finally:
        p.stdin.close()
        p.wait()


def _scan_commits(root: Path, commits: list[str], literals) -> list[Finding]:
    blobs: dict[str, str] = {}  # blob sha -> one path it appears at (dedupes identical files)
    for c in commits:
        for entry in _git(root, "ls-tree", "-r", "-z", c).split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            _mode, kind, sha = meta.split()
            if kind == "blob":
                blobs.setdefault(sha, path)
    out = []
    for sha, data in _read_blobs(root, list(blobs)):
        out += scan(blobs[sha], data, literals)
    return out + email_findings(commits, root)


def staged(root: Path, literals) -> list[Finding]:
    paths = [p for p in _git(root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").split("\0") if p]
    out = []
    for path in paths:
        sha = _git(root, "rev-parse", f":{path}").strip()
        for _, data in _read_blobs(root, [sha]):
            out += scan(path, data, literals)
    return out


def tree(root: Path, literals) -> list[Finding]:
    out = []
    for path in _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
        f = root / path
        if path and f.is_file():
            out += scan(path, f.read_bytes(), literals)
    return out


def history(root: Path, literals) -> list[Finding]:
    commits = _git(root, "rev-list", "--all").split()
    return _scan_commits(root, commits, literals)


def push(root: Path, literals, remote: str, refs: str) -> list[Finding]:
    """refs: pre-push stdin, one '<local ref> <local sha> <remote ref> <remote sha>' per line."""
    commits: list[str] = []
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA:  # a branch delete sends nothing
            continue
        local, remote_sha = parts[1], parts[3]
        try:
            rng = [f"{remote_sha}..{local}"] if remote_sha != ZERO_SHA else [local, "--not", f"--remotes={remote}"]
            commits += _git(root, "rev-list", *rng).split()
        except subprocess.CalledProcessError:  # remote sha unknown locally (force push): check all
            commits += _git(root, "rev-list", local, "--not", f"--remotes={remote}").split()
    return _scan_commits(root, list(dict.fromkeys(commits)), literals)


# --------------------------------------------------------------------------- entry point

def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("staged", "push", "tree", "history"):
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    literals = load_literals(root)
    mode = argv[0]
    if mode == "staged":
        found = staged(root, literals)
    elif mode == "tree":
        found = tree(root, literals)
    elif mode == "history":
        found = history(root, literals)
    else:
        found = push(root, literals, argv[1] if len(argv) > 1 else "origin", sys.stdin.read())

    if not found:
        print(f"leakguard ({mode}): clean, checked {len(literals)} local values + {len(PATTERNS)} patterns",
              file=sys.stderr)
        return 0
    print(f"leakguard ({mode}): BLOCKED, {len(found)} finding(s):", file=sys.stderr)
    for f in found:
        print(f"  {f.where}: {f.rule}", file=sys.stderr)
    print("Fix these before committing/pushing. A pattern false positive can be allowed by adding\n"
          f"'{ALLOW_MARK}' to that line. Don't use --no-verify.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
