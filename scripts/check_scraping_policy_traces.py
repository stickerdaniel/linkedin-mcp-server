#!/usr/bin/env python3
"""Compare generated semantic scraping traces with canonical fixtures."""

from __future__ import annotations

from difflib import unified_diff
from pathlib import Path

import asyncio
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.scraping.policy_scenarios import (  # noqa: E402
    TRACE_ROOT,
    build_policy_traces,
    canonical_json,
)


def main() -> int:
    if sys.argv[1:] not in ([], ["--check"]):
        print("usage: check_scraping_policy_traces.py [--check]", file=sys.stderr)
        return 2

    generated = asyncio.run(build_policy_traces())
    expected_names = set(generated)
    actual_names = {path.name for path in TRACE_ROOT.glob("*.json")}
    failed = False

    for missing in sorted(expected_names - actual_names):
        print(f"missing canonical trace: {TRACE_ROOT / missing}", file=sys.stderr)
        failed = True
    for extra in sorted(actual_names - expected_names):
        print(f"unexpected canonical trace: {TRACE_ROOT / extra}", file=sys.stderr)
        failed = True

    for name in sorted(expected_names & actual_names):
        path = TRACE_ROOT / name
        expected = path.read_text(encoding="utf-8")
        actual = canonical_json(generated[name])
        if expected == actual:
            continue
        failed = True
        sys.stderr.writelines(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"generated/{name}",
            )
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
