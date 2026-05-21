#!/usr/bin/env python3
"""Telegram bridge for the applier — both notifications and approval gates.

Uses the user's existing Telegram bot (TELEGRAM_BOT_TOKEN in ~/.hermes/.env,
chat_id 7024030224 from the Hermes config).

Two public functions:
  notify(text)  — send a message, no expectation of reply
  approval_gate(text, timeout_seconds=300) — send, wait for emoji reaction or message reply

Falls back to log-only if Telegram is unreachable, so the applier never blocks
on networking issues.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

import httpx

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
TG_OFFSET_CACHE = HERMES_HOME / "cache" / "applier-tg-offset.txt"
DEFAULT_CHAT_ID = "7024030224"  # from Hermes config — Thomas's home channel


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


def _bot_request(method: str, params: dict | None = None, timeout: float = 10.0) -> dict | None:
    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = httpx.post(url, json=params or {}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        print(f"[telegram] request failed: {e}", file=sys.stderr)
        return None


def notify(text: str, chat_id: str | None = None, parse_mode: str = "Markdown") -> bool:
    """Send a one-way notification. Returns True on success."""
    chat_id = chat_id or os.environ.get("TELEGRAM_DEFAULT_CHAT_ID") or DEFAULT_CHAT_ID
    result = _bot_request("sendMessage", {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    })
    return bool(result and result.get("ok"))


def approval_gate(
    text: str,
    timeout_seconds: int = 300,
    poll_interval: float = 3.0,
    chat_id: str | None = None,
) -> Literal["approve", "deny", "timeout", "error"]:
    """Send message asking for user input, poll updates for reaction or text reply.

    Approve = thumbs-up reaction OR text reply matching /yes|ok|go|apply|👍/i.
    Deny    = thumbs-down reaction OR text reply matching /no|skip|stop|cancel|👎/i.
    Timeout = no recognizable reply within timeout_seconds.
    """
    chat_id = chat_id or os.environ.get("TELEGRAM_DEFAULT_CHAT_ID") or DEFAULT_CHAT_ID

    # Snapshot last update_id so we only see replies AFTER this prompt
    initial = _bot_request("getUpdates", {"timeout": 0, "limit": 1, "offset": -1})
    start_offset = 0
    if initial and initial.get("result"):
        start_offset = initial["result"][-1]["update_id"] + 1

    sent = _bot_request("sendMessage", {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })
    if not sent or not sent.get("ok"):
        return "error"

    deadline = time.time() + timeout_seconds
    offset = start_offset
    approve_pat = ("yes", "y", "ok", "go", "apply", "👍", "✅")
    deny_pat = ("no", "n", "skip", "stop", "cancel", "👎", "❌")

    while time.time() < deadline:
        time.sleep(poll_interval)
        updates = _bot_request("getUpdates", {
            "offset": offset,
            "timeout": int(min(poll_interval, deadline - time.time())),
            "limit": 10,
            "allowed_updates": ["message", "message_reaction"],
        })
        if not updates or not updates.get("ok"):
            continue
        for u in updates.get("result", []):
            offset = max(offset, u["update_id"] + 1)
            # Text reply
            msg = u.get("message")
            if msg and str(msg.get("chat", {}).get("id")) == str(chat_id):
                text_in = (msg.get("text") or "").strip().lower()
                if any(text_in == p or text_in.startswith(p + " ") or p in text_in for p in approve_pat):
                    return "approve"
                if any(text_in == p or text_in.startswith(p + " ") or p in text_in for p in deny_pat):
                    return "deny"
            # Reaction event (newer telegram bots support this)
            rx = u.get("message_reaction")
            if rx and str(rx.get("chat", {}).get("id")) == str(chat_id):
                new_emojis = [r.get("emoji") for r in rx.get("new_reaction", []) if r.get("type") == "emoji"]
                if any(e in ("👍", "✅") for e in new_emojis):
                    return "approve"
                if any(e in ("👎", "❌") for e in new_emojis):
                    return "deny"
    return "timeout"


if __name__ == "__main__":
    # Quick non-destructive sanity ping
    ok = notify(
        "📡 *applier telegram bridge online* — this is a self-test from "
        "`~/career-ops/applier/common/telegram.py`. If you're seeing this, the bridge works.",
    )
    print(f"send ok: {ok}")
    sys.exit(0 if ok else 1)
