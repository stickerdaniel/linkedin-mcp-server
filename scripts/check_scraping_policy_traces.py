#!/usr/bin/env python3
"""Compare generated semantic scraping traces with canonical fixtures."""

from __future__ import annotations

from pathlib import Path

import argparse
import asyncio
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.scraping.policy_scenarios import (  # noqa: E402
    TRACE_ROOT,
    build_policy_traces,
    canonical_json,
    policy_trace_diff,
)


def _inside_fixture_root(path: Path) -> bool:
    return path.resolve().is_relative_to(TRACE_ROOT.parent.resolve())


def _write_generated(output: Path, generated: dict[str, dict]) -> None:
    if _inside_fixture_root(output):
        raise ValueError(
            f"refusing to write generated output inside canonical fixture directory: {output}"
        )
    if output.exists():
        raise ValueError(f"refusing to overwrite generated output: {output}")
    output.mkdir(parents=True)
    for name, trace in sorted(generated.items()):
        (output / name).write_text(canonical_json(trace), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--output", type=Path)
    args = parser.parse_args()

    generated = asyncio.run(build_policy_traces())
    if args.output is not None:
        try:
            _write_generated(args.output, generated)
        except ValueError as error:
            parser.error(str(error))
        print(args.output)
        return 0

    difference = policy_trace_diff(generated)
    if difference:
        sys.stderr.write(difference)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
