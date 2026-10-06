"""Decide whether a pull request diff needs the optional CI matrices.

The comparison is the merge-base diff of the event's base and head SHAs, not a
two-dot diff against the base tip. A stacked pull request is based on the
branch under it, and that branch may have moved since the branch was cut.
Renames are disabled so a path that left the tree is still part of the diff.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# Anything under these trees, or one of these files, can change what the
# optional Windows and platform jobs run. Docker and build inputs are included.
_RELEVANT_PREFIXES = (
    "linkedin_mcp_server/",
    "tests/",
    "scripts/",
    "requirements/",
    ".github/workflows/",
    "build/",
)
_RELEVANT_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "Dockerfile",
        "docker-compose.yml",
        ".dockerignore",
    }
)
# Only a diff that stays inside documentation, changelog fragments and assets
# skips the optional matrices. Markdown is documentation wherever it sits,
# except inside a relevant tree, which is decided first.
_DOC_PREFIXES = ("docs/", "changelog.d/", "assets/")
_DOC_FILES = frozenset({"LICENSE", "NOTICE"})
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")

_SHA = re.compile(r"^[0-9a-f]{40}$")
MATRICES_REQUIRED = "required"
MATRICES_SKIP = "skip"


class CiChangesError(RuntimeError):
    """The diff could not be decided. Callers fail closed and write no output."""


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_relevant(path: str) -> bool:
    return path in _RELEVANT_FILES or path.startswith(_RELEVANT_PREFIXES)


def _is_doc_changelog_or_asset(path: str) -> bool:
    if path.startswith(_DOC_PREFIXES) or path in _DOC_FILES:
        return True
    if "/assets/" in f"/{path}":
        return True
    if path.endswith(".md"):
        return True
    return path.endswith(_ASSET_SUFFIXES)


def needs_optional_matrices(paths: Iterable[str]) -> bool:
    """True unless every path is documentation, a changelog fragment, or an asset.

    An empty diff needs nothing. A path this module does not recognise needs the
    matrices: skipping has to be a positive classification, not a default.
    """
    for path in paths:
        normalized = _normalize(path)
        if not normalized or _is_relevant(normalized):
            return True
        if not _is_doc_changelog_or_asset(normalized):
            return True
    return False


def _require_sha(value: str, label: str) -> str:
    if not _SHA.fullmatch(value):
        raise CiChangesError(f"{label} is not a commit sha")
    return value


def _git(repo: Path, args: list[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise CiChangesError("git failed") from exc
    if completed.returncode != 0:
        raise CiChangesError("git failed")
    return completed.stdout


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    """Paths this pull request changes, relative to the merge base of base and head."""
    base = _require_sha(base, "base")
    head = _require_sha(head, "head")
    _git(repo, ["cat-file", "-e", f"{base}^{{commit}}"])
    _git(repo, ["cat-file", "-e", f"{head}^{{commit}}"])
    merge_base = _git(repo, ["merge-base", base, head]).decode("utf-8").strip()
    merge_base = _require_sha(merge_base, "merge-base")
    # NUL-delimited and without rename detection, so a rename contributes the
    # deleted path and the added path. Line-oriented output would split a name
    # that itself contains a newline.
    payload = _git(
        repo,
        [
            "diff",
            "-z",
            "--name-only",
            "--no-renames",
            merge_base,
            head,
            "--",
        ],
    )
    return [path.decode("utf-8") for path in payload.split(b"\0") if path]


def matrices_decision(repo: Path, base: str, head: str) -> str:
    if needs_optional_matrices(changed_paths(repo, base, head)):
        return MATRICES_REQUIRED
    return MATRICES_SKIP


def _write_output(name: str, value: str) -> None:
    line = f"{name}={value}\n"
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        sys.stdout.write(line)
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(line)


def main() -> int:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    repo = Path(os.environ.get("CI_CHANGES_REPO", "."))
    try:
        paths = changed_paths(repo, base, head)
        decision = (
            MATRICES_REQUIRED if needs_optional_matrices(paths) else MATRICES_SKIP
        )
    except (CiChangesError, UnicodeError):
        print("ci changes: failed", file=sys.stderr)
        return 1
    print(f"ci changes: {decision} ({len(paths)} paths)")
    _write_output("matrices", decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
