# Scoring prompt

Used once per job, per candidate variant. Input placeholders: `{facts}` from `cv_render.allowed_facts_text(master, variant)`,
`{job}` = title, company, location, full description (any language).

Parse the reply with `json.loads`; on failure retry once, then skip the job and log it. Validate types and the caps
below in code too (don't trust the model to apply them).

Output fields:
- `score` (int 0-100)
- `seniority_ok` (bool): false if the role clearly targets senior/staff/lead or requires 5+ years
- `hard_blockers` (list of str): unmet MUST requirements such as years of experience, a foreign (non-Israeli) security clearance, degree level,
  specific hardware/RTOS/MCU experience, relocation, a language he lacks, current student enrollment
- `matched` (list of str, max 6): requirements the facts clearly satisfy
- `missing` (list of str, max 6): important requirements not in the facts
- `needs_review` (list of str): skills he lists but should refresh before an interview, if the role leans on them
- `reason` (str): one plain sentence explaining the score

---

You are screening a job posting for one candidate. Judge fit honestly. A wrong high score wastes an application;
a wrong low score hides a good one. The candidate is a junior engineer: a recent CS graduate with a
student internship and an intensive C/C++ training program. He has no full-time professional experience.
He has graduated and is not enrolled in any degree program, so a role for current students is a hard blocker.

Candidate facts (the ONLY things you may assume he has):
<facts>
{facts}
</facts>

Job posting:
<job>
{job}
</job>

Scoring guide:
- 80-100: core stack matches, junior-appropriate, no hard blockers.
- 60-79: good match with gaps he could close quickly; or a stretch (3+ years asked) with a strong stack match.
- 40-59: partial match; adjacent stack or several important gaps.
- 0-39: wrong stack, senior role, or a hard blocker.
Any hard blocker caps the score at 45. seniority_ok=false caps it at 30.
Do not reward keyword overlap alone: "C++" in a posting about C++ test automation is not a systems role.

`seniority_ok` is false if the role clearly targets senior/staff/lead or requires 5+ years.
An Israeli security clearance (סיווג ביטחוני) is never a blocker: he is an Israeli citizen and IDF veteran who can
obtain one. Only a clearance from another country, or another country's citizenship, is.
`hard_blockers` lists unmet MUST requirements: years of experience, a foreign security clearance, degree level,
specific hardware/RTOS/MCU experience, relocation, a language he lacks, or current student enrollment.
`matched` and `missing` hold at most 6 short items each.
`needs_review` lists skills from the facts that the role leans on and that he should refresh before an interview.

Reply with one JSON object and nothing else, exactly these keys:
{"score": 72, "seniority_ok": true, "hard_blockers": [], "matched": ["C++", "multithreading"], "missing": ["Kubernetes"], "needs_review": [], "reason": "Strong C++ and concurrency match for a junior role; lacks Kubernetes."}
