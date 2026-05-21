#!/usr/bin/env python3
"""Lever ATS adapter.

URL: jobs.lever.co/<company>/<id> or jobs.eu.lever.co/<company>/<id>
Form: standard fields (name/email/phone/resume) + urls[LinkedIn]/urls[GitHub]/urls[Other]
Submit: button[type='submit']
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from patchright.sync_api import sync_playwright, Page

from common import telegram, captcha
from common.profile import load as load_profile
from common.cv import find_cv_for_job
from common.sanity_check import check_application
from common.logger import log_attempt

ATS_NAME = "lever"


def detect(url: str) -> bool:
    return "jobs.lever.co" in (url or "").lower() or "jobs.eu.lever.co" in (url or "").lower()


def _ensure_apply_page(page: Page, url: str) -> None:
    """If we're on the JD page, navigate to /apply."""
    if not page.url.endswith("/apply"):
        try:
            apply_btn = page.locator("a[href$='/apply'], a.postings-btn:has-text('Apply')").first
            if apply_btn.count() > 0:
                apply_btn.click()
                page.wait_for_load_state("networkidle", timeout=10000)
            else:
                page.goto(url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass


def _extract_meta(page: Page) -> dict:
    title = ""
    company = ""
    description = ""
    try:
        title = (page.locator(".posting-headline h2, .posting-name").first.text_content(timeout=2000) or "").strip()
    except Exception:
        pass
    m = re.search(r"jobs\.(?:eu\.)?lever\.co/([^/]+)/", page.url)
    if m:
        company = m.group(1).replace("-", " ").title()
    try:
        description = (page.locator(".posting-page, .section-wrapper").first.text_content(timeout=2000) or "").strip()
    except Exception:
        pass
    return {"title": title, "company": company, "description": description[:8000], "url": page.url}


def _fill_form(page: Page, profile: dict, cv_path: Path) -> list[str]:
    warnings = []

    page.wait_for_selector("input[name='name'], input[name='resume'], form", timeout=10000)

    # Lever has a single "name" field
    full_name = profile["full_name"]
    for sel, val, label in [
        ("input[name='name']", full_name, "name"),
        ("input[name='email']", profile["email"], "email"),
        ("input[name='phone']", profile["phone"], "phone"),
        ("input[name='urls[LinkedIn]']", profile["linkedin"], "linkedin"),
        ("input[name='urls[GitHub]']", profile["github"], "github"),
    ]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(val, timeout=3000)
        except Exception as e:
            warnings.append(f"{label} fill failed: {e}")

    # Resume
    try:
        resume = page.locator("input[name='resume'], input[type='file']").first
        if resume.count() > 0:
            resume.set_input_files(str(cv_path))
        else:
            warnings.append("no resume upload input")
    except Exception as e:
        warnings.append(f"resume upload: {e}")

    return warnings


def apply(url: str, *, dry_run: bool = True, skip_sanity: bool = False) -> dict:
    result = {"ats": ATS_NAME, "url": url, "dry_run": dry_run, "submitted": False, "warnings": [], "submit_error": None}
    profile = load_profile()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=dry_run, channel="chrome")
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=10000)

            meta = _extract_meta(page)
            result.update(meta)

            if not skip_sanity:
                v = check_application({"title": meta["title"], "company": meta["company"], "url": url, "description": meta["description"], "score": None})
                result["sanity_verdict"] = "go" if v.go else "no_go"
                result["sanity_reasons"] = v.reasons
                if not v.go and not v.should_ask_user:
                    log_attempt(job_url=url, ats=ATS_NAME, company=meta["company"], title=meta["title"], score=None,
                                sanity_verdict="silent_skip", sanity_reasons=v.reasons, submitted=False)
                    return result
                if v.should_ask_user and v.telegram_message:
                    if telegram.approval_gate(v.telegram_message, timeout_seconds=300) == "deny":
                        log_attempt(job_url=url, ats=ATS_NAME, company=meta["company"], title=meta["title"], score=None,
                                    sanity_verdict="user_denied", sanity_reasons=v.reasons, submitted=False)
                        return result

            _ensure_apply_page(page, url)

            cv_path = find_cv_for_job(company=meta["company"], title=meta["title"])
            if not cv_path:
                result["submit_error"] = "no CV PDF found"
                return result
            result["cv_path"] = str(cv_path)

            if not captcha.solve(page, url):
                result["submit_error"] = "captcha unsolved"
                return result

            warnings = _fill_form(page, profile, cv_path)
            result["warnings"].extend(warnings)

            if dry_run:
                result["submit_error"] = "dry_run — form filled, not submitted"
                telegram.notify(f"🧪 DRY RUN Lever — {meta['title']} at {meta['company']}. Warnings: {warnings or 'none'}")
            else:
                gate = telegram.approval_gate(
                    f"📤 *Submit Lever app?*\n*{meta['title']}* at *{meta['company']}*\nCV: `{cv_path.name}`\n👍 send · 👎 abort · 5min = send",
                    timeout_seconds=300,
                )
                if gate == "deny":
                    result["submit_error"] = "user denied"
                else:
                    try:
                        submit = page.locator("button[type='submit'], button:has-text('Submit')").first
                        submit.click(timeout=5000)
                        page.wait_for_load_state("networkidle", timeout=20000)
                        body = (page.locator("body").text_content() or "").lower()
                        if any(s in body for s in ("thanks for applying", "thank you", "application submitted")):
                            result["submitted"] = True
                            telegram.notify(f"✅ Submitted — {meta['title']} at {meta['company']}")
                        else:
                            result["submit_error"] = "submit clicked but no success text"
                    except Exception as e:
                        result["submit_error"] = f"submit failed: {e}"

            log_attempt(job_url=url, ats=ATS_NAME, company=meta["company"], title=meta["title"], score=None,
                        sanity_verdict=result.get("sanity_verdict", "skipped"),
                        sanity_reasons=result.get("sanity_reasons", []),
                        submitted=result["submitted"], submit_error=result.get("submit_error"), cv_path=str(cv_path))
        finally:
            context.close()
            browser.close()

    return result
