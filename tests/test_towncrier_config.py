"""Contracts for the repository's towncrier configuration and CHANGELOG."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MARKER = "<!-- towncrier release notes start -->"


def _fixture(tmp_path: Path, fragments: dict[str, str]) -> Path:
    shutil.copy(_REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(_REPO_ROOT / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    fragments_dir = tmp_path / "changelog.d"
    fragments_dir.mkdir()
    shutil.copy(_REPO_ROOT / "changelog.d" / "README.md", fragments_dir)
    for name, text in fragments.items():
        (fragments_dir / name).write_text(text, encoding="utf-8")
    return tmp_path


_ALL_TYPES = {
    "1.fix.md": "Fixed a crash.\n",
    "2.breaking.md": "Removed a setting.\n",
    "3.feat.md": "Added a tool.\n",
}


def _build(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--version",
            "9.9.9",
            "--date",
            "2026-09-24",
            *extra,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_draft_renders_version_categories_and_links(tmp_path: Path) -> None:
    result = _build(_fixture(tmp_path, _ALL_TYPES), "--draft")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    headings = [
        lines.index("## 9.9.9 (2026-09-24)"),
        lines.index("### Breaking Changes"),
        lines.index("### Features"),
        lines.index("### Bug Fixes"),
    ]
    assert headings == sorted(headings)
    assert (
        "- Added a tool. "
        "([#3](https://github.com/stickerdaniel/linkedin-mcp-server/pull/3))"
    ) in lines


def test_unknown_fragment_type_fails_the_build(tmp_path: Path) -> None:
    root = _fixture(tmp_path, {**_ALL_TYPES, "4.feature.md": "Misfiled.\n"})

    result = _build(root, "--draft")

    assert result.returncode != 0
    assert "4.feature.md" in result.stdout + result.stderr


def test_build_writes_below_the_header_and_marker(tmp_path: Path) -> None:
    root = _fixture(tmp_path, _ALL_TYPES)

    result = _build(root, "--keep")

    assert result.returncode == 0, result.stdout + result.stderr
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith("# Changelog\n")
    assert changelog.count(_MARKER) == 1
    assert changelog.index(_MARKER) < changelog.index("\n## 9.9.9 (2026-09-24)\n")
    assert (root / "changelog.d" / "3.feat.md").exists()


def test_changelog_carries_the_configured_marker_once() -> None:
    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert config["tool"]["towncrier"]["start_string"] == f"{_MARKER}\n"
    assert changelog.count(_MARKER) == 1
    assert f"\n{_MARKER}\n" in changelog
