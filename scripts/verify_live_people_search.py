"""Verify people-search pagination against live, authenticated LinkedIn.

This is deliberately an opt-in development harness rather than a pytest test:
it needs a contributor's real LinkedIn session and must never run in CI.

Run after ``uv run -m linkedin_mcp_server --login``::

    uv run python scripts/verify_live_people_search.py "software engineer"

The JSON report contains only navigation/count metadata, never result text or
names. Production failure diagnostics may still be emitted to stderr, so review
terminal output before sharing it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from linkedin_mcp_server.live_verification import verify_live_people_search


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify live LinkedIn people-search pagination"
    )
    parser.add_argument(
        "keywords",
        nargs="?",
        default="software engineer",
        help="Broad people-search keywords (default: software engineer)",
    )
    parser.add_argument("--location", help="Optional LinkedIn location filter")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Pages to request, from 2 to 10 (default: 2)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser while verification runs",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    # The production config loader parses ``sys.argv`` lazily when browser
    # startup begins. Keep harness-only arguments out of that second parser.
    sys.argv = [sys.argv[0]]
    try:
        report = await verify_live_people_search(
            args.keywords,
            location=args.location,
            max_pages=args.max_pages,
            headless=not args.headed,
        )
    except Exception as exc:
        print(f"Live people-search verification failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
