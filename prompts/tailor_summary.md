# Summary tailoring prompt

Used only for jobs that passed scoring. Placeholders: `{facts}` (allowed_facts_text for the chosen variant),
`{base_summary}` (the variant's summary from master.yaml), `{job}`, `{matched}` (from scoring).

After the model answers, VALIDATE in code before rendering (reject and fall back to `{base_summary}` on any failure):
- length <= 600 chars, no en/em dashes, no first person ("I", "my"), no numbers that aren't in `{facts}`;
- every technology named appears in `{facts}` (simple case-insensitive check against the facts text);
- no forbidden words: production, shipped, deployed, senior, expert, years of experience, embedded hardware, firmware;
- no "student", "undergraduate", "enrolled" or "studying", except "student intern" / "student internship" (he graduated).
Log every fallback so wording problems are visible.

---

Rewrite a CV summary so a recruiter for this specific role sees the relevant parts first.

Hard rules:
- Use ONLY facts from <facts>. Do not add skills, tools, projects, numbers, employers, or claims that are not there,
  even if the job asks for them. Missing requirements stay missing.
- Lcontrol was a student internship. Say "developed", never "production", "shipped", or "deployed".
- He has graduated and is not enrolled anywhere. Never call him a student or say he is studying, even if
  the job is a student position. "Student internship" for Lcontrol is the only allowed use of "student".
- No first person. No en or em dashes; use commas or plain hyphens.
- 3 to 4 sentences, at most 600 characters, English.
- Reorder and reword; mirror the job's terminology only where a fact genuinely matches it
  (e.g. the job says "concurrency", facts say "multithreading": you may say "concurrency").

<facts>
{facts}
</facts>

Current summary:
<base>
{base_summary}
</base>

Requirements already confirmed as matched: {matched}

Job:
<job>
{job}
</job>

Return only the new summary text.
