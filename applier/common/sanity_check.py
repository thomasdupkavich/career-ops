#!/usr/bin/env python3
"""Pre-submit sanity check for the career-ops applier.

Pure-Python deterministic checks. No LLM, no API calls. Fast and offline.
The Hermes skill at ~/.hermes/skills/career-ops-sanity-check/SKILL.md documents
the design and when this fires.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path

CAREER_OPS = Path.home() / "career-ops"
PROFILE = CAREER_OPS / "modes" / "_profile.md"
APPLICATIONS = CAREER_OPS / "data" / "applications.md"
SCORE_FLOOR = 4.0

# Strong-positive keywords (any 1+ in a role → eligible, all-zero → red flag)
STRONG_KEYWORDS = {
    "C#": [r"\bC#\b", r"\bC-?Sharp\b"],
    ".NET": [r"\b\.NET( Core)?\b", r"\bdotnet\b", r"\bASP\.?NET\b"],
    "Backend": [r"\bbackend\b", r"\bback-end\b", r"\bAPI\b", r"\bREST\b", r"\bWeb API\b"],
    "SQL Server": [r"\bSQL Server\b", r"\bMSSQL\b", r"\bT-?SQL\b"],
    "EF": [r"\bEntity Framework\b", r"\bEF Core\b"],
    "Enterprise": [r"\benterprise\b", r"\bmanufacturing\b", r"\blogistics\b"],
    "UI tech": [r"\bKendo\b", r"\bTelerik\b", r"\bdashboard\b"],
    "DevOps": [r"\bAzure DevOps\b", r"\bAzure Pipelines\b"],
}

# Weak signals — red-flag patterns
WEAK_SIGNALS = [
    (re.compile(r"\bpure (front|frontend|front-end)\b|\bfrontend only\b|\bfrontend developer\b(?!.*(\.NET|backend|C#))", re.I),
     "pure-frontend role with no backend / .NET pairing"),
    (re.compile(r"\b(iOS|Android|mobile)( developer| engineer)\b(?!.*(\.NET|full[- ]?stack))", re.I),
     "mobile-only role"),
    (re.compile(r"\b(blockchain|web3|crypto|smart contract|solidity)\b", re.I),
     "blockchain/web3 role"),
    (re.compile(r"\bunpaid\b|\bequity[- ]?only\b|\bvolunteer\b", re.I),
     "unpaid / equity-only position"),
    (re.compile(r"\bjunior\b|\bentry[- ]?level\b|\bgraduate\b|\bnew[- ]?grad\b", re.I),
     "junior / entry-level role (user is senior)"),
    (re.compile(r"\bheavy Java\b|\bJava[- ]?only\b|\bsenior Java\b(?!.*(\.NET|C#))", re.I),
     "Java-heavy role without .NET bridge"),
    (re.compile(r"\bpython[- ]?only\b|\bsenior python\b(?!.*(\.NET|C#))", re.I),
     "Python-only role without .NET bridge"),
]

# Comp red-flag
COMP_LOW_PATTERN = re.compile(
    r"\$\s*([4-9]\d|\d{2}(?:[\.,]\d)?)\s*[Kk]?\s*[-–]\s*\$?\s*([5-9]\d|\d{2}(?:[\.,]\d)?)\s*[Kk]?",
)
COMP_FLOOR_K = 100  # JD with explicit < $100K range → skip

# Location red-flag
ONSITE_OK_AREAS = re.compile(r"\b(huntington|long island|nyc|new york|nassau|suffolk|queens|brooklyn)\b", re.I)
REMOTE_OK = re.compile(r"\bremote\b|\bhybrid\b|\bwork[- ]?from[- ]?home\b|\bWFH\b", re.I)
ONSITE_FLAG = re.compile(r"\bon[- ]?site only\b|\bmust be on[- ]?site\b|\bon[- ]?premise\b", re.I)


@dataclass
class Verdict:
    go: bool
    should_ask_user: bool
    reasons: list[str] = field(default_factory=list)
    telegram_message: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _company_recently_applied(company: str, applications_md: str, days: int = 30) -> tuple[bool, str | None]:
    """Returns (already_applied, date_str)."""
    if not company:
        return False, None
    cutoff = datetime.now() - timedelta(days=days)
    company_norm = company.strip().lower()
    last_date = None
    for raw in applications_md.splitlines():
        if company_norm and company_norm in raw.lower():
            m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%Y-%m-%d")
                    if d >= cutoff:
                        return True, m.group(1)
                    if not last_date or d > last_date:
                        last_date = d
                except ValueError:
                    pass
    return False, last_date.strftime("%Y-%m-%d") if last_date else None


def _check_score(job: dict) -> tuple[bool, str | None]:
    score = job.get("score")
    if score is None:
        return True, None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return True, None
    if s < SCORE_FLOOR:
        return False, f"score {s:.1f} below {SCORE_FLOOR} floor"
    return True, None


def _check_weak_signals(jd_text: str) -> tuple[bool, str | None]:
    for pat, label in WEAK_SIGNALS:
        if pat.search(jd_text):
            return False, label
    return True, None


def _check_keyword_coverage(jd_text: str) -> tuple[int, list[str]]:
    hits = []
    for label, pats in STRONG_KEYWORDS.items():
        for p in pats:
            if re.search(p, jd_text, re.I):
                hits.append(label)
                break
    return len(hits), hits


def _check_comp(jd_text: str) -> tuple[bool, str | None]:
    m = COMP_LOW_PATTERN.search(jd_text)
    if not m:
        return True, None
    try:
        low = float(m.group(1).replace(",", "."))
        high = float(m.group(2).replace(",", "."))
    except ValueError:
        return True, None
    # Normalize K
    if low < 10:
        low *= 100  # "100-130K" interpreted variably
    if high < 10:
        high *= 100
    if high < COMP_FLOOR_K:
        return False, f"comp range below ${COMP_FLOOR_K}K (${low:.0f}K–${high:.0f}K)"
    return True, None


def _check_location(jd_text: str) -> tuple[bool, str | None]:
    if REMOTE_OK.search(jd_text):
        return True, None
    if ONSITE_FLAG.search(jd_text) and not ONSITE_OK_AREAS.search(jd_text):
        return False, "on-site only outside Long Island / NYC area"
    return True, None


def check_application(job: dict, applications_md: str | None = None) -> Verdict:
    """
    Main entry point. Returns a Verdict.

    job dict expected keys (any subset OK):
      title, company, url, score (float), description (raw JD text)
    """
    jd_text = f"{job.get('title','')} {job.get('description','')}"
    company = job.get("company", "")
    title = job.get("title", "?")
    apps_md = applications_md if applications_md is not None else _read_text(APPLICATIONS)

    # 1. Score floor — silent skip
    ok, reason = _check_score(job)
    if not ok:
        return Verdict(go=False, should_ask_user=False, reasons=[reason or "low score"])

    # 2. Weak signals — surface
    ok, reason = _check_weak_signals(jd_text)
    if not ok:
        return Verdict(
            go=False,
            should_ask_user=True,
            reasons=[reason or "weak signal"],
            telegram_message=f"Skipping {title} at {company}: {reason}. 👎 to confirm, otherwise applying in 5m.",
        )

    # 3. Duplicate company in last 30d
    already, when = _company_recently_applied(company, apps_md, days=30)
    if already:
        return Verdict(
            go=False,
            should_ask_user=True,
            reasons=[f"already applied to {company} on {when}"],
            telegram_message=f"Skipping {title} at {company} — already applied on {when}. 👍 if you want to apply anyway.",
        )

    # 4. Keyword coverage
    hits, hit_list = _check_keyword_coverage(jd_text)
    score = float(job.get("score", 0) or 0)
    if hits == 0 and score < 4.5:
        return Verdict(
            go=True,
            should_ask_user=True,
            reasons=["zero strong-keyword hits in JD"],
            telegram_message=f"Heads up: {title} at {company} matches none of your strong keywords (.NET / SQL Server / Enterprise). Score {score:.1f}. 👎 to skip, otherwise applying in 5m.",
        )

    # 5. Comp
    ok, reason = _check_comp(jd_text)
    if not ok:
        return Verdict(
            go=False,
            should_ask_user=True,
            reasons=[reason or "comp too low"],
            telegram_message=f"Skipping {title} at {company}: {reason}. 👍 to apply anyway.",
        )

    # 6. Location
    ok, reason = _check_location(jd_text)
    if not ok:
        return Verdict(
            go=False,
            should_ask_user=True,
            reasons=[reason or "location mismatch"],
            telegram_message=f"Skipping {title} at {company}: {reason}. 👍 to apply anyway.",
        )

    # Clean — silent go
    return Verdict(go=True, should_ask_user=False, reasons=[f"clean (keywords: {','.join(hit_list) or 'none, score-driven'})"])


# Self-test
if __name__ == "__main__":
    import sys
    cases = [
        # Should-go: clean .NET role
        {"title": "Senior .NET Developer", "company": "Acme", "score": 4.5,
         "description": "C#, ASP.NET Core, SQL Server, REST API for manufacturing dashboard. Remote OK. $130K-$160K."},
        # Should-fail: weak signal (junior)
        {"title": "Junior Frontend Developer", "company": "Foo", "score": 4.5,
         "description": "Entry-level React position. Pure frontend. $70K."},
        # Should-skip: score floor
        {"title": "Whatever", "company": "Bar", "score": 3.5,
         "description": ".NET role"},
        # Should-flag: zero keywords
        {"title": "Senior Engineer", "company": "Baz", "score": 4.3,
         "description": "We need a senior engineer to work on Ruby and Go services."},
    ]
    for c in cases:
        v = check_application(c)
        print(f"  {c['title']:35} go={v.go!s:5} ask={v.should_ask_user!s:5} reasons={v.reasons}")
    sys.exit(0)
