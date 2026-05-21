#!/usr/bin/env python3
"""User profile loader — parses career-ops/cv.md + modes/_profile.md + config/profile.yml.

Returns a structured dict the form-fillers use. Cached for the process lifetime.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path

CAREER_OPS = Path.home() / "career-ops"
CV_MD = CAREER_OPS / "cv.md"
PROFILE_MD = CAREER_OPS / "modes" / "_profile.md"
PROFILE_YML = CAREER_OPS / "config" / "profile.yml"


# Hard constants from the user (per Hermes memory)
CONSTANTS = {
    "first_name": "Thomas",
    "last_name": "Dupkavich",
    "full_name": "Thomas J. Dupkavich",
    "address_line1": "12 Oak Ave",
    "city": "Huntington Station",
    "state": "NY",
    "state_full": "New York",
    "zip": "11746",
    "country": "United States",
    "citizenship": "United States Citizen",
    "work_authorized": True,
    "require_sponsorship": False,
    "linkedin": "https://www.linkedin.com/in/thomasdupkavich/",
    "github": "https://github.com/thomasdupkavich",
    "portfolio": "",
    "salary_min_usd": 120000,
    "availability_weeks": 2,
    "willing_to_relocate": False,
    "preferred_work_arrangement": "Remote or Hybrid",
    "veteran_status": "I am not a veteran",
    "disability_status": "I do not have a disability",
    "gender": "Decline to state",
    "race": "Decline to state",
    "hispanic_latino": "Decline to state",
}


@functools.lru_cache(maxsize=1)
def load() -> dict:
    """Return one merged profile dict for use by adapters."""
    p = dict(CONSTANTS)

    # Extract email / phone from cv.md (more reliable than env)
    if CV_MD.exists():
        cv_text = CV_MD.read_text(encoding="utf-8")
        m = re.search(r"\*\*Email:\*\*\s*([^\s]+)", cv_text)
        if m:
            p["email"] = m.group(1).strip()
        m = re.search(r"\*\*Phone:\*\*\s*\(?(\d{3})\)?[\s-]*(\d{3})[\s-]*(\d{4})", cv_text)
        if m:
            p["phone"] = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
            p["phone_digits"] = m.group(1) + m.group(2) + m.group(3)
        # Summary — first non-empty para after "## Professional Summary"
        m = re.search(r"##\s+Professional Summary\s*\n+([^\n#]+)", cv_text)
        if m:
            p["summary"] = m.group(1).strip()
        p["cv_text"] = cv_text

    p.setdefault("email", "ThomasDupkavich@gmail.com")
    p.setdefault("phone", "(631) 338-2304")

    return p


if __name__ == "__main__":
    import json, sys
    profile = load()
    redacted = {k: v for k, v in profile.items() if k != "cv_text"}
    print(json.dumps(redacted, indent=2))
    sys.exit(0)
