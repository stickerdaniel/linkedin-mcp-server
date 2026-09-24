"""Contracts for composing the GitHub release body from CHANGELOG.md."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "compose_release_notes.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_TEMPLATE = _REPO_ROOT / "RELEASE_NOTES_TEMPLATE.md"
_REPOSITORY = "stickerdaniel/linkedin-mcp-server"
_SHOULD_RELEASE = "steps.check.outputs.should-release == 'true'"

_CHANGELOG = """\
# Changelog

Entries start with the release that adopted towncrier.

<!-- towncrier release notes start -->

## 4.27.0 (2026-10-01)

### Features

- Newer feature. ([#3](https://example.test/pull/3))


## 4.26.0 (2026-09-24)

### Breaking Changes

- Removed a setting. ([#2](https://example.test/pull/2))

### Bug Fixes

- Fixed a crash. ([#1](https://example.test/pull/1))


## 4.25.1 (2026-09-01)

No significant changes.
"""

_INSTALL = "## Install or update\n\nGet v${VERSION} from ${VERSION}.\n"


def _compose(
    tmp_path: Path,
    version: str,
    *,
    changelog: str = _CHANGELOG,
    template: str = _INSTALL,
    fragments: tuple[str, ...] = ("README.md",),
    previous: str = "4.25.0",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "TEMPLATE.md").write_text(template, encoding="utf-8")
    fragments_dir = tmp_path / "changelog.d"
    fragments_dir.mkdir(exist_ok=True)
    for name in fragments:
        (fragments_dir / name).write_text("text\n", encoding="utf-8")
    output = tmp_path / "RELEASE_NOTES.md"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--changelog",
            str(tmp_path / "CHANGELOG.md"),
            "--template",
            str(tmp_path / "TEMPLATE.md"),
            "--fragments-dir",
            str(fragments_dir),
            "--version",
            version,
            "--previous-version",
            previous,
            "--repository",
            _REPOSITORY,
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output


def test_section_between_two_others_composes_the_exact_body(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.26.0")

    assert result.returncode == 0, result.stdout
    assert output.read_text(encoding="utf-8") == (
        "### Breaking Changes\n"
        "\n"
        "- Removed a setting. ([#2](https://example.test/pull/2))\n"
        "\n"
        "### Bug Fixes\n"
        "\n"
        "- Fixed a crash. ([#1](https://example.test/pull/1))\n"
        "\n"
        "## Install or update\n"
        "\n"
        "Get v4.26.0 from 4.26.0.\n"
        "\n"
        "**Full Changelog**: https://github.com/stickerdaniel/linkedin-mcp-server"
        "/compare/v4.25.0...v4.26.0\n"
    )


def test_section_at_end_of_file_with_no_significant_changes(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.25.1", previous="4.25.0")

    assert result.returncode == 0, result.stdout
    assert output.read_text(encoding="utf-8") == (
        "No significant changes.\n"
        "\n"
        "## Install or update\n"
        "\n"
        "Get v4.25.1 from 4.25.1.\n"
        "\n"
        "**Full Changelog**: https://github.com/stickerdaniel/linkedin-mcp-server"
        "/compare/v4.25.0...v4.25.1\n"
    )


def test_version_is_matched_literally(tmp_path: Path) -> None:
    decoy = _CHANGELOG.replace("## 4.26.0 (", "## 4x26y0 (")

    result, output = _compose(tmp_path, "4.26.0", changelog=decoy)

    assert result.returncode == 1
    assert not output.exists()
    assert "::error::CHANGELOG.md has no section for 4.26.0." in result.stdout


def test_missing_section_names_the_build_command(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.28.0")

    assert result.returncode == 1
    assert not output.exists()
    assert "no section for 4.28.0" in result.stdout
    assert "uv run towncrier build --version 4.28.0 --yes" in result.stdout


def test_duplicate_dated_sections_are_rejected(tmp_path: Path) -> None:
    changelog = _CHANGELOG.replace(
        "## 4.25.1 (2026-09-01)", "## 4.26.0 (2026-09-25)\n\n- Again.\n\n## 4.25.1"
    )

    result, output = _compose(tmp_path, "4.26.0", changelog=changelog)

    assert result.returncode == 1
    assert not output.exists()
    assert "CHANGELOG.md has 2 sections for 4.26.0." in result.stdout


def test_whitespace_only_section_is_rejected(tmp_path: Path) -> None:
    changelog = _CHANGELOG.replace(
        "## 4.25.1 (2026-09-01)\n\nNo significant changes.\n",
        "## 4.25.1 (2026-09-01)\n\n   \n\t\n",
    )

    result, output = _compose(tmp_path, "4.25.1", changelog=changelog)

    assert result.returncode == 1
    assert not output.exists()
    assert "empty section for 4.25.1" in result.stdout


def test_leftover_fragment_stops_the_release(tmp_path: Path) -> None:
    result, output = _compose(
        tmp_path, "4.26.0", fragments=("README.md", "1080.fix.md")
    )

    assert result.returncode == 1
    assert not output.exists()
    assert "Fragments remain after the version bump: 1080.fix.md." in result.stdout


def test_template_without_install_heading_is_rejected(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.26.0", template="Get v${VERSION}.\n")

    assert result.returncode == 1
    assert not output.exists()
    assert "no `## Install or update` heading" in result.stdout


def test_repository_template_composes(tmp_path: Path) -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")

    result, output = _compose(tmp_path, "4.26.0", template=template)

    assert result.returncode == 0, result.stdout
    body = output.read_text(encoding="utf-8")
    assert "$" not in body
    assert "linkedin-mcp-server-v4.26.0.mcpb" in body
    assert body.index("### Bug Fixes") < body.index("## Install or update")
    assert body.rstrip("\n").splitlines()[-1].startswith("**Full Changelog**: ")


def _workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    [step] = [step for step in job["steps"] if step.get("name") == name]
    return step


def test_notes_are_composed_before_anything_is_published() -> None:
    jobs = _workflow()["jobs"]
    check = jobs["check-version-bump"]
    names = [step.get("name") for step in check["steps"]]

    assert names.index("Check if version was bumped") < names.index(
        "Compose release notes"
    )
    assert names.index("Compose release notes") < names.index("Upload release notes")
    assert jobs["build"]["needs"] == "check-version-bump"
    assert "build" in jobs["publish-pypi"]["needs"]

    compose = _step(check, "Compose release notes")
    assert compose["if"] == _SHOULD_RELEASE
    assert compose["run"].startswith("set -euo pipefail\n")
    assert "git show HEAD~1:pyproject.toml" in compose["run"]
    assert "tomllib" in compose["run"]
    assert "|| echo" not in compose["run"]
    assert (
        'git ls-remote --exit-code --tags origin "refs/tags/v$PREVIOUS"'
        in compose["run"]
    )
    assert "scripts/compose_release_notes.py" in compose["run"]
    assert "--output RELEASE_NOTES.md" in compose["run"]

    upload = _step(check, "Upload release notes")
    assert upload["if"] == _SHOULD_RELEASE
    assert upload["with"] == {
        "name": "release-notes",
        "path": "RELEASE_NOTES.md",
        "if-no-files-found": "error",
        "overwrite": True,
        "retention-days": 7,
    }


def test_release_body_comes_from_the_composed_notes() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    jobs = _workflow()["jobs"]
    release = jobs["create-github-release"]

    assert "generate-notes" not in text
    assert "envsubst" not in text
    assert "RELEASE_BODY" not in text
    assert not any(
        "actions/checkout" in step.get("uses", "") for step in release["steps"]
    )
    downloads = [
        step["with"]["name"]
        for step in release["steps"]
        if "actions/download-artifact" in step.get("uses", "")
    ]
    assert downloads == ["github-release-assets", "release-notes"]
    create = _step(release, "Create GitHub Release")
    assert create["with"]["body_path"] == "RELEASE_NOTES.md"
    assert create["with"]["generate_release_notes"] is False
    assert "*.mcpb" in create["with"]["files"]

    assets = _step(jobs["build-mcpb"], "Upload release assets")
    assert "RELEASE_NOTES" not in assets["with"]["path"]


# The prepare-release job holds the admin token. This change must leave it
# byte for byte as it was at 4b8117bb; an intended edit to that job updates
# the digest in the same pull request.
_PREPARE_RELEASE_SHA256 = (
    "b61da44f9e3bac83bbd78c0432d5cc0e38f179f2d56ece62a1554bcac16a2180"
)


def test_prepare_release_job_is_unchanged() -> None:
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines(keepends=True)
    start = lines.index("  prepare-release:\n")
    end = next(
        index
        for index in range(start + 1, len(lines))
        if re.match(r"  \S", lines[index])
    )
    block = "".join(lines[start:end])

    assert hashlib.sha256(block.encode("utf-8")).hexdigest() == (
        _PREPARE_RELEASE_SHA256
    )
