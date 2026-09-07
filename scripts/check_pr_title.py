"""Validate pull request titles for the required conventional-commit shape."""

from __future__ import annotations

import os
import re

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
UNSAFE_CHARACTER = "PR title contains an unsafe character."
INVALID_SHAPE = "PR title must use an accepted conventional-commit shape."
UNSUPPORTED_TYPE = "PR title uses an unsupported type."
INVALID_SCOPE = "PR title has an invalid scope."
INVALID_SUBJECT = "PR title has an invalid subject."
FINAL_PERIOD = "PR title subject must not end with an ASCII period."

HINT = (
    "Expected: type: subject, type(scope): subject, type!: subject, or "
    "type(scope)!: subject. Allowed types: " + ", ".join(ALLOWED_TYPES) + "."
)

_TITLE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: "
    r"(?P<subject>.*)$"
)
_BOUNDARY_WHITESPACE = {
    0x0020,
    0x00A0,
    0x1680,
    *range(0x2000, 0x200B),
    0x202F,
    0x205F,
    0x3000,
}
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
        or codepoint in {0x2028, 0x2029, 0xFEFF, 0xFFFD}
        or codepoint in _BIDI_FORMATTING
    )


def _has_boundary_whitespace(value: str) -> bool:
    return bool(value) and (
        ord(value[0]) in _BOUNDARY_WHITESPACE
        or ord(value[-1]) in _BOUNDARY_WHITESPACE
        or _is_unsafe(value[0])
        or _is_unsafe(value[-1])
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
    if scope is not None and (not scope or _has_boundary_whitespace(scope)):
        return INVALID_SCOPE

    subject = match["subject"]
    if not subject or _has_boundary_whitespace(subject):
        return INVALID_SUBJECT
    if subject.endswith("."):
        return FINAL_PERIOD

    return None


def main() -> int:
    title = os.environ.get("PR_TITLE")
    diagnostic = MISSING_TITLE if not title else validate_title(title)
    if diagnostic is None:
        return 0

    print(f"::error::{diagnostic}")
    print(HINT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
