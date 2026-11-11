# jobagent

Personal job-search agent for Daniel (the owner).
Finds relevant roles, scores them against his CV, tailors the CV per role, and asks for approval on Telegram
before anything is sent.

## Public repo: personal data stays out
This repo is public. Personal data lives only in gitignored files: `CLAUDE.local.md` (owner background, preferences,
honesty constraints, tracker id, where he applied), `cv/master.yaml` (the real CV; `cv/master.example.yaml` is a
fictional template), `.env`, `secrets/`, `data/`, `out/`.
- Never write contact details, sheet ids, chat ids, keys, or "applied/interviewed at X" into a committed file.
  Use placeholders (`you@gmail.com`, Acme/Globex/Initech) in tests, fixtures and examples.
- `scripts/leakguard.py` runs as pre-commit and pre-push hooks (`git config core.hooksPath .githooks`). It blocks
  secret files, key patterns, every value in the local `.env` / `secrets/*.json`, the CV's phone and email,
  anything in `.leakguard-denylist`, and pushes whose author email isn't a GitHub noreply address.
  Never bypass it with `--no-verify`; fix the finding instead.

## Decisions already made (do not relitigate without asking)
- **Stack:** Python, runs in Docker on Daniel's Ubuntu server, on a schedule (24/7).
- **LLM:** Claude API (small model) for scoring + summary tailoring. Key in `.env` as `ANTHROPIC_API_KEY`.
- **Submit mode:** approval-gated. Telegram message per match with Approve / Skip buttons. Nothing is sent without Approve.
- **Location filter:** Israel + remote.
- **Notifications:** Telegram bot (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), each message shows match %, one-line reasoning, variant chosen, prep notes, apply link, tailored CV PDF.
- **Credentials:** only in `.env` on the server, never committed, never pasted into chats.

## CV system (built, see cv/ and jobagent/cv_render.py)
- `cv/master.yaml` is the single source of truth for every fact. Variants (systems, hft, embedded_linux, fullstack)
  only choose which projects/skill groups appear and in what order.
- Per role the agent may change ONLY `title` and `summary`. Everything else is fixed.
- The LLM writing a summary gets `allowed_facts_text()` as its only source and must not add skills, projects,
  numbers or claims that aren't in it. Validate output (no en/em dashes, <= 600 chars) before rendering.
- `verified: false` items are never rendered and never shown to the LLM.
- `needs_review` skills (Spring Boot, Java): add a "review before interview" prep note when a role leans on them.
- Renderer enforces: one page, no en/em dashes, intra-word non-breaking hyphens (ATS-safe), single column, no tables/images.
- Honesty constraints from Daniel: listed in `CLAUDE.local.md` and enforced in `prompts/` and `tests/test_cv_render.py`.
  Student-only roles are dropped by the title filter and are a hard blocker in scoring.
- Render: `python -m jobagent.cv_render <variant> [--title ...] [--summary ...] [--stem ...]` (needs LibreOffice for PDF).

## Repo map
- `cv/master.yaml` (real, gitignored), `cv/master.example.yaml` (fictional template), `jobagent/cv_render.py`: CV system (done).
- `scripts/leakguard.py`, `.githooks/`: pre-commit / pre-push leak guard (see "Public repo" above).
- `prompts/score.md`, `prompts/tailor_summary.md`, `prompts/interview_prep.md`: LLM prompts, including the post-validation rules for summaries. Use them as written; change only with Daniel's OK.
- `.env.example`: every setting the agent needs. Copy to `.env`.
- `tests/`: run `pytest` before and after changes. Add tests for each new module (sources: parse saved fixture responses, not live sites).
- Docker image (and the host, for local runs) must include `libreoffice-writer`, `poppler-utils`, and `fonts-crosextra-carlito` for PDF rendering. Carlito is the metric-compatible Calibri substitute; without it LibreOffice falls back to another font, the layout shifts, and the renderer rejects the CV as 2 pages. Also include Hebrew-capable fonts (e.g. `fonts-dejavu`, `fonts-noto`) for cover notes.

