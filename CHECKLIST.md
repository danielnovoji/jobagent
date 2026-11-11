# jobagent: status

Run `pytest` before and after every change. Details of how things work live in the code
and `CLAUDE.md`; this file only tracks what's done and what's left.

## What works

| Part | Status |
|---|---|
| Workday (Nvidia, Intel, KLA) | Working. Full pagination, real locations for multi-office jobs |
| Jobify | Working. Israel-only, descriptions from the job page |
| Microsoft, Apple | Working. Microsoft ~21 Israel roles, Apple ~140, full descriptions |
| Greenhouse (8 cos), Check Point, Amazon/Annapurna, 4 more Workday | Working since 2026-11-11, ~218 Israeli jobs pass the free filters |
| LinkedIn alert emails | Built and checked on 37 real emails, gets no input until the Gmail filter below is set |
| Filters | Location, seniority (English + Hebrew), 5+ years, student roles, non-engineering roles, foreign clearance |
| Scoring | Keyword (no key) or Claude Haiku 5.5 (with key) |
| Summary tailoring and CV PDF | One PDF per job, validated against `cv/master.yaml` facts |
| Telegram cards | Approve / Skip / Submitted, survive a restart |
| Apply by email | Recruiter address extracted, shown on the card, sent on Approve |
| Google Sheet tracker | Dedupe, append row, confirmed status updates |
| Inbox reply scan | Read-only, proposes a status, asks before changing the sheet |
| Notifications | Telegram + email, failed sends retried from the SQLite outbox |
| Scheduler + Docker files | Written. Docker image not built or run yet |

## Known gaps

- **Meta:** returns nothing from a server IP (its GraphQL query id changes with each deploy).
  Microsoft and Apple work since 2026-11-11. LinkedIn alerts showed 0 Apple/Microsoft jobs in 30 days,
  so don't count on alerts for big companies' own postings.
- **Claude scoring has not run against the live API.** It's tested with the real SDK against
  a fake server. The first live run is the real test.
- **Cardo:** no careers URL yet.
- **Past interviews outside the sheet** (list in `CLAUDE.local.md`): no skip list yet.

## Your action items

- [ ] **Decide on the Claude API key** and, if yes, set `ANTHROPIC_API_KEY` in `.env`.
      The API is billed separately from a Claude subscription.
- [ ] **Gmail filter:** `From: jobalerts-noreply@linkedin.com` → apply label `job-alerts`.
      The label exists and alerts already arrive daily, they just don't reach the label yet.
      Applying it to old mail is safe: the agent only reads alerts from the last 7 days.
- [ ] **Docker:** install it on the server, then `docker compose up --build -d`.
- [ ] **Go live:** set `DRY_RUN=false` in `.env`. While it's `true`, Approve does nothing.

## Manual test cycle

`python diag_fetch.py` shows per-source counts and top scores. It only fetches, with no
writes to the DB, sheet or Telegram.
