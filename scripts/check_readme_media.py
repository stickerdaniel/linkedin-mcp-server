"""Require before and after screenshots when a pull request changes a README.

Runs in the README media workflow from the trusted base revision. The pull
request's body, its rendering and its files arrive as API data and are never
checked out or executed.
"""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
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

INVALID_BODY_HTML = "Unable to read the rendered pull request description."

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

_BEFORE = re.compile(r"\bbefore\b", re.IGNORECASE)
_AFTER = re.compile(r"\bafter\b", re.IGNORECASE)


class _Rendered(HTMLParser):
    """Collect the images and the text of GitHub's rendered HTML.

    GitHub's renderer has already dropped HTML comments, escaped code and
    resolved reference images, so every ``<img>`` left is one a reader sees.
    Alt text counts as text: a dropped file named before.png labels itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self.images = 0
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            self.images += 1
            self.text.extend(value or "" for name, value in attrs if name == "alt")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _load_pr(path: Path) -> tuple[int, bool]:
    """Return the changed file count and whether the PR is exempt."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise _InputError
    changed_files = data.get("changed_files")
    labels = data.get("labels")
    if not _is_int(changed_files) or changed_files < 0:
        raise _InputError
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in labels
    ):
        raise _InputError
    exempt = any(label["name"] == EXEMPT_LABEL for label in labels)
    return changed_files, exempt


def _is_rendered_page(path: Any) -> bool:
    return isinstance(path, str) and (
        _README.fullmatch(path) is not None or path == _DOCKER_HUB
    )


def rendered_files(files: list[dict[str, Any]]) -> list[str]:
    """The changed files whose rendered output readers see.

    A rename counts by either name, so moving a README away still shows.
    """
    found: list[str] = []
    for entry in files:
        path = entry["filename"]
        if _is_rendered_page(path) or _is_rendered_page(entry.get("previous_filename")):
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


def has_comparison(body_html: str) -> bool:
    """Whether the rendered description shows two images and names before and after."""
    rendered = _Rendered()
    rendered.feed(body_html)
    rendered.close()
    text = " ".join(rendered.text)
    return (
        rendered.images >= 2
        and _BEFORE.search(text) is not None
        and _AFTER.search(text) is not None
    )


def check(
    body_html: str, files: list[dict[str, Any]], *, exempt: bool = False
) -> list[str]:
    """Return one diagnostic per problem, or an empty list."""
    changed = rendered_files(files)
    if not changed or exempt or has_comparison(body_html):
        return []
    names = ", ".join(_shown(path) for path in changed)
    message = (
        f"This pull request changes {names}, which readers see rendered. "
        "Add screenshots of every changed section to the PR description, "
        "one before and one after your change, at the same width, for example "
        "in a | Before | After | table. For a Markdown file, take the before "
        "shot from its GitHub file page on the base branch and the after shot "
        "from the same page on your branch."
    )
    if _MANIFEST in changed:
        message += (
            " For manifest.json, show the long_description rendered as "
            "Markdown, not the JSON source."
        )
    message += (
        " Drop the images into the description editor, not into a comment. "
        f"A maintainer adds the {EXEMPT_LABEL} label when the rendered page "
        "does not change."
    )
    return [message]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", type=Path, required=True)
    parser.add_argument("--files-json", type=Path, required=True)
    parser.add_argument(
        "--body-html",
        type=Path,
        required=True,
        help="The description as GitHub's Markdown API renders it.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        changed_files, exempt = _load_pr(args.pr_json)
    except _InputError:
        print(f"::error::{INVALID_PR_DATA}")
        return 1
    try:
        files = _load_files(args.files_json)
        if any(
            not isinstance(entry.get("previous_filename", ""), str) for entry in files
        ):
            raise _InputError
    except _InputError:
        print(f"::error::{INVALID_FILES_DATA}")
        return 1
    inventory_error = _inventory_error(changed_files, files)
    if inventory_error is not None:
        print(f"::error::{inventory_error}")
        return 1
    try:
        body_html = args.body_html.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print(f"::error::{INVALID_BODY_HTML}")
        return 1

    errors = check(body_html, files, exempt=exempt)
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
