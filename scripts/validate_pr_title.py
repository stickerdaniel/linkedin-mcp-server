#!/usr/bin/env python3
"""Validate a GitHub pull request title as a Conventional Commit subject.

PRs in this repository are squash-merged, so the title becomes the permanent
commit subject on ``main``. Types match ``AGENTS.md`` / ``CLAUDE.md``, plus
``build`` so Renovate and the label workflow stay aligned.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Keep in sync with label-pr.yml's case list and AGENTS.md commit types.
CONVENTIONAL_TYPES = frozenset(
    {
        "feat",
        "fix",
        "docs",
        "style",
        "refactor",
        "test",
        "chore",
        "perf",
        "ci",
        "build",
    }
)

_TITLE_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
    r"(?P<breaking>!)?"
    r": "
    r"(?P<subject>.+)$"
)

_EXAMPLE = "feat(server): Add optional minimum tool interval"


def validate_pr_title(title: str) -> str | None:
    """Return ``None`` when *title* is valid, else a precise correction message."""
    cleaned = title.strip()
    if not cleaned:
        return (
            f"PR title is empty. Use a Conventional Commit subject, e.g. {_EXAMPLE!r}."
        )
    if "\n" in cleaned or "\r" in cleaned:
        return (
            "PR title must be a single line. "
            f"Use a Conventional Commit subject, e.g. {_EXAMPLE!r}."
        )

    match = _TITLE_RE.match(cleaned)
    if match is None:
        bare = re.fullmatch(
            r"(?P<type>[a-z]+)"
            r"(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
            r"(?P<breaking>!)?"
            r":?",
            cleaned,
        )
        if bare is not None and bare.group("type") in CONVENTIONAL_TYPES:
            return (
                "PR title subject is empty after the type. "
                f"Add an imperative summary, e.g. {_EXAMPLE!r}."
            )
        if ": " not in cleaned:
            return (
                "PR title is missing the ': ' separator after the type. "
                f"Expected type(optional-scope): subject, e.g. {_EXAMPLE!r}."
            )
        type_part = cleaned.split(": ", 1)[0]
        if "(" in type_part and not re.search(r"\([a-z0-9][a-z0-9._/-]*\)", type_part):
            return (
                "PR title has an invalid scope. Use lowercase letters, digits, "
                f". _ / - inside parentheses, e.g. {_EXAMPLE!r}."
            )
        return (
            "PR title is not a Conventional Commit subject. "
            f"Expected type(optional-scope): subject, e.g. {_EXAMPLE!r}."
        )
    commit_type = match.group("type")
    subject = match.group("subject")
    if commit_type not in CONVENTIONAL_TYPES:
        allowed = ", ".join(sorted(CONVENTIONAL_TYPES))
        return (
            f"Unknown type {commit_type!r}. Allowed types: {allowed}. "
            f"Example: {_EXAMPLE!r}."
        )
    if not subject.strip():
        return (
            "PR title subject is empty after ': '. "
            f"Add an imperative summary, e.g. {_EXAMPLE!r}."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a pull request title as a Conventional Commit subject."
    )
    parser.add_argument(
        "title",
        nargs="?",
        default=None,
        help="Title to validate (default: $PR_TITLE)",
    )
    args = parser.parse_args(argv)
    title = args.title if args.title is not None else os.environ.get("PR_TITLE", "")
    error = validate_pr_title(title)
    if error is None:
        print(f"PR title ok: {title.strip()}")
        return 0
    print(f"::error::{error}")
    print(error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