## Sources (to build)
| Source | Method | Notes |
|---|---|---|
| Nvidia, Intel, KLA, Palo Alto (+CyberArk), Applied Materials, Marvell, Unity | Workday JSON (`<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs`) | one generic Workday scraper, per-company config; confirm KLA host |
| Microsoft | Eightfold pcsx API (`apply.careers.microsoft.com/api/pcsx/search`, no login) | working 2026-11-11; URL carries `?job_number=<displayJobId>` because the link needs the internal id |
| Apple | jobs.apple.com/en-il/search?location=israel-ISR, JSON embedded in the page | working 2026-11-11, ~140 Israel roles |
| JFrog, SentinelOne, Wiz, Cato, Taboola, Payoneer, Riskified, Similarweb | Greenhouse boards API (`jobagent/sources/greenhouse.py`, one line per company) | working 2026-11-11 |
| Check Point | SmartRecruiters postings API (`country=il`) | working 2026-11-11 |
| Amazon / AWS / Annapurna Labs | amazon.jobs/en/search.json (description included) | working 2026-11-11 |
| HiBob, Playtika, monday, Wix, Mobileye, Elbit, IAI/Elta | no public feed (JS apps / login-walled API / bot block) | rely on LinkedIn company alerts |
| Cardo (+ any Comeet company) | Comeet careers API, token read from the hosted page (`jobagent/sources/comeet.py`) | working 2026-11-11 |
| Qualcomm | Eightfold pcsx API, same module as Microsoft (`SITES` in `jobagent/sources/microsoft.py`) | working 2026-11-11 |
| Meta | metacareers.com (GraphQL, harder) | |
| Jobify (jobify360.co.il) | public listing pages, no API | aggregator, overlaps with others: dedupe |
| LinkedIn | NO scraping, NO auto-apply (ToS, account bans) | read LinkedIn job-alert emails via Gmail IMAP, label `job-alerts`; fetch public guest job page for the description at a low rate |

All sources normalize into one job record -> SQLite, dedupe on (company, normalized title, location) + URL.

## Application flow
1. Fetch -> filter (location, obvious seniority mismatch like Senior/Staff/Lead/5+ years) -> dedupe.
2. LLM score 0-100 against the best variant, flag hard blockers (years required, foreign clearance, hardware, degree).
   An Israeli security clearance is NOT a blocker (Daniel, 2026-11-11): he can obtain one.
3. Above threshold: tailor summary, render PDF, send Telegram card.
4. Approve: if posting has a recruiter email -> send via Gmail SMTP (app password) with CV + short cover note.
   Otherwise -> reply with CV + apply link for Daniel to submit himself (portals: Workday, LinkedIn, Meta, Apple).
4b. After each application (email sent / Submitted tapped): an interview prep PDF (topics to review, gaps,
   8-10 likely questions with answer points) on both channels, written in the background by `jobagent/prep.py`
   (Haiku, same small model as scoring: Daniel picked it over Opus after a side-by-side, ~$0.003 a sheet).
   Retried at the next fetch, up to 3 tries.
   Re-attached when a status changes to `התקבלה תשובה`.
5. Log every application and status in SQLite; periodic summary digest on Telegram AND email (same dual-channel rule as below).

