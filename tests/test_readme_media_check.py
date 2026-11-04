"""Contracts for the README media gate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_readme_media.py"

_COMPARISON = """## Summary

| Width | Before | After |
| --- | --- | --- |
| 1440 px | ![Before](https://github.com/user-attachments/assets/a) | ![After](https://github.com/user-attachments/assets/b) |
"""
_TEMPLATE = (
    Path(__file__).resolve().parent.parent / ".github" / "pull_request_template.md"
).read_text(encoding="utf-8")


def _file(name: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b") -> dict[str, Any]:
    entry: dict[str, Any] = {"filename": name, "status": "modified"}
    if patch is not None:
        entry["patch"] = patch
    return entry


def _run(
    tmp_path: Path,
    body: str | None,
    files: list[dict[str, Any]],
    labels: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    pr_json = tmp_path / "pull-request.json"
    files_json = tmp_path / "pull-request-files.json"
    pr = {
        "body": body,
        "changed_files": len(files),
        "labels": [{"name": name} for name in labels],
    }
    pr_json.write_text(json.dumps(pr), encoding="utf-8")
    files_json.write_text(json.dumps([files]), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--pr-json",
            str(pr_json),
            "--files-json",
            str(files_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "name", ["README.md", "docs/docker-hub.md", "changelog.d/README.md"]
)
def test_rendered_change_without_images_fails(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, _TEMPLATE, [_file(name)])

    assert result.returncode == 1
    assert f"This pull request changes {name}," in result.stdout


@pytest.mark.parametrize(
    "name", ["README.md", "docs/docker-hub.md", "changelog.d/README.md"]
)
def test_rendered_change_with_comparison_passes(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, _COMPARISON, [_file(name)])

    assert result.returncode == 0, result.stdout


def test_dropped_img_tags_count(tmp_path: Path) -> None:
    body = (
        'Before: <img width="800" alt="x" src="https://github.com/user-attachments/assets/a" />\n'
        'After: <img width="800" alt="y" src="https://github.com/user-attachments/assets/b" />\n'
    )

    assert _run(tmp_path, body, [_file("README.md")]).returncode == 0


@pytest.mark.parametrize(
    "body",
    [
        None,
        "Before and after: ![Screenshot](https://example.com/a.png)",
        "![One](https://example.com/a.png) ![Two](https://example.com/b.png)",
        "After: ![One](https://example.com/a.png) ![Two](https://example.com/b.png)",
        "Before: ![One](https://example.com/a.png) ![Two](https://example.com/b.png)",
        "<!-- Before ![a](a.png) after ![b](b.png) -->",
        "```markdown\nBefore ![a](a.png)\nAfter ![b](b.png)\n```",
    ],
    ids=[
        "empty",
        "one-image",
        "unlabelled",
        "after-only",
        "before-only",
        "in-comment",
        "in-code-block",
    ],
)
def test_body_without_a_visible_comparison_fails(
    tmp_path: Path, body: str | None
) -> None:
    assert _run(tmp_path, body, [_file("README.md")]).returncode == 1


def test_other_files_need_no_images(tmp_path: Path) -> None:
    files = [
        _file("docs/browser-fingerprint.md"),
        _file("linkedin_mcp_server/server.py"),
    ]

    assert _run(tmp_path, "", files).returncode == 0


def test_manifest_version_bump_needs_no_images(tmp_path: Path) -> None:
    patch = '@@ -2 +2 @@\n-  "version": "4.1.0",\n+  "version": "4.2.0",'

    assert _run(tmp_path, "", [_file("manifest.json", patch)]).returncode == 0


@pytest.mark.parametrize(
    "patch",
    [
        '@@ -7 +7 @@\n-  "long_description": "# Old",\n+  "long_description": "# New",',
        None,
    ],
    ids=["long-description", "patch-too-large"],
)
def test_manifest_description_change_fails(tmp_path: Path, patch: str | None) -> None:
    result = _run(tmp_path, "", [_file("manifest.json", patch)])

    assert result.returncode == 1
    assert "This pull request changes manifest.json," in result.stdout


def test_exempt_label_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, "", [_file("README.md")], labels=("no-visual-change",))

    assert result.returncode == 0


def test_incomplete_file_list_fails(tmp_path: Path) -> None:
    pr_json = tmp_path / "pull-request.json"
    files_json = tmp_path / "pull-request-files.json"
    pr_json.write_text(json.dumps({"body": "", "changed_files": 2, "labels": []}))
    files_json.write_text(json.dumps([[_file("linkedin_mcp_server/server.py")]]))
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--pr-json",
            str(pr_json),
            "--files-json",
            str(files_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "do not match its changed_files count" in result.stdout
