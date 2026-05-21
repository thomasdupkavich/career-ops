#!/usr/bin/env python3
"""Career-ops applier CLI.

Usage:
  python -m applier <job-url>                 — dry run (fills form, doesn't submit)
  python -m applier <job-url> --submit        — actually submit
  python -m applier <job-url> --skip-sanity   — bypass the sanity-check skill
  python -m applier --status                  — show recent application stats

Hermes calls this from Telegram /apply N commands and from cron jobs.
"""
from __future__ import annotations

import argparse
import json
import sys

from adapters import route
from fallback import browser_use_agent
from common.logger import summary_stats


def _do_apply(url: str, submit: bool, skip_sanity: bool) -> int:
    adapter = route(url)
    if adapter is None:
        print(f"No deterministic adapter matched — using browser-use LLM fallback for {url}")
        adapter = browser_use_agent

    print(f"→ adapter: {adapter.ATS_NAME}")
    result = adapter.apply(url, dry_run=not submit, skip_sanity=skip_sanity)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("submitted") or result.get("dry_run") else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="applier")
    ap.add_argument("url", nargs="?", help="Job posting URL")
    ap.add_argument("--submit", action="store_true", help="Actually submit (default: dry run)")
    ap.add_argument("--skip-sanity", action="store_true", help="Bypass sanity-check skill")
    ap.add_argument("--status", action="store_true", help="Show application stats and exit")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(summary_stats(), indent=2))
        return 0
    if not args.url:
        ap.print_help()
        return 2
    return _do_apply(args.url, submit=args.submit, skip_sanity=args.skip_sanity)


if __name__ == "__main__":
    sys.exit(main())
