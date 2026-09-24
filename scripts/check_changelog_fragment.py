"""Require a changelog fragment for user-facing pull requests.

Runs in the PR Title workflow from the trusted base revision. The pull
request's files arrive as API data and are never checked out or executed.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from check_pr_title import _TITLE, validate_title

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The fragment directory's own documentation, which towncrier also skips.
_README = "README.md"

INVALID_PR_DATA = "Unable to read current pull request data."
INVALID_FILES_DATA = "Unable to read the pull request's changed files."
INVALID_CONFIG = "Unable to read [tool.towncrier] from pyproject.toml."

# Echo a path only when it cannot carry a newline, a workflow command or a
# bidirectional control character; the name comes from the pull request.
_SHOWABLE = re.compile(r"[A-Za-z0-9._/+-]{1,200}")


class _InputError(Exception):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _InputError from None


def _load_pr(path: Path) -> tuple[int, str]:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise _InputError
    number = data.get("number")
    title = data.get("title")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise _InputError
    if not isinstance(title, str) or not title:
        raise _InputError
    return number, title


def _load_files(path: Path) -> list[dict[str, Any]]:
    """Flatten ``gh api --paginate --slurp`` output: a list of pages."""
    data = _read_json(path)
    if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
        raise _InputError
    files = [entry for page in data for entry in page]
    for entry in files:
        if not isinstance(entry, dict):
            raise _InputError
        if not isinstance(entry.get("filename"), str):
            raise _InputError
        if not isinstance(entry.get("status"), str):
            raise _InputError
        patch = entry.get("patch")
        if patch is not None and not isinstance(patch, str):
            raise _InputError
    return files


def _load_config(path: Path) -> tuple[str, tuple[str, ...]]:
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))["tool"]["towncrier"]
        directory = config["directory"]
        types = tuple(entry["directory"] for entry in config["type"])
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        raise _InputError from None
    if not isinstance(directory, str) or not directory:
        raise _InputError
    if not types or not all(isinstance(name, str) and name for name in types):
        raise _InputError
    return directory.rstrip("/"), types


def _has_added_text(entry: dict[str, Any]) -> bool:
    patch = entry.get("patch") or ""
    return any(line.startswith("+") and line[1:].strip() for line in patch.splitlines())


def _shown(path: str) -> str:
    return path if _SHOWABLE.fullmatch(path) else "a file with an unsafe name"


def _required_type(title: str) -> str | None:
    if validate_title(title) is not None:
        # The title step before this one has already failed the job.
        return None
    match = _TITLE.fullmatch(title)
    if match is None:
        return None
    if match["breaking"]:
        return "breaking"
    if match["type"] in {"feat", "fix"}:
        return match["type"]
    return None


def check(
    number: int,
    title: str,
    files: list[dict[str, Any]],
    directory: str,
    types: tuple[str, ...],
) -> list[str]:
    """Return one diagnostic per problem, or an empty list."""
    errors: list[str] = []
    prefix = f"{directory}/"
    name_pattern = re.compile(
        r"[0-9]+\.(?:" + "|".join(re.escape(name) for name in types) + r")\.md"
    )

    # Every surviving fragment is checked, whatever the title says, so a
    # docs PR cannot slip a malformed file past the release build. Removals
    # stay legal: the bump PR folds and deletes every fragment.
    for entry in files:
        path = entry["filename"]
        if not path.startswith(prefix) or entry["status"] == "removed":
            continue
        name = path.removeprefix(prefix)
        if name == _README:
            continue
        if not name_pattern.fullmatch(name):
            errors.append(
                f"{_shown(path)} is not a fragment name. Use "
                f"{prefix}<PR number>.<type>.md with a type of "
                f"{', '.join(types)}."
            )
        elif entry["status"] == "added" and not _has_added_text(entry):
            errors.append(f"{_shown(path)} is empty. Write one user-facing sentence.")

    required = _required_type(title)
    if required is None:
        return errors

    # The files API compares against the base, so this PR's own fragment is
    # always "added", even after a rename from one type to another. An empty
    # one was already reported by the loop above.
    expected = f"{prefix}{number}.{required}.md"
    if not any(
        entry["filename"] == expected and entry["status"] == "added" for entry in files
    ):
        errors.append(
            f"This pull request needs {expected} with one user-facing sentence. "
            "If it already has a fragment of another type, rename that file and "
            "edit it. Do not run `towncrier create` again: a second run writes a "
            f"numbered copy such as {prefix}{number}.{required}.1.md, which this "
            "check rejects."
        )
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-json", type=Path, required=True)
    parser.add_argument("--files-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        number, title = _load_pr(args.pr_json)
    except _InputError:
        print(f"::error::{INVALID_PR_DATA}")
        return 1
    try:
        files = _load_files(args.files_json)
    except _InputError:
        print(f"::error::{INVALID_FILES_DATA}")
        return 1
    try:
        directory, types = _load_config(_PYPROJECT)
    except _InputError:
        print(f"::error::{INVALID_CONFIG}")
        return 1

    errors = check(number, title, files, directory, types)
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
