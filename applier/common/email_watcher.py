#!/usr/bin/env python3
"""Gmail watcher for job-related emails.

Runs every 10 min via Hermes cron. No LLM dependency — pure IMAP + regex.

Outputs:
  ~/.hermes/cache/2fa-recent.json   — recent 2FA codes the applier can read
  ~/.hermes/cache/job-emails.jsonl  — append log of classified job-related emails

Classification (regex-based, deterministic):
  - 2fa            — subject/body contains "code", "verify", "verification", 4-8 digit code
  - application    — "thank you for applying", "we received your application", "submitted"
  - interview      — "interview", "schedule", "calendly", "phone screen"
  - rejection      — "we have decided", "unfortunately", "moving forward with other"
  - recruiter      — sender domain matches known ATS or recruiter pattern
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone, timedelta
from email.header import decode_header, make_header
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
CACHE_DIR = HERMES_HOME / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TWOFA_CACHE = CACHE_DIR / "2fa-recent.json"
JOB_EMAIL_LOG = CACHE_DIR / "job-emails.jsonl"

ATS_SENDERS = re.compile(
    r"(greenhouse|lever\.co|ashbyhq|workday|smartrecruiters|icims|taleo|"
    r"jobvite|bamboohr|recruitee|breezy|linkedin|indeed|ziprecruiter|"
    r"wellfound|internshala|hire\.com|myworkday|@careers\.|noreply.*hr|"
    r"recruiting|talent|jobs@)",
    re.IGNORECASE,
)

TWOFA_PATTERNS = [
    re.compile(r"\bcode\b.*?\b(\d{4,8})\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(\d{6})\b.*?\b(verification|verify|sign[ -]?in|login|code)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(verification|verify|sign[ -]?in|login)\s+code[^\d]*(\d{4,8})", re.IGNORECASE),
    re.compile(r"one[- ]time.*?\b(\d{4,8})\b", re.IGNORECASE | re.DOTALL),
]

INTERVIEW_KW = re.compile(
    r"\b(interview|phone screen|tech screen|technical screen|recruiter call|"
    r"would you be available|schedule.{0,30}call|calendly|chronograph|cronofy)\b",
    re.IGNORECASE,
)

APPLICATION_KW = re.compile(
    r"\b(thank you for applying|we received your application|application received|"
    r"successfully applied|application has been submitted)\b",
    re.IGNORECASE,
)

REJECTION_KW = re.compile(
    r"\b(we have decided|unfortunately|moving forward with other|not moving forward|"
    r"decided not to move forward|other candidates|wish you the best|"
    r"will not be progressing)\b",
    re.IGNORECASE,
)


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


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _body_text(msg: email.message.Message) -> str:
    """Extract plaintext body, falling back to stripping HTML."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
            elif ctype == "text/html" and not parts:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    parts.append(re.sub(r"<[^>]+>", " ", html))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            ctype = msg.get_content_type()
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
            if ctype == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            parts.append(text)
    return "\n".join(parts)[:8000]


def _classify(subject: str, sender: str, body: str) -> dict:
    """Return classification dict with keys: kind, twofa_code (optional)."""
    blob = f"{subject}\n{body}"

    # 2FA first — highest priority
    for pat in TWOFA_PATTERNS:
        m = pat.search(blob)
        if m:
            # Pull the digit group
            for g in m.groups():
                if g and g.isdigit() and 4 <= len(g) <= 8:
                    return {"kind": "2fa", "twofa_code": g}

    if INTERVIEW_KW.search(blob):
        return {"kind": "interview"}
    if APPLICATION_KW.search(blob):
        return {"kind": "application"}
    if REJECTION_KW.search(blob):
        return {"kind": "rejection"}
    if ATS_SENDERS.search(sender) or ATS_SENDERS.search(subject):
        return {"kind": "recruiter"}
    return {"kind": "other"}


def _imap_connect():
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    socket.setdefaulttimeout(15)
    conn = imaplib.IMAP4_SSL(host, 993)
    conn.login(user, pw)
    return conn


def main(lookback_hours: int = 1) -> int:
    _load_env()

    try:
        conn = _imap_connect()
    except Exception as e:
        print(f"IMAP connection failed: {e}", file=sys.stderr)
        return 1

    try:
        conn.select("INBOX", readonly=True)
        # IMAP SEARCH for recent
        since = (datetime.utcnow() - timedelta(hours=lookback_hours)).strftime("%d-%b-%Y")
        typ, data = conn.search(None, f'(SINCE {since})')
        if typ != "OK":
            print("IMAP search failed", file=sys.stderr)
            return 1
        ids = data[0].split()
        # Process newest first
        ids = list(reversed(ids))[:120]

        twofa_recent = []
        classified = []
        for mid in ids:
            typ, msg_data = conn.fetch(mid, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject", ""))
            sender = _decode(msg.get("From", ""))
            date_hdr = _decode(msg.get("Date", ""))
            body = _body_text(msg)
            result = _classify(subject, sender, body)
            if result["kind"] == "other":
                continue
            entry = {
                "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                "ts": datetime.utcnow().isoformat() + "Z",
                "subject": subject[:200],
                "from": sender[:200],
                "date": date_hdr,
                **result,
            }
            classified.append(entry)
            if result["kind"] == "2fa":
                twofa_recent.append(entry)

        # Write 2FA cache (always overwrite — only keep recent codes)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        # 2FA codes older than 15 min are useless
        TWOFA_CACHE.write_text(json.dumps({
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "codes": twofa_recent,
        }, indent=2))

        # Append to job email log
        with JOB_EMAIL_LOG.open("a") as f:
            for c in classified:
                if c["kind"] != "2fa":  # don't log 2FA codes to disk persistently
                    f.write(json.dumps(c) + "\n")

        # Stdout summary for cron capture
        kinds = {}
        for c in classified:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        if kinds:
            parts = [f"{n} {k}" for k, n in sorted(kinds.items())]
            print(f"📧 Job-related: {', '.join(parts)} in last {lookback_hours}h")
        return 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main(lookback_hours=1))
