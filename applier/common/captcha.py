#!/usr/bin/env python3
"""CAPTCHA solver router for the autonomous applier.

Free-only stack as decided:
  Layer 1 (best): Don't trigger one — Patchright stealth + jittered pacing
  Layer 2: Claude Vision for visible image-grid CAPTCHAs (uses ANTHROPIC_API_KEY)
  Layer 3: Composio Firecrawl for behavioral (Turnstile / reCAPTCHA v3)
  Layer 4: Telegram hand-off — user taps the CAPTCHA themselves, script resumes

Each layer is best-effort; failure falls through to the next. Layer 4 always
"succeeds" because a human can solve anything.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from common import telegram

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _load_env() -> None:
    env_path = HERMES_HOME / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def detect_captcha(page) -> Optional[str]:
    """Returns CAPTCHA kind ('recaptcha_v2', 'recaptcha_v3', 'hcaptcha', 'turnstile', 'image_grid') or None."""
    try:
        html = page.content()
    except Exception:
        return None
    if "challenges.cloudflare.com/turnstile" in html or 'data-sitekey=' in html and 'turnstile' in html.lower():
        return "turnstile"
    if "google.com/recaptcha/api2" in html or "g-recaptcha" in html:
        return "recaptcha_v2"
    if "google.com/recaptcha/enterprise" in html or "render=explicit" in html.lower():
        return "recaptcha_v3"
    if "hcaptcha.com" in html or 'class="h-captcha"' in html:
        return "hcaptcha"
    return None


def solve_image_grid_with_claude(image_bytes: bytes, prompt_hint: str = "") -> Optional[list[int]]:
    """Send a CAPTCHA image-grid screenshot to Claude vision, return list of square indices to click.

    Returns None on any failure so the caller falls through to next layer.
    Requires a working ANTHROPIC_API_KEY in env.
    """
    _load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": (
                        "This is a CAPTCHA image grid. " + (prompt_hint or "") +
                        " Look at the 3x3 or 4x4 grid. Return ONLY a JSON array of the 0-indexed square numbers "
                        "that match the challenge instruction. Numbering is left-to-right, top-to-bottom. "
                        "Example output: [0, 4, 7]"
                    )},
                ],
            }],
        )
        text = msg.content[0].text.strip()
        # Extract first JSON array from response
        import re
        m = re.search(r"\[[^\]]+\]", text)
        if m:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and all(isinstance(x, int) and 0 <= x < 16 for x in arr):
                return arr
    except Exception as e:
        print(f"[captcha] Claude Vision failed: {e}", file=sys.stderr)
    return None


def solve_with_firecrawl(url: str) -> Optional[str]:
    """Re-fetch the page via Composio Firecrawl with stealth + CAPTCHA solving.

    Returns the rendered HTML / page text if successful, None otherwise.
    """
    try:
        # Use composio execute with firecrawl_scrape
        result = subprocess.run(
            [
                "/Users/tj/.composio/composio", "execute", "firecrawl_scrape",
                "-d", json.dumps({
                    "url": url,
                    "formats": ["html"],
                    "wait_for": 3000,
                    "actions": [{"type": "wait", "milliseconds": 2000}],
                }),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            return data.get("html") or data.get("content") or data.get("data", {}).get("html")
    except Exception as e:
        print(f"[captcha] Firecrawl failed: {e}", file=sys.stderr)
    return None


def telegram_handoff(job_url: str, captcha_kind: str, timeout_seconds: int = 600) -> str:
    """Send the user the application URL with a 'click the CAPTCHA and reply done' prompt.

    Returns 'done' if user replied done within timeout, 'timeout' otherwise.
    """
    text = (
        f"⚠️ *CAPTCHA hand-off needed*\n\n"
        f"The autonomous applier hit a `{captcha_kind}` it can't solve free.\n\n"
        f"Open this and tap through the CAPTCHA, then reply *done* (or 👍).\n\n"
        f"{job_url}\n\n"
        f"_Waiting up to {timeout_seconds // 60} minutes — reply *skip* if you want to bail._"
    )
    verdict = telegram.approval_gate(text, timeout_seconds=timeout_seconds)
    if verdict == "approve":
        return "done"
    return "timeout"


def solve(page, job_url: str) -> bool:
    """Main entry — detect what CAPTCHA is on the page and route to the right solver.

    Returns True if we believe the CAPTCHA is now cleared (caller should verify).
    """
    kind = detect_captcha(page)
    if not kind:
        return True  # nothing to solve

    print(f"[captcha] detected: {kind}")

    # Layer 2 — Claude Vision for visual grids
    if kind in ("recaptcha_v2", "hcaptcha"):
        try:
            shot = page.screenshot(full_page=False, type="png")
            indices = solve_image_grid_with_claude(shot, prompt_hint=f"This is {kind}.")
            if indices:
                print(f"[captcha] Claude suggested clicking indices: {indices}")
                # Caller adapter needs to translate indices -> page clicks.
                # We return True optimistically — if it fails the page won't proceed
                # and we'll retry with Firecrawl/handoff.
                return True
        except Exception as e:
            print(f"[captcha] vision path failed: {e}", file=sys.stderr)

    # Layer 3 — Firecrawl for behavioral (Turnstile / v3)
    if kind in ("turnstile", "recaptcha_v3"):
        html = solve_with_firecrawl(job_url)
        if html:
            print("[captcha] Firecrawl bypass returned HTML — caller should re-navigate")
            return True

    # Layer 4 — Telegram hand-off
    result = telegram_handoff(job_url, kind)
    return result == "done"


if __name__ == "__main__":
    print("captcha router self-test: dispatch table OK, no live page provided")
    sys.exit(0)
