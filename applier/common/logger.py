#!/usr/bin/env python3
"""Outcome log — SQLite + applications.md append.

Single source of truth for "what jobs did we apply to and how did they go".
Feeds the self-improvement loop in Phase 7 (DSPy GEPA reads this table).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

CAREER_OPS = Path.home() / "career-ops"
DECISIONS_DB = CAREER_OPS / "data" / "decisions.sqlite"
APPLICATIONS_MD = CAREER_OPS / "data" / "applications.md"

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_url TEXT NOT NULL UNIQUE,
    ats TEXT,
    company TEXT,
    title TEXT,
    score REAL,
    sanity_verdict TEXT,
    sanity_reasons TEXT,
    submitted INTEGER DEFAULT 0,
    submitted_at TEXT,
    submit_error TEXT,
    cv_path TEXT,
    response_received_at TEXT,
    response_type TEXT,            -- 'interview' | 'rejection' | 'recruiter' | null
    response_days_to_first INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);
CREATE INDEX IF NOT EXISTS idx_applications_submitted ON applications(submitted);
"""


def _conn() -> sqlite3.Connection:
    DECISIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DECISIONS_DB))
    c.executescript(SCHEMA)
    return c


def log_attempt(
    *,
    job_url: str,
    ats: str,
    company: str,
    title: str,
    score: Optional[float],
    sanity_verdict: str,
    sanity_reasons: list[str],
    submitted: bool,
    submit_error: Optional[str] = None,
    cv_path: Optional[str] = None,
) -> int:
    """Insert or update the application record. Returns row id."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO applications (job_url, ats, company, title, score, sanity_verdict, sanity_reasons, "
            "submitted, submitted_at, submit_error, cv_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_url) DO UPDATE SET "
            "  submitted=excluded.submitted, "
            "  submitted_at=excluded.submitted_at, "
            "  submit_error=excluded.submit_error, "
            "  sanity_verdict=excluded.sanity_verdict, "
            "  sanity_reasons=excluded.sanity_reasons, "
            "  cv_path=excluded.cv_path "
            "RETURNING id",
            (
                job_url, ats, company, title, score, sanity_verdict, ";".join(sanity_reasons or []),
                1 if submitted else 0,
                datetime.utcnow().isoformat() + "Z" if submitted else None,
                submit_error, cv_path,
            ),
        )
        row_id = cur.fetchone()[0]

    if submitted:
        _append_applications_md(company=company, title=title, url=job_url, ats=ats)
    return row_id


def log_response(job_url: str, response_type: str) -> None:
    """Update the response columns when Gmail watcher detects an inbound."""
    with _conn() as c:
        # Compute days_to_first as days since submitted_at
        row = c.execute("SELECT submitted_at FROM applications WHERE job_url = ?", (job_url,)).fetchone()
        days = None
        if row and row[0]:
            try:
                t0 = datetime.fromisoformat(row[0].rstrip("Z"))
                days = (datetime.utcnow() - t0).days
            except ValueError:
                days = None
        c.execute(
            "UPDATE applications SET response_received_at=?, response_type=?, response_days_to_first=? "
            "WHERE job_url = ? AND response_received_at IS NULL",
            (datetime.utcnow().isoformat() + "Z", response_type, days, job_url),
        )


def _append_applications_md(*, company: str, title: str, url: str, ats: str) -> None:
    """Append a line to data/applications.md so the career-ops tracker stays in sync."""
    APPLICATIONS_MD.parent.mkdir(parents=True, exist_ok=True)
    date_iso = datetime.now().strftime("%Y-%m-%d")
    line = f"- [{date_iso}] **{company}** — {title} ({ats}) — applied autonomously — {url}\n"
    if APPLICATIONS_MD.exists():
        existing = APPLICATIONS_MD.read_text()
        if url not in existing:
            APPLICATIONS_MD.write_text(existing.rstrip() + "\n" + line)
    else:
        APPLICATIONS_MD.write_text("# Applications\n\n" + line)


def summary_stats() -> dict:
    """Quick stats for the daily digest."""
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        submitted = c.execute("SELECT COUNT(*) FROM applications WHERE submitted=1").fetchone()[0]
        responses = c.execute("SELECT COUNT(*) FROM applications WHERE response_received_at IS NOT NULL").fetchone()[0]
        by_type = dict(c.execute(
            "SELECT response_type, COUNT(*) FROM applications WHERE response_type IS NOT NULL GROUP BY response_type"
        ).fetchall())
        last_7d = c.execute(
            "SELECT COUNT(*) FROM applications WHERE submitted=1 AND submitted_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
    return {
        "total_attempts": total,
        "total_submitted": submitted,
        "submitted_last_7d": last_7d,
        "responses_total": responses,
        "responses_by_type": by_type,
    }


if __name__ == "__main__":
    import json, sys
    print(json.dumps(summary_stats(), indent=2))
    sys.exit(0)
