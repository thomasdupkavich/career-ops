#!/usr/bin/env python3
"""Daily job-search digest for Telegram.

Reads career-ops state, summarizes via Anthropic Haiku, returns a tight Telegram-ready message.
Designed to be called by a Hermes cron job (or directly: `uv run python -m common.digest`).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

CAREER_OPS = Path.home() / "career-ops"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

PIPELINE = CAREER_OPS / "data" / "pipeline.md"
APPLICATIONS = CAREER_OPS / "data" / "applications.md"
SCAN_HISTORY = CAREER_OPS / "data" / "scan-history.tsv"
LEGACY_JOBS_LAST = HERMES_HOME / "job-search" / "jobs-last.json"

MODEL = "claude-haiku-4-5-20251001"


def _load_env() -> None:
    env_path = HERMES_HOME / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _pipeline_pending() -> list[str]:
    if not PIPELINE.exists():
        return []
    lines = []
    in_pending = False
    for raw in PIPELINE.read_text().splitlines():
        if raw.lstrip().startswith("## Pending"):
            in_pending = True
            continue
        if raw.lstrip().startswith("##") and in_pending:
            break
        if in_pending and re.match(r"\s*-\s*\[\s*\]", raw):
            lines.append(raw.strip())
    return lines


def _applications_status_changes(days: int = 1) -> list[str]:
    if not APPLICATIONS.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    changes = []
    for raw in APPLICATIONS.read_text().splitlines():
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if d >= cutoff and raw.strip():
            changes.append(raw.strip())
    return changes


def _scan_history_recent(hours: int = 24) -> int:
    if not SCAN_HISTORY.exists():
        return 0
    cutoff = datetime.now() - timedelta(hours=hours)
    n = 0
    for raw in SCAN_HISTORY.read_text().splitlines():
        if not raw or raw.startswith("url"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        try:
            first_seen = datetime.fromisoformat(parts[1].split("T")[0])
        except (ValueError, IndexError):
            continue
        if first_seen >= cutoff:
            n += 1
    return n


def _legacy_jobs() -> list[dict]:
    if not LEGACY_JOBS_LAST.exists():
        return []
    try:
        data = json.loads(LEGACY_JOBS_LAST.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data[:10]
    if isinstance(data, dict) and "jobs" in data:
        return data["jobs"][:10]
    return []


def _summarize(pending: list[str], status_changes: list[str], new_scans: int, legacy_jobs: list[dict]) -> str:
    client = anthropic.Anthropic()
    context = {
        "date": datetime.now().strftime("%A %B %-d, %Y"),
        "pending_count": len(pending),
        "pending_sample": pending[:8],
        "status_changes_last_24h": status_changes[:6],
        "new_scans_last_24h": new_scans,
        "legacy_jobs_first_5": legacy_jobs[:5],
    }

    msg = client.messages.create(
        model=MODEL,
        max_tokens=600,
        system=(
            "You write tight, useful daily job-search digests for Thomas to read on Telegram. "
            "No fluff, no 'great news!', no emojis except where they add scannable structure. "
            "Markdown allowed (bold, lists). Max 12 lines. End with one actionable next step."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a daily digest from this career-ops state. Highlight: count of pending jobs to review, "
                    "any status changes from the last 24h, top 3-5 jobs by interest (use legacy_jobs_first_5 if pending is empty), "
                    "and one specific next action.\n\n"
                    + json.dumps(context, indent=2, default=str)
                ),
            }
        ],
    )
    return msg.content[0].text.strip()


def main() -> int:
    _load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    pending = _pipeline_pending()
    status_changes = _applications_status_changes(days=1)
    new_scans = _scan_history_recent(hours=24)
    legacy_jobs = _legacy_jobs()

    if not (pending or status_changes or new_scans or legacy_jobs):
        print("📭 No job activity in the last 24h. Run `cd ~/career-ops && npm run scan` to discover new postings.")
        return 0

    summary = _summarize(pending, status_changes, new_scans, legacy_jobs)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
