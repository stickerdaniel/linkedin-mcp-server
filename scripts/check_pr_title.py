"""Validate pull request titles for the required conventional-commit shape."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from pathlib import Path

ALLOWED_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "test",
    "chore",
    "perf",
    "ci",
)

MISSING_TITLE = "PR title is missing."
INVALID_PR_DATA = "Unable to read current pull request data."
UNSAFE_CHARACTER = "PR title contains an unsafe character."
INVALID_SHAPE = "PR title must use an accepted conventional-commit shape."
UNSUPPORTED_TYPE = "PR title uses an unsupported type."
INVALID_SCOPE = "PR title has an invalid scope."
INVALID_SUBJECT = "PR title has an invalid subject."
FINAL_PERIOD = "PR title subject must not end with an ASCII period."
SUBJECT_LIMIT = 50
LOWERCASE_SUBJECT = "PR title subject must start with a capital letter."
LONG_SUBJECT = f"PR title subject must be under {SUBJECT_LIMIT} characters."

# Renovate's account. A `[bot]` login is reserved for GitHub Apps, so no user
# account can claim it. Renovate lowercases the first letter of every subject
# after a commit prefix, with no setting to change that, and names packages at
# full length, so its titles are held to the shape alone.
RENOVATE = {"login": "renovate[bot]", "type": "Bot"}

HINT = (
    "Expected: type: subject, type(scope): subject, type!: subject, or "
    "type(scope)!: subject. Allowed types: " + ", ".join(ALLOWED_TYPES) + ". "
    f"The subject starts with a capital letter and stays under {SUBJECT_LIMIT} "
    "characters."
)

_TITLE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: "
    r"(?P<subject>.*)$"
)
_BIDI_FORMATTING = {
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


def _is_unsafe(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint <= 0x001F
        or 0x007F <= codepoint <= 0x009F
        or codepoint in {0x200B, 0x2028, 0x2029, 0x2060, 0xFEFF, 0xFFFD}
        or codepoint in _BIDI_FORMATTING
    )


def _is_substantive(character: str) -> bool:
    return unicodedata.category(character)[0] not in {"C", "M", "Z"}


def _last_substantive(value: str) -> str | None:
    return next(
        (character for character in reversed(value) if _is_substantive(character)),
        None,
    )


def _has_invalid_boundary(value: str) -> bool:
    return bool(value) and any(
        unicodedata.category(character) == "Cf"
        or unicodedata.category(character).startswith("Z")
        for character in (value[0], value[-1])
    )


def validate_title(title: str) -> str | None:
    """Return a fixed diagnostic for an invalid title, or ``None``."""
    if any(_is_unsafe(character) for character in title):
        return UNSAFE_CHARACTER

    match = _TITLE.fullmatch(title)
    if match is None:
        return INVALID_SHAPE

    if match["type"] not in ALLOWED_TYPES:
        return UNSUPPORTED_TYPE

    scope = match["scope"]
    if scope is not None and (
        _last_substantive(scope) is None or _has_invalid_boundary(scope)
    ):
        return INVALID_SCOPE

    subject = match["subject"]
    last_substantive = _last_substantive(subject)
    if last_substantive is None:
        return INVALID_SUBJECT
    if last_substantive == ".":
        return FINAL_PERIOD
    if _has_invalid_boundary(subject):
        return INVALID_SUBJECT

    return None


def validate_style(title: str) -> str | None:
    """Return a diagnostic for a valid title that breaks the house style, or ``None``.

    Run after ``validate_title``; Renovate's titles skip it, see ``RENOVATE``.
    """
    match = _TITLE.fullmatch(title)
    if match is None:
        return INVALID_SHAPE
    subject = match["subject"]
    if subject[0].islower():
        return LOWERCASE_SUBJECT
    if len(subject) >= SUBJECT_LIMIT:
        return LONG_SUBJECT
    return None


def is_renovate(user: object) -> bool:
    """Whether a pull request's ``user`` object is Renovate's app account."""
    return isinstance(user, dict) and all(
        user.get(key) == value for key, value in RENOVATE.items()
    )


def _load_pr(path: Path) -> tuple[str, bool] | None:
    """Read the title and whether Renovate wrote it, or ``None`` if unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if not isinstance(title, str) or not title:
        return None
    return title, is_renovate(data.get("user"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-json", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.pr_json is not None:
        loaded = _load_pr(args.pr_json)
        if loaded is None:
            diagnostic = INVALID_PR_DATA
        else:
            title, by_renovate = loaded
            diagnostic = validate_title(title) or (
                None if by_renovate else validate_style(title)
            )
    else:
        title = os.environ.get("PR_TITLE")
        diagnostic = (
            MISSING_TITLE
            if not title
            else validate_title(title) or validate_style(title)
        )

    if diagnostic is None:
        return 0

    print(f"::error::{diagnostic}")
    print(HINT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
