#!/usr/bin/env python3
"""One-shot fetch diagnostic: per-source counts, filter breakdown, top-10 scores.

Read-only — nothing written to DB, sheet, or Telegram.

Usage:
    python diag_fetch.py
"""
from __future__ import annotations

import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from jobagent.filters import (
    passes_clearance,
    passes_description_seniority,
    passes_location,
    passes_title_seniority,
)
from jobagent.http_client import HttpClient
from jobagent.keyword_scorer import keyword_score_job
from jobagent.models import JobRecord

from jobagent.sources.amazon import fetch as amazon_fetch
from jobagent.sources.apple import fetch as apple_fetch
from jobagent.sources.comeet import fetch as comeet_fetch
from jobagent.sources.greenhouse import fetch as greenhouse_fetch
from jobagent.sources.smartrecruiters import fetch as smartrecruiters_fetch
from jobagent.sources.jobify import fetch as jobify_fetch
from jobagent.sources.meta import fetch as meta_fetch
from jobagent.sources.microsoft import fetch as ms_fetch
from jobagent.sources.workday import COMPANIES as WORKDAY_COMPANIES
from jobagent.sources.workday import fetch_company as workday_fetch
from jobagent.sources.workday import fetch_description as workday_fetch_desc


@dataclass
class SourceResult:
    fetched: int = 0
    error: str = ""


@dataclass
class FilterReason:
    location: int = 0
    title_seniority: int = 0
    desc_seniority: int = 0
    clearance: int = 0


def _filter_reason(job: JobRecord) -> Optional[str]:
    if not passes_location(job.location):
        return "location"
    if not passes_title_seniority(job.title):
        return "title_seniority"
    if job.description and not passes_description_seniority(job.description):
        return "desc_seniority"
    if job.description and not passes_clearance(job.description, job.location):
        return "clearance"
    return None


def _fetch_source(client: HttpClient, name: str, fn) -> tuple[list[JobRecord], str]:
    try:
        jobs = fn(client)
        return jobs, ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def main() -> None:
    print("=== jobagent fetch diagnostic ===\n")

    sources: dict[str, SourceResult] = {}
    all_jobs: list[JobRecord] = []

    with HttpClient(delay=1.0) as client:
        # Workday companies
        for cfg in WORKDAY_COMPANIES:
            name = f"workday:{cfg.company}"
            print(f"  fetching {name} ...", flush=True)
            jobs, err = _fetch_source(client, name, lambda c, cfg=cfg: workday_fetch(c, cfg))
            sources[name] = SourceResult(fetched=len(jobs), error=err)
            if err:
                print(f"    ERROR: {err}")
            else:
                print(f"    {len(jobs)} jobs")
            all_jobs.extend(jobs)

        # Other sources
        other = [
            ("microsoft", ms_fetch),
            ("apple", apple_fetch),
            ("greenhouse", greenhouse_fetch),
            ("smartrecruiters", smartrecruiters_fetch),
            ("amazon", amazon_fetch),
            ("comeet", comeet_fetch),
            ("jobify", jobify_fetch),
            ("meta", meta_fetch),
        ]
        for name, fn in other:
            print(f"  fetching {name} ...", flush=True)
            jobs, err = _fetch_source(client, name, fn)
            sources[name] = SourceResult(fetched=len(jobs), error=err)
            if err:
                print(f"    ERROR: {err}")
            else:
                print(f"    {len(jobs)} jobs")
            all_jobs.extend(jobs)

    total = len(all_jobs)
    print(f"\n--- FETCH SUMMARY ({total} total) ---")
    for src, res in sources.items():
        status = f"ERROR: {res.error}" if res.error else f"{res.fetched} jobs"
        print(f"  {src:<30} {status}")

    # --- Pre-filter (title + location, no description yet) ---
    print(f"\n--- FILTER BREAKDOWN ---")
    filter_counts: dict[str, int] = defaultdict(int)
    pre_passing: list[JobRecord] = []
    for job in all_jobs:
        if not passes_location(job.location):
            filter_counts["location"] += 1
        elif not passes_title_seniority(job.title):
            filter_counts["title_seniority"] += 1
        else:
            pre_passing.append(job)

    # --- Enrich with full descriptions for Workday jobs ---
    workday_jobs = [j for j in pre_passing if j.source.startswith("workday:")]
    if workday_jobs:
        print(f"  fetching full descriptions for {len(workday_jobs)} Workday jobs ...", flush=True)
        with HttpClient(delay=1.0) as client:
            for j in workday_jobs:
                desc = workday_fetch_desc(client, j)
                if desc:
                    j.description = desc
        print(f"  done")

    # --- Post-filter (description seniority + clearance) ---
    passing: list[JobRecord] = []
    for job in pre_passing:
        if job.description and not passes_description_seniority(job.description):
            filter_counts["desc_seniority"] += 1
        elif job.description and not passes_clearance(job.description, job.location):
            filter_counts["clearance"] += 1
        else:
            passing.append(job)

    print(f"  passed:               {len(passing)}")
    if filter_counts.get("location"):
        print(f"  dropped/location:     {filter_counts['location']}")
    if filter_counts.get("title_seniority"):
        print(f"  dropped/title:        {filter_counts['title_seniority']}")
    if filter_counts.get("desc_seniority"):
        print(f"  dropped/desc_yrs:     {filter_counts['desc_seniority']}")
    if filter_counts.get("clearance"):
        print(f"  dropped/clearance:    {filter_counts['clearance']}")

    # Show some examples of what was filtered
    location_dropped = [j for j in all_jobs if not passes_location(j.location)]
    if location_dropped:
        print(f"\n  Location-dropped examples (first 5):")
        for j in location_dropped[:5]:
            print(f"    {j.company}: {j.title!r} @ {j.location!r}")

    title_dropped = [
        j for j in all_jobs
        if passes_location(j.location) and not passes_title_seniority(j.title)
    ]
    if title_dropped:
        print(f"\n  Title-dropped examples (first 5):")
        for j in title_dropped[:5]:
            print(f"    {j.company}: {j.title!r}")

    # --- Score passing jobs ---
    print(f"\n--- SCORING {len(passing)} passing jobs ---")
    scored: list[tuple[int, str, str, str]] = []  # (score, variant, company, title)
    score_errors = 0
    for job in passing:
        try:
            result = keyword_score_job(job)
            if result:
                scored.append((result.score, result.variant, job.company, job.title))
            else:
                score_errors += 1
        except Exception as exc:
            score_errors += 1
            print(f"  score error for {job.company}/{job.title}: {exc}")

    scored.sort(reverse=True)
    print(f"  scored: {len(scored)}  errors: {score_errors}")

    threshold = 10  # matches _NO_LLM_THRESHOLD in run.py
    above = [(s, v, c, t) for s, v, c, t in scored if s >= threshold]
    below = len(scored) - len(above)
    print(f"  above threshold ({threshold}): {len(above)}")
    print(f"  below threshold ({threshold}): {below}")

    print(f"\n--- TOP 10 SCORES ---")
    if not scored:
        print("  (no scores — all sources returned 0 jobs or all were filtered)")
    for i, (score, variant, company, title) in enumerate(scored[:10], 1):
        print(f"  {i:2d}. {score:3d}%  [{variant:<15}]  {company}: {title}")

    if len(scored) > 10:
        print(f"\n  ... {len(scored) - 10} more scored jobs (all below rank 10)")

    print(f"\n=== done ===")


if __name__ == "__main__":
    main()
