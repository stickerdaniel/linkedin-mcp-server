"""The committed changelog text stays within the fragment length limit.

The PR Title gate measures a fragment only when a pull request adds it. A
later edit of that fragment, and the hand edits a version bump makes in
CHANGELOG.md (Highlights, rewording), reach the release notes unmeasured, so
this checks the repository state itself against the gate's own limit.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Callable, cast

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
_FRAGMENTS = _REPO_ROOT / "changelog.d"
_CHANGELOG = _REPO_ROOT / "docs/CHANGELOG.md"
_MARKER = "<!-- towncrier release notes start -->"

# The gate imports check_pr_title from its own directory.
_SPEC = importlib.util.spec_from_file_location(
    "check_changelog_fragment", _SCRIPTS / "check_changelog_fragment.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_SPEC)
sys.path.insert(0, str(_SCRIPTS))
try:
    _SPEC.loader.exec_module(_GATE)
finally:
    sys.path.remove(str(_SCRIPTS))

MAX_FRAGMENT_CHARS = cast(int, _GATE.MAX_FRAGMENT_CHARS)
_added_text = cast(Callable[[dict[str, Any]], str], _GATE._added_text)

_VERSION = re.compile(r"## (\S+) \(")
# The link towncrier appends, and the credit a release body may carry.
_TRAILING_LINK = re.compile(
    r" \(\[#[0-9]+\]\([^)]*\)(?: by @[A-Za-z0-9-]+(?:\[bot\])?)?\)$"
)


def _joined(text: str) -> str:
    """The text the way the gate reads an added fragment."""
    return _added_text({"patch": "".join(f"+{line}\n" for line in text.splitlines())})


def _bullets(changelog: str) -> list[tuple[str, str]]:
    """Every top-level bullet below the marker, as (version, joined text)."""
    bullets: list[tuple[str, list[str]]] = []
    version = "an unversioned section"
    in_bullet = False
    for line in changelog.split(_MARKER, 1)[1].splitlines():
        heading = _VERSION.match(line)
        if heading:
            version = heading[1]
        if line.startswith("- "):
            bullets.append((version, [line[2:]]))
            in_bullet = True
        elif in_bullet and line[:1].isspace() and line.strip():
            bullets[-1][1].append(line)
        else:
            in_bullet = False
    return [(version, _joined("\n".join(lines))) for version, lines in bullets]


def _too_long(entries: list[tuple[str, str]]) -> list[str]:
    return [
        f"{where}: {len(text)} characters: {text}"
        for where, text in entries
        if len(text) > MAX_FRAGMENT_CHARS
    ]


def test_every_fragment_is_within_the_limit() -> None:
    fragments = [
        (f"changelog.d/{path.name}", _joined(path.read_text(encoding="utf-8")))
        for path in sorted(_FRAGMENTS.iterdir())
        if path.name != "README.md"
    ]

    assert not _too_long(fragments), (
        f"Fragments longer than {MAX_FRAGMENT_CHARS} characters:\n"
        + "\n".join(_too_long(fragments))
    )


def test_every_changelog_bullet_is_within_the_limit() -> None:
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    bullets = [
        (f"CHANGELOG.md {version}", _TRAILING_LINK.sub("", text))
        for version, text in _bullets(changelog)
    ]

    # A parser that finds nothing would pass any changelog.
    assert bullets
    assert not _too_long(bullets), (
        f"CHANGELOG.md bullets longer than {MAX_FRAGMENT_CHARS} characters, "
        "without their pull request link:\n" + "\n".join(_too_long(bullets))
    )
