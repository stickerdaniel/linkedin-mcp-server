"""Require before and after screenshots when a pull request changes a README.

Runs in the README media workflow from the trusted base revision. The pull
request's body and files arrive as API data and are never checked out or
executed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from check_changelog_fragment import (
    INVALID_FILES_DATA,
    INVALID_PR_DATA,
    _InputError,
    _inventory_error,
    _is_int,
    _load_files,
    _read_json,
    _shown,
)

# A maintainer applies this label when a change to a README leaves the
# rendered page as it was, such as an edit inside an HTML comment.
EXEMPT_LABEL = "no-visual-change"

# Files that readers see as rendered Markdown. GitHub renders every README.md
# under its folder listing. PyPI shows README.md as the project description
# (`readme` in pyproject.toml). Docker Hub shows docs/docker-hub.md (the
# release workflow uploads it). Claude Desktop shows the `long_description`
# of manifest.json, so only that key counts there. The workflow's `paths`
# filter lists the same files.
_README = re.compile(r"(?:.*/)?README\.md")
_DOCKER_HUB = "docs/docker-hub.md"
_MANIFEST = "manifest.json"
_LONG_DESCRIPTION = re.compile(r'[+-]\s*"long_description"\s*:')

# Markup that GitHub does not render as an image: HTML comments, such as the
# PR template's instructions, and fenced code blocks.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?^ {0,3}\1", re.DOTALL | re.MULTILINE
)
# A Markdown image, or the <img> tag GitHub inserts for a dropped file.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*\bsrc\s*=", re.IGNORECASE)
_BEFORE = re.compile(r"\bbefore\b", re.IGNORECASE)
_AFTER = re.compile(r"\bafter\b", re.IGNORECASE)


def _load_pr(path: Path) -> tuple[str, int, bool]:
    """Return the body, the changed file count and whether the PR is exempt."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise _InputError
    body = data.get("body") or ""
    changed_files = data.get("changed_files")
    labels = data.get("labels")
    if not isinstance(body, str):
        raise _InputError
    if not _is_int(changed_files) or changed_files < 0:
        raise _InputError
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in labels
    ):
        raise _InputError
    exempt = any(label["name"] == EXEMPT_LABEL for label in labels)
    return body, changed_files, exempt


def rendered_files(files: list[dict[str, Any]]) -> list[str]:
    """The changed files whose rendered output readers see."""
    found: list[str] = []
    for entry in files:
        path = entry["filename"]
        if _README.fullmatch(path) or path == _DOCKER_HUB:
            found.append(path)
        elif path == _MANIFEST:
            # Without a patch (a diff too large for the API) the change cannot
            # be ruled out, so it counts.
            patch = entry.get("patch")
            if patch is None or any(
                _LONG_DESCRIPTION.match(line) for line in patch.splitlines()
            ):
                found.append(path)
    return found


def has_comparison(body: str) -> bool:
    """Whether the rendered body shows two images and names before and after."""
    visible = _FENCE.sub("", _COMMENT.sub("", body))
    return (
        len(_IMAGE.findall(visible)) >= 2
        and _BEFORE.search(visible) is not None
        and _AFTER.search(visible) is not None
    )


def check(body: str, files: list[dict[str, Any]], *, exempt: bool = False) -> list[str]:
    """Return one diagnostic per problem, or an empty list."""
    changed = rendered_files(files)
    if not changed or exempt or has_comparison(body):
        return []
    names = ", ".join(_shown(path) for path in changed)
    return [
        f"This pull request changes {names}, which readers see rendered. "
        "Add screenshots of every changed section to the PR description, "
        "one before and one after your change, at the same width, for example "
        "in a | Before | After | table. Take the before shot from the file on "
        "the base branch and the after shot from the file on your branch, as "
        "GitHub renders them. Drop the images into the description editor, "
        "not into a comment. A maintainer adds the "
        f"{EXEMPT_LABEL} label when the rendered page does not change."
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", type=Path, required=True)
    parser.add_argument("--files-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        body, changed_files, exempt = _load_pr(args.pr_json)
    except _InputError:
        print(f"::error::{INVALID_PR_DATA}")
        return 1
    try:
        files = _load_files(args.files_json)
    except _InputError:
        print(f"::error::{INVALID_FILES_DATA}")
        return 1
    inventory_error = _inventory_error(changed_files, files)
    if inventory_error is not None:
        print(f"::error::{inventory_error}")
        return 1

    errors = check(body, files, exempt=exempt)
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
