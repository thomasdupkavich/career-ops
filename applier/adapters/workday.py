#!/usr/bin/env python3
"""Workday ATS adapter — SKELETON.

Workday is the hard one. Each tenant customizes the form layout, multi-page flow is the norm,
2FA / email verification is common, and anti-bot is aggressive. This skeleton:
  - Detects Workday URLs
  - Opens the page and surfaces it to the user via Telegram with a hand-off link
  - Records the attempt as "skipped — needs manual click"

Real Workday submission requires:
  - Account creation flow (multi-step)
  - Email 2FA reading from ~/.hermes/cache/2fa-recent.json (Phase 2 watcher already feeds this)
  - Per-page form-fill (3-5 pages of resume sections, work history, voluntary disclosures)
  - Final submit click

We'll build this out in a dedicated Phase 5 session against a real target.
"""
from __future__ import annotations

import re
from common import telegram
from common.logger import log_attempt

ATS_NAME = "workday"


def detect(url: str) -> bool:
    u = (url or "").lower()
    return ("myworkdayjobs.com" in u or "wd1.myworkdaysite.com" in u or "workday.com/external" in u)


def apply(url: str, *, dry_run: bool = True, skip_sanity: bool = False) -> dict:
    company = ""
    m = re.search(r"//([^.]+)\.(?:wd\d+\.)?myworkdayjobs\.com", url)
    if m:
        company = m.group(1).replace("-", " ").title()

    telegram.notify(
        f"⚠️ *Workday hand-off* — {company or 'this job'}\n\n"
        f"Workday auto-apply isn't built yet. Open the link, complete the form yourself.\n\n"
        f"{url}\n\n"
        f"Phase 5 will tackle Workday with full 2FA pickup from the Gmail watcher."
    )

    log_attempt(
        job_url=url, ats=ATS_NAME, company=company, title="(workday)",
        score=None, sanity_verdict="skipped_workday_handoff",
        sanity_reasons=["Workday adapter not built — hand-off to user"],
        submitted=False, submit_error="workday adapter is a skeleton (Phase 5 work)",
    )

    return {
        "ats": ATS_NAME, "url": url, "company": company,
        "dry_run": dry_run, "submitted": False,
        "submit_error": "workday adapter is a skeleton — user hand-off only",
        "warnings": ["Phase 5 will implement Workday with 2FA pickup"],
    }
