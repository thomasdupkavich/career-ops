#!/usr/bin/env python3
"""Greenhouse ATS adapter.

Greenhouse uses a consistent job_application form structure across all customers
hosted at boards.greenhouse.io/<company>/jobs/<id>. Embedded portals also exist
(e.g. <company>.com/careers/...) but they iframe the same form, which Patchright
handles fine if we wait for the right frame.

Form fields are predictable:
  #first_name, #last_name, #email, #phone
  #resume (file input — accepts PDF)
  #cover_letter (file input — optional)
  input[id*="urls_attributes"] for LinkedIn/GitHub/portfolio URLs
  custom questions live under [id^="job_application_answers_attributes"]
  submit: #submit_app
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from patchright.sync_api import sync_playwright, Page, BrowserContext

from common import telegram, captcha
from common.profile import load as load_profile
from common.cv import find_cv_for_job
from common.sanity_check import check_application
from common.logger import log_attempt


ATS_NAME = "greenhouse"


def detect(url: str) -> bool:
    """Match boards.greenhouse.io and embedded greenhouse forms."""
    url = (url or "").lower()
    return (
        "boards.greenhouse.io" in url
        or "boards-api.greenhouse.io" in url
        or "greenhouse.io/embed" in url
    )


def _extract_job_meta(page: Page) -> dict:
    """Pull title/company/description from a Greenhouse job page."""
    title = ""
    company = ""
    description = ""
    try:
        title = (page.locator("h1, .app-title").first.text_content(timeout=2000) or "").strip()
    except Exception:
        pass
    try:
        company = (page.locator(".company-name, header .company").first.text_content(timeout=2000) or "").strip()
    except Exception:
        # Fallback: scrape from URL path: boards.greenhouse.io/{company}/jobs/{id}
        m = re.search(r"boards\.greenhouse\.io/([^/]+)/", page.url)
        if m:
            company = m.group(1).replace("-", " ").title()
    try:
        description = (page.locator("#content, .app-body, .opening").first.text_content(timeout=2000) or "").strip()
    except Exception:
        pass
    return {"title": title, "company": company, "description": description[:8000], "url": page.url}


def _fill_form(page: Page, profile: dict, cv_path: Path) -> list[str]:
    """Fill the standard Greenhouse application form. Returns list of warnings."""
    warnings = []

    # Wait for the form to be present
    page.wait_for_selector("#first_name, input[name='first_name'], form#main_fields", timeout=10000)

    # Standard fields — try multiple selectors per field for robustness
    def fill_one(selectors: list[str], value: str, label: str):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                loc.fill(value, timeout=3000)
                return
            except Exception:
                continue
        warnings.append(f"could not fill {label} ({selectors[0]})")

    fill_one(["#first_name", "input[name='first_name']", "input[autocomplete='given-name']"], profile["first_name"], "first_name")
    fill_one(["#last_name", "input[name='last_name']", "input[autocomplete='family-name']"], profile["last_name"], "last_name")
    fill_one(["#email", "input[type='email']", "input[name='email']"], profile["email"], "email")
    fill_one(["#phone", "input[type='tel']", "input[name='phone']"], profile["phone"], "phone")

    # Resume upload
    try:
        resume_input = page.locator("input[type='file'][name*='resume'], input#resume, input[type='file']").first
        if resume_input.count() > 0:
            resume_input.set_input_files(str(cv_path))
        else:
            warnings.append("no resume upload input found")
    except Exception as e:
        warnings.append(f"resume upload failed: {e}")

    # URL fields (LinkedIn / GitHub / portfolio) — Greenhouse uses
    # input[name="job_application[urls_attributes][N][value]"]
    url_inputs = page.locator("input[name*='urls_attributes'][name*='[value]']")
    n_url = url_inputs.count()
    for i in range(n_url):
        try:
            inp = url_inputs.nth(i)
            label_text = ""
            # Try to find the associated label
            try:
                label_text = (inp.evaluate("el => el.closest('div')?.querySelector('label')?.textContent || ''") or "").lower()
            except Exception:
                pass
            if "linkedin" in label_text:
                inp.fill(profile["linkedin"], timeout=2000)
            elif "github" in label_text:
                inp.fill(profile["github"], timeout=2000)
            elif "portfolio" in label_text or "website" in label_text:
                if profile.get("portfolio"):
                    inp.fill(profile["portfolio"], timeout=2000)
        except Exception:
            pass

    # Common yes/no compliance questions Greenhouse often asks:
    #   "Are you legally authorized to work in the US?" → Yes
    #   "Do you require visa sponsorship?" → No
    _handle_yes_no(page, ["legally authorized", "authorized to work"], yes=True, warnings=warnings)
    _handle_yes_no(page, ["sponsorship", "require visa"], yes=False, warnings=warnings)

    return warnings


def _handle_yes_no(page: Page, keywords: list[str], yes: bool, warnings: list[str]):
    """Find question containing any keyword, click Yes or No radio/select."""
    try:
        # Strategy: find any <label> matching keyword, then click the appropriate radio
        for kw in keywords:
            label = page.locator(f"label:has-text('{kw}'), .question:has-text('{kw}')").first
            if label.count() == 0:
                continue
            # Find sibling radios or a select
            parent = label.locator("xpath=ancestor::div[contains(@class,'field') or contains(@class,'question')][1]").first
            if parent.count() == 0:
                continue
            target_text = "Yes" if yes else "No"
            # Try select first
            try:
                select = parent.locator("select").first
                if select.count() > 0:
                    select.select_option(label=target_text)
                    return
            except Exception:
                pass
            # Try radio with matching label
            try:
                radio_label = parent.locator(f"label:has-text('{target_text}')").first
                if radio_label.count() > 0:
                    radio_label.click()
                    return
            except Exception:
                pass
        warnings.append(f"could not handle yes/no for: {keywords}")
    except Exception as e:
        warnings.append(f"yes/no handling exception: {e}")


def apply(url: str, *, dry_run: bool = True, skip_sanity: bool = False) -> dict:
    """Run the full apply flow.

    Returns: { ats, url, company, title, score, sanity_verdict, submitted, submit_error, warnings }
    """
    result = {
        "ats": ATS_NAME,
        "url": url,
        "dry_run": dry_run,
        "submitted": False,
        "warnings": [],
        "submit_error": None,
    }

    profile = load_profile()

    with sync_playwright() as p:
        # Patchright with stealth channel=chrome
        # Dry run = HEADED (so you can watch it work). Submit = headless (background ops).
        browser = p.chromium.launch(headless=not dry_run)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Some greenhouse boards autoredirect to /apply — give it a beat
            page.wait_for_load_state("networkidle", timeout=10000)
            time.sleep(1)

            meta = _extract_job_meta(page)
            result.update(meta)

            # Sanity check
            if not skip_sanity:
                verdict = check_application({
                    "title": meta["title"],
                    "company": meta["company"],
                    "url": url,
                    "score": None,  # Phase 3 doesn't have a scorer yet
                    "description": meta["description"],
                })
                result["sanity_verdict"] = ("go" if verdict.go else "no_go")
                result["sanity_reasons"] = verdict.reasons
                if not verdict.go and not verdict.should_ask_user:
                    log_attempt(
                        job_url=url, ats=ATS_NAME,
                        company=meta["company"], title=meta["title"], score=None,
                        sanity_verdict="silent_skip", sanity_reasons=verdict.reasons,
                        submitted=False,
                    )
                    return result
                if verdict.should_ask_user and verdict.telegram_message:
                    gate = telegram.approval_gate(verdict.telegram_message, timeout_seconds=300)
                    if gate == "deny":
                        log_attempt(
                            job_url=url, ats=ATS_NAME,
                            company=meta["company"], title=meta["title"], score=None,
                            sanity_verdict="user_denied", sanity_reasons=verdict.reasons,
                            submitted=False,
                        )
                        return result
                    # 'approve' or 'timeout' → proceed (timeout defaults to go)

            # Pick the right CV
            cv_path = find_cv_for_job(company=meta["company"], title=meta["title"])
            if not cv_path:
                result["submit_error"] = "no CV PDF found in ~/career-ops/output/"
                return result
            result["cv_path"] = str(cv_path)

            # Open the apply form — many greenhouse pages already have it inline
            try:
                apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply')").first
                if apply_btn.count() > 0 and apply_btn.is_visible():
                    apply_btn.click()
                    page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            # Solve any CAPTCHA blocking the form
            if not captcha.solve(page, url):
                result["submit_error"] = "captcha could not be solved"
                return result

            # Fill
            warnings = _fill_form(page, profile, cv_path)
            result["warnings"].extend(warnings)

            # Pre-submit Telegram confirmation (the only "gate" — irreversibility check)
            confirm_msg = (
                f"📤 *About to submit Greenhouse application*\n\n"
                f"*{meta['title']}* at *{meta['company']}*\n"
                f"CV: `{cv_path.name}`\n"
                f"Warnings: {len(warnings)} field(s) — {warnings[:3] if warnings else 'none'}\n\n"
                f"👍 to send · 👎 to abort · 5min timeout = send"
            )
            if dry_run:
                result["submit_error"] = "dry_run — form is filled, nothing submitted"
                telegram.notify(
                    f"🧪 *DRY RUN complete* — {meta['title']} at {meta['company']}.\n"
                    f"Form filled successfully. Re-run with `--submit` to actually send.\n"
                    f"Warnings: {warnings or 'none'}"
                )
            else:
                gate = telegram.approval_gate(confirm_msg, timeout_seconds=300)
                if gate == "deny":
                    result["submit_error"] = "user denied via Telegram"
                else:
                    # Click submit
                    try:
                        submit = page.locator("#submit_app, input[type='submit'], button:has-text('Submit Application')").first
                        submit.click(timeout=5000)
                        page.wait_for_load_state("networkidle", timeout=20000)
                        # Look for success indicator
                        success_text = page.locator("body").text_content() or ""
                        if any(s in success_text.lower() for s in ("thank you for applying", "application received", "successfully")):
                            result["submitted"] = True
                            telegram.notify(f"✅ *Submitted* — {meta['title']} at {meta['company']}")
                        else:
                            result["submit_error"] = "submit clicked but no success indicator found"
                    except Exception as e:
                        result["submit_error"] = f"submit click failed: {e}"

            # Log to SQLite + applications.md
            log_attempt(
                job_url=url, ats=ATS_NAME,
                company=meta["company"], title=meta["title"], score=None,
                sanity_verdict=result.get("sanity_verdict", "skipped"),
                sanity_reasons=result.get("sanity_reasons", []),
                submitted=result["submitted"],
                submit_error=result.get("submit_error"),
                cv_path=str(cv_path),
            )
        finally:
            context.close()
            browser.close()

    return result


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("usage: python -m adapters.greenhouse <greenhouse-job-url> [--submit]")
        sys.exit(1)
    url = sys.argv[1]
    submit = "--submit" in sys.argv
    res = apply(url, dry_run=not submit)
    print(json.dumps(res, indent=2, default=str))
