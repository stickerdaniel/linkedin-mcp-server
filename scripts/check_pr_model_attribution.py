#!/usr/bin/env python3
"""Require model attribution as the final non-empty PR body line."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


_PREFIX = "Generated with "
_PLACEHOLDER_RE = re.compile(r"<[^>]*>|\[[^]]*]")
_ERROR = (
    "Model attribution is required as the final non-empty PR body line. "
    'Examples: "Generated with Claude Sonnet 4.5 for implementation in Claude Code." '
    'or "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for '
    'review in Claude Code."'
)


def _has_valid_model_jobs(attribution: str) -> bool:
    model, separator, jobs = attribution.partition(" for ")
    if not separator or not model.strip():
        return False

    while True:
        next_for = f"{jobs} ".find(" for ")
        if next_for == -1:
            return bool(jobs.strip())
        next_pair = jobs.rfind(" and ", 0, next_for)
        if next_pair == -1:
            return bool(jobs.strip())
        if not jobs[:next_pair].strip() or not jobs[next_pair + 5 : next_for].strip():
            return False
        jobs = jobs[next_for + 5 :]


def is_valid_attribution(line: str) -> bool:
    """Return whether a line follows the required attribution grammar."""
    if not line.startswith(_PREFIX) or not line.endswith("."):
        return False
    if _PLACEHOLDER_RE.search(line):
        return False

    attribution, separator, harness = line[len(_PREFIX) : -1].rpartition(" in ")
    return bool(separator and harness.strip() and _has_valid_model_jobs(attribution))


def has_model_attribution(body: str | None) -> bool:
    """Check the final non-empty line of a pull request body."""
    if not body:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return bool(lines) and is_valid_attribution(lines[-1])


def read_pr_body(event_path: Path) -> str | None:
    """Read the pull request body from a GitHub event payload."""
    event: Any = json.loads(event_path.read_text(encoding="utf-8"))
    if not isinstance(event, dict):
        raise ValueError("GitHub event must be a JSON object")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("GitHub event has no pull_request object")
    body = pull_request.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("pull_request.body must be a string or null")
    return body


def _event_path(argument: str | None) -> Path:
    value = argument or os.environ.get("GITHUB_EVENT_PATH")
    if not value:
        raise ValueError("event path is required")
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_path", nargs="?")
    args = parser.parse_args(argv)

    try:
        valid = has_model_attribution(read_pr_body(_event_path(args.event_path)))
    except (OSError, ValueError, json.JSONDecodeError):
        valid = False

    if not valid:
        print(f"::error title=PR model attribution required::{_ERROR}")
        return 1
    print("PR model attribution is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
