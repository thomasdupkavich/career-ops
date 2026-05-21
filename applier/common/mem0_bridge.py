#!/usr/bin/env python3
"""Mem0 bridge for the self-improvement loop.

Stores and queries patterns from completed applications:
  - "remote-only roles at <50-person startups → 3x response rate"
  - "Workday submits via direct URL more often than browse → 2.5x success"
  - "applications with 5+ strong-keyword matches → 5x response rate"

Mem0 is configured locally (no cloud). Storage backend: SQLite by default,
upgradeable to vector store later. API key not required for local mode.

Methods:
  remember(content, metadata) — write an observation
  recall(query, limit=5)      — search past observations

Phase 7's DSPy GEPA cron will read decisions.sqlite + recall() to refine prompts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

MEM0_DIR = Path.home() / ".hermes" / "mem0"
MEM0_DIR.mkdir(parents=True, exist_ok=True)


def _get_client():
    """Lazy import + initialize Mem0 with local config."""
    try:
        from mem0 import Memory
    except ImportError:
        return None
    config = {
        "vector_store": {
            "provider": "chroma",
            "config": {"path": str(MEM0_DIR / "chroma")},
        },
        "history_db_path": str(MEM0_DIR / "history.db"),
    }
    try:
        return Memory.from_config(config)
    except Exception as e:
        print(f"[mem0] init failed: {e}", file=sys.stderr)
        return None


def remember(content: str, metadata: Optional[dict] = None) -> bool:
    """Store one observation about the job-search loop."""
    client = _get_client()
    if not client:
        return False
    try:
        client.add(messages=[{"role": "user", "content": content}], user_id="career-ops", metadata=metadata or {})
        return True
    except Exception as e:
        print(f"[mem0] remember failed: {e}", file=sys.stderr)
        return False


def recall(query: str, limit: int = 5) -> list[dict]:
    """Search past observations for relevant patterns."""
    client = _get_client()
    if not client:
        return []
    try:
        result = client.search(query=query, user_id="career-ops", limit=limit)
        # mem0 v1 returns list of dicts with 'memory' key
        return result if isinstance(result, list) else result.get("results", [])
    except Exception as e:
        print(f"[mem0] recall failed: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m common.mem0_bridge {remember|recall} <text>")
        sys.exit(1)
    cmd = sys.argv[1]
    text = " ".join(sys.argv[2:])
    if cmd == "remember":
        print("ok" if remember(text) else "fail")
    elif cmd == "recall":
        import json
        print(json.dumps(recall(text), indent=2))