## Application tracker (Google Sheet, existing)
- Daniel's existing tracker: a Google Sheet, id in `.env` as `TRACKER_SHEET_ID` (name and id in `CLAUDE.local.md`). Hebrew, right-to-left. Daniel also edits it by hand: never reorder,
  rename, or delete existing columns or rows. Append new rows with `valueInputOption=USER_ENTERED` so dates and the
  formula are parsed like typed input and the dropdown/colors apply (they're preset down to row 1000).
- Existing columns (A-H): `#` (formula `=ROW()-1`: write that same formula into column A of every new row, never a number), `תאריך הגשה`, `חברה`, `תפקיד`, `סטטוס`, `עדכון אחרון`, `תחום`, `מיקום / מקור`.
  Dates as DD/MM/YYYY.
- Status stages (Daniel, 2026-11-11), defined once in `jobagent/statuses.py`, in dropdown order: `הוגש`, `התקבלה תשובה`,
  `שיחת היכרות`, `מבחן / מטלה`, `מחכה לראיון` (his own wording), `הצעת עבודה`, `נדחה`, `ויתרתי`. Use only these.
  On start the agent extends the dropdown and adds a color rule for any stage without one; Daniel's original three
  color rules (conditional formatting on the status column) are never modified.
- Columns the agent adds after H (create headers if missing, same header style): `ציון התאמה` (match %),
  `גרסת CV` (variant), `ערוץ הגשה` (email / portal / LinkedIn), `קישור` (job URL), `הערות` (agent's one-line reasoning + prep notes),
  `מספר משרה` (the posting's own id from its URL, e.g. Workday JR number; Daniel calls it the most important column).
- Columns are found by header text, not letter, so Daniel can move them. A missing or renamed required header stops
  the agent with an alert rather than writing into the wrong cells.
- When a row is written:
  - email applications: right after the email is sent, status `הוגש`.
  - portal / LinkedIn applications: only after Daniel taps "Submitted" on the Telegram card (add that button),
    so the sheet never lists something he didn't actually submit.
- Status updates: the agent scans the Gmail inbox (headers first, bodies only from matching senders) for mail from
  companies with an open row (every stage except `נדחה` / `ויתרתי`). A sender matches by the company's own domain, or a
  hiring platform (Greenhouse, Workday, Comeet...) naming the company; the mail must be about an application. Telegram
  shows the email (from, subject, text) with a button per stage, the guess first; one tap sets the cell and
  `עדכון אחרון`. Never change the cell without the tap. A mail whose stage equals the current one is info only.
- Also used for dedupe: never send a card for a job already in the sheet, matched by `מספר משרה` or by company+role
  (hand-added rows have no number). Checked again, with a fresh read, right before Approve sends anything.
  Existing hand-added rows (listed in `CLAUDE.local.md`) count toward the per-company 30-day limit.
- **Every update on an applied job is sent on BOTH Telegram and email** (Daniel's explicit requirement).
  Events: applied (email sent / Submitted tapped), status proposed, status changed, recruiter reply detected,
  any change written to the sheet. Telegram for the instant alert + buttons; email as the durable record.
  - Email goes to `NOTIFY_EMAIL` via the same Gmail SMTP (sending to himself, free).
  - One Gmail thread per application: fixed subject `[jobagent] <Company> - <Role>` plus `In-Reply-To`/`References`
    headers to the first message, so the whole history of each job stacks in one thread.
  - Body: event, date, current status, match %, CV variant, job link, sheet link. Plain text, short.
  - Daniel can add a Gmail filter on `[jobagent]` to label them; keep the prefix stable.
  - If one channel fails, still deliver on the other and retry the failed one (log it); never drop an update silently.
- Access: Google Sheets API with a service account. Daniel shares the sheet with the service account email (Editor);
  JSON key path in `.env` as `GOOGLE_SERVICE_ACCOUNT_FILE`.

## Targeting
- Seniority: junior / entry level / associate. Drop Senior, Staff, Principal, Lead, Manager, and hard "5+ years" requirements.
  3+ years: score as a stretch, don't drop.
- Variant choice by role: C/C++, Linux, systems, networking, infrastructure -> `systems`; low-latency, trading, HFT -> `hft`;
  embedded Linux / firmware-adjacent user-space -> `embedded_linux` (roles requiring hardware/MCU/RTOS experience = low score);
  JS/TS, React, Node, web backend -> `fullstack`. Ambiguous backend roles: score against both systems and fullstack, keep the higher.
- Postings may be in Hebrew (Jobify, Israeli companies). Cover notes match the posting's language.

## Guard rails
- Never apply twice to the same job; dedupe against the applications table before sending a card.
- Max 2 applications per company per 30 days (applying to 10 roles at one company looks bad). Pick the best-scoring ones.
- Polite fetching: low request rate per host, real User-Agent, back off on 429/403, cache job descriptions.
- Daily cap on Telegram cards (default 10, best scores first) so the inbox stays usable.
- Every job is scored once: status in SQLite + cross-source dedupe (`models.company_key`) mean a job seen again, or
  from a second source, never reaches the model again. Bumps and reminders reuse stored data and cost no API calls.
- Bumps (Daniel, 2026-11-11): a card with no Approve/Skip gets a short joke reply under the original card every 4h,
  at most 3 times, only 09:00-22:00 Israel time (`run.run_bumps`). Cards without a stored Telegram message id are never bumped.
- Never log Telegram URLs: they contain the bot token (httpx logger is set to WARNING in `run.main`).
- Health (Daniel, 2026-11-11), all free: `/status` in Telegram (only his chat); a heartbeat every 10 min sends
  "back online after Xh" after a sleep/power-off and runs a catch-up fetch if a slot was missed (>13h since the last);
  messages when the charger is unplugged/connected and once below 20% battery. Power is read from /sys inside Docker.
- LinkedIn alert emails come from `jobalerts-noreply@linkedin.com`; Gmail filter labels them `job-alerts`, agent reads only that label.
- Outgoing email: from `GMAIL_ADDRESS`, plain text, short, with LinkedIn + GitHub in the signature, tailored PDF attached.

## Open questions (ask Daniel before building the related part)
- Match threshold for sending a card (suggest 60%).
- How often to fetch (suggest twice daily) and how often to send the summary digest (suggest daily evening).
- Cover note: English template + Hebrew template; Daniel should approve the wording once.

## Next steps
1. Telegram bot skeleton (send card, Approve/Skip callbacks) + `.env` loading.
2. Gmail IMAP reader for LinkedIn alerts.
2b. Google Sheet tracker client (read for dedupe, append rows, propose status updates).
3. Workday scraper (Nvidia first), then Microsoft, Apple, Jobify, Meta.
4. Scoring + tailoring with the Claude API.
5. Dockerfile + compose + scheduler.
