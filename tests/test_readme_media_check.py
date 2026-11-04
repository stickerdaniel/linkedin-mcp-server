"""Contracts for the README media gate.

Each fixture pairs a PR description (``.md``) with the HTML GitHub's Markdown
API rendered from it (``.html``), recorded from ``POST /markdown`` in gfm mode.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "check_readme_media.py"
_FIXTURES = _ROOT / "tests" / "fixtures" / "readme_media"


def _file(
    name: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b", **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {"filename": name, "status": "modified", **extra}
    if patch is not None:
        entry["patch"] = patch
    return entry


def _run(
    tmp_path: Path,
    body: str,
    files: list[dict[str, Any]],
    labels: tuple[str, ...] = (),
    changed_files: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the checker on the rendered fixture ``body``."""
    pr_json = tmp_path / "pull-request.json"
    files_json = tmp_path / "pull-request-files.json"
    pr = {
        "changed_files": len(files) if changed_files is None else changed_files,
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
            "--body-html",
            str(_FIXTURES / f"{body}.html"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


_RENDERED = ["README.md", "docs/docker-hub.md", "changelog.d/README.md"]


@pytest.mark.parametrize("name", _RENDERED)
def test_rendered_change_without_images_fails(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, "template", [_file(name)])

    assert result.returncode == 1
    assert f"This pull request changes {name}," in result.stdout


@pytest.mark.parametrize("name", _RENDERED)
def test_rendered_change_with_comparison_passes(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, "table", [_file(name)])

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("body", ["img-tags", "reference-images", "alt-only"])
def test_other_image_forms_count(tmp_path: Path, body: str) -> None:
    assert _run(tmp_path, body, [_file("README.md")]).returncode == 0


@pytest.mark.parametrize(
    "body",
    [
        "one-image",
        "unlabelled",
        "after-only",
        "before-only",
        "in-comment",
        "inline-code",
        "unclosed-fence",
    ],
)
def test_body_without_a_visible_comparison_fails(tmp_path: Path, body: str) -> None:
    assert _run(tmp_path, body, [_file("README.md")]).returncode == 1


def test_other_files_need_no_images(tmp_path: Path) -> None:
    files = [
        _file("docs/browser-fingerprint.md"),
        _file("linkedin_mcp_server/server.py"),
    ]

    assert _run(tmp_path, "template", files).returncode == 0


def test_renaming_a_readme_away_fails(tmp_path: Path) -> None:
    renamed = _file(
        "changelog.d/FORMAT.md",
        status="renamed",
        previous_filename="changelog.d/README.md",
    )

    result = _run(tmp_path, "template", [renamed])

    assert result.returncode == 1
    assert "This pull request changes changelog.d/FORMAT.md," in result.stdout


def test_manifest_version_bump_needs_no_images(tmp_path: Path) -> None:
    patch = '@@ -2 +2 @@\n-  "version": "4.1.0",\n+  "version": "4.2.0",'

    assert _run(tmp_path, "template", [_file("manifest.json", patch)]).returncode == 0


@pytest.mark.parametrize(
    "patch",
    [
        '@@ -7 +7 @@\n-  "long_description": "# Old",\n+  "long_description": "# New",',
        None,
    ],
    ids=["long-description", "patch-too-large"],
)
def test_manifest_description_change_fails(tmp_path: Path, patch: str | None) -> None:
    result = _run(tmp_path, "template", [_file("manifest.json", patch)])

    assert result.returncode == 1
    assert "This pull request changes manifest.json," in result.stdout
    assert "long_description rendered as Markdown" in result.stdout


def test_exempt_label_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "template", [_file("README.md")], labels=("no-visual-change",)
    )

    assert result.returncode == 0


def test_incomplete_file_list_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "table", [_file("linkedin_mcp_server/server.py")], changed_files=2
    )

    assert result.returncode == 1
    assert "do not match its changed_files count" in result.stdout
