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
_RESERVED_MINIMAL_PARTS = (" for ", " in ", " and ", " via ")
_ERROR = (
    "Model attribution is required as the final non-empty PR body line. "
    'Model-only is the minimum: "Generated with Claude Opus 5." '
    "Detailed attribution with the job, coding-agent harness, and optional "
    'host is recommended: "Generated with Claude Opus 5 for implementation in '
    'Claude Code via T3 Code." Use commas or "/" between jobs for one model; '
    '"and" separates model/job pairs.'
)


def _has_valid_model_jobs(attribution: str) -> bool:
    for model_job in attribution.split(" and "):
        model, separator, job = model_job.partition(" for ")
        if not separator or not model.strip() or not job.strip():
            return False
    return True


def _has_valid_harness(value: str) -> bool:
    if value.startswith("via "):
        return False
    parts = value.split(" via ")
    return len(parts) <= 2 and all(part.strip() for part in parts)


def is_valid_attribution(line: str) -> bool:
    """Return whether a line follows the required attribution grammar."""
    if not line.startswith(_PREFIX) or not line.endswith("."):
        return False
    if _PLACEHOLDER_RE.search(line):
        return False

    attribution = line[len(_PREFIX) : -1]
    model_jobs, separator, harness = attribution.rpartition(" in ")
    if separator:
        return _has_valid_harness(harness) and _has_valid_model_jobs(model_jobs)
    return bool(
        attribution.strip()
        and not any(part in attribution for part in _RESERVED_MINIMAL_PARTS)
    )


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
