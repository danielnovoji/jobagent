# Interview prep prompt

Used once per application (email sent, or Submitted tapped), never per card. Placeholders: `{facts}`
(allowed_facts_text for the chosen variant), `{job}`, `{matched}`, `{missing}`, `{flagged}` (scoring's
needs_review list). The reply is JSON forced by a schema in `jobagent/prep.py`; list lengths are capped there too.

The sheet is for Daniel only and never reaches an employer, so general technical knowledge is fine.
Only answers about his own projects are limited to the facts, so he never rehearses a claim his CV doesn't make.

---

You are preparing a candidate for interviews for the job below. He has applied and says he is rusty on most
topics, so the sheet should refresh what interviewers for this role are likely to ask, in the order that
matters most. He is a junior engineer: a recent CS graduate with a student internship and an intensive
C/C++ training program, no full-time professional experience.

Candidate facts (what his CV says; interviewers may ask about any of it):
<facts>
{facts}
</facts>

From screening: requirements his CV matches: {matched}. Requirements his CV lacks: {missing}.
Skills screening flagged to refresh: {flagged}.

Job posting:
<job>
{job}
</job>

Write three things. The sheet is for him to read, so address him directly as "you" ("your thread pool",
"your internship"), never "he" or "the candidate".

role_focus: two or three sentences on what this role does day to day and what the interviews will most
likely probe.

topics: 6 to 10 topics to review, most important first. Favor topics that are both in the posting and on
his CV, because interviewers dig into what a CV claims. Include at most two topics the posting wants that
his CV lacks, framed as basics to learn. For each topic give a name, one sentence on why it matters for
this job, and 3 to 5 short points to refresh: the concepts, common pitfalls, and what he should be able to
explain or write on a whiteboard.

questions: 8 to 10 likely interview questions. Mostly technical questions on this role's stack, two or
three about his own projects or internship as they appear in the facts, and one behavioral question. For
each give the kind (technical, cv, or behavioral) and 3 to 5 short points a strong answer covers.

Rules:
- Answer points about his projects and internship use only the facts. Do not invent features, numbers,
  results, or scale.
- He has graduated and is not enrolled. Lcontrol was a student internship tested on local cameras, never
  production. He has no embedded hardware experience. Never suggest he claim otherwise.
- Technical points must be correct and specific: name the concept, the system call or API, the trade-off.
  Depth on fundamentals beats a long list of buzzwords.
- Plain English, short phrases, no markdown inside the strings.
