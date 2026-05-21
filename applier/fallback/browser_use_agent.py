#!/usr/bin/env python3
"""LLM-driven fallback for unknown ATS — uses browser-use with Claude.

Last resort when no deterministic adapter matches the URL. Slow, expensive
per-step (LLM call per browser action), capped at 12 steps total to keep
runaway loops in check.

Requires ANTHROPIC_API_KEY set in env. If not available, surfaces Telegram
hand-off instead.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from common import telegram
from common.profile import load as load_profile
from common.cv import find_cv_for_job
from common.sanity_check import check_application
from common.logger import log_attempt

ATS_NAME = "browser_use_fallback"
MAX_STEPS = 12


def detect(url: str) -> bool:
    # Fallback adapter — never claims as primary
    return False


def _build_task(url: str, profile: dict, cv_path: Path) -> str:
    return (
        f"You are an autonomous job-application agent. Navigate to {url}, find the apply form, "
        f"fill it with the following user data, and submit it.\n\n"
        f"User data:\n"
        f"  Full name: {profile['full_name']}\n"
        f"  First name: {profile['first_name']}\n"
        f"  Last name: {profile['last_name']}\n"
        f"  Email: {profile['email']}\n"
        f"  Phone: {profile['phone']}\n"
        f"  Address: {profile['address_line1']}, {profile['city']}, {profile['state']} {profile['zip']}\n"
        f"  LinkedIn: {profile['linkedin']}\n"
        f"  GitHub: {profile['github']}\n"
        f"  Resume PDF: {cv_path}\n"
        f"  Work authorization: US Citizen, no sponsorship needed\n"
        f"  Comp target: ${profile['salary_min_usd']:,}+\n"
        f"  Availability: {profile['availability_weeks']} weeks\n\n"
        f"For any compliance question:\n"
        f"  'Authorized to work in US?' → Yes\n"
        f"  'Require sponsorship?' → No\n"
        f"  'Veteran status?' → I am not a veteran\n"
        f"  'Disability?' → I do not have a disability\n"
        f"  'Race / Gender?' → Decline to state\n\n"
        f"STOP before clicking the final submit button. Report what's in the form so a human can verify."
    )


async def _run_browser_use(task: str) -> dict:
    """Run browser-use agent with bounded steps."""
    from browser_use import Agent
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", max_tokens=2048)
    agent = Agent(task=task, llm=llm, max_failures=3)
    history = await agent.run(max_steps=MAX_STEPS)
    return {
        "steps_taken": len(history.history),
        "final_result": history.final_result(),
        "errors": history.errors(),
    }


def apply(url: str, *, dry_run: bool = True, skip_sanity: bool = False) -> dict:
    result = {"ats": ATS_NAME, "url": url, "dry_run": dry_run, "submitted": False, "warnings": [], "submit_error": None}
    profile = load_profile()

    if not skip_sanity:
        v = check_application({"title": "", "company": "", "url": url, "description": "", "score": None})
        result["sanity_verdict"] = "go" if v.go else "no_go"
        result["sanity_reasons"] = v.reasons
        if not v.go and not v.should_ask_user:
            log_attempt(job_url=url, ats=ATS_NAME, company="", title="", score=None,
                        sanity_verdict="silent_skip", sanity_reasons=v.reasons, submitted=False)
            return result

    cv_path = find_cv_for_job(company="", title="")
    if not cv_path:
        result["submit_error"] = "no CV PDF found"
        return result
    result["cv_path"] = str(cv_path)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        result["submit_error"] = "ANTHROPIC_API_KEY missing — falling back to telegram handoff"
        telegram.notify(
            f"⚠️ *Unknown ATS — hand-off*\n\nNo deterministic adapter matched and the LLM fallback is unavailable "
            f"(no working ANTHROPIC_API_KEY).\n\n{url}\n\nOpen and apply yourself."
        )
        log_attempt(job_url=url, ats=ATS_NAME, company="", title="", score=None,
                    sanity_verdict="handoff", sanity_reasons=["no LLM key"], submitted=False,
                    submit_error="no api key", cv_path=str(cv_path))
        return result

    task = _build_task(url, profile, cv_path)
    if dry_run:
        result["submit_error"] = "dry_run — task built, browser-use not invoked"
        result["task_preview"] = task[:300]
        telegram.notify(f"🧪 LLM fallback DRY RUN for {url}. Task built ({len(task)} chars).")
        return result

    try:
        bu_result = asyncio.run(_run_browser_use(task))
        result["browser_use"] = bu_result
        # Browser-use returns the LLM's narrative; we don't auto-confirm "submitted"
        # because we instructed it NOT to click final submit. Telegram the user with the state.
        telegram.notify(
            f"🤖 *LLM fallback completed* — {url}\n\n"
            f"Steps: {bu_result.get('steps_taken')}\n"
            f"Result: {str(bu_result.get('final_result',''))[:300]}\n\n"
            f"Form is filled in headed browser. Manually verify and submit."
        )
        result["warnings"].append("LLM fallback: human must click final submit")
    except Exception as e:
        result["submit_error"] = f"browser-use error: {e}"

    log_attempt(job_url=url, ats=ATS_NAME, company="", title="", score=None,
                sanity_verdict=result.get("sanity_verdict", "skipped"),
                sanity_reasons=result.get("sanity_reasons", []),
                submitted=False, submit_error=result.get("submit_error"), cv_path=str(cv_path))
    return result
