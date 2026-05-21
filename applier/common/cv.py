#!/usr/bin/env python3
"""CV PDF finder — picks the right tailored CV for a job.

career-ops/output/ contains tailored CVs named like:
  cv-thomas-dupkavich-{company}-{title-slug}-{date}.pdf

This module resolves the best CV path for an (company, title) tuple,
falling back to the most-recent CV if no tailored match exists.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

CAREER_OPS = Path.home() / "career-ops"
OUTPUT_DIR = CAREER_OPS / "output"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def find_cv_for_job(company: str = "", title: str = "") -> Optional[Path]:
    """Find the best-matching CV PDF.

    Priority:
      1. Exact match on company + title slug
      2. Match on company alone (most recent)
      3. Match on title slug alone (most recent)
      4. Most-recent CV in output/
    """
    if not OUTPUT_DIR.exists():
        return None

    pdfs = sorted(OUTPUT_DIR.glob("cv-thomas-dupkavich-*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        return None

    company_slug = _slug(company)
    title_slug = _slug(title)

    # 1. Try exact-ish match
    if company_slug and title_slug:
        for p in pdfs:
            name = p.name.lower()
            if company_slug in name and any(word in name for word in title_slug.split("-") if len(word) > 3):
                return p

    # 2. Company match only
    if company_slug:
        for p in pdfs:
            if company_slug in p.name.lower():
                return p

    # 3. Title match only
    if title_slug:
        for p in pdfs:
            name = p.name.lower()
            if any(word in name for word in title_slug.split("-") if len(word) > 3):
                return p

    # 4. Most recent
    return pdfs[0]


def needs_tailoring(cv_path: Path, max_age_hours: int = 168) -> bool:
    """Returns True if the chosen CV is older than a week — signal to regen via career-ops."""
    if not cv_path or not cv_path.exists():
        return True
    age_hours = (time.time() - cv_path.stat().st_mtime) / 3600
    return age_hours > max_age_hours


if __name__ == "__main__":
    import sys
    p = find_cv_for_job(company="coreweave", title="senior business systems engineer")
    print(f"Tailored match: {p}")
    p = find_cv_for_job(company="acme", title="senior .NET developer")
    print(f"Fallback (no tailored): {p}")
    if p:
        print(f"  Needs re-tailoring? {needs_tailoring(p)}")
    sys.exit(0)
