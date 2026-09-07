"""Contracts for pull request title validation and its workflows."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, cast

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_pr_title.py"
_SPEC = importlib.util.spec_from_file_location("check_pr_title", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_VALIDATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATOR)

ALLOWED_TYPES = cast(tuple[str, ...], _VALIDATOR.ALLOWED_TYPES)
FINAL_PERIOD = cast(str, _VALIDATOR.FINAL_PERIOD)
INVALID_SCOPE = cast(str, _VALIDATOR.INVALID_SCOPE)
INVALID_SHAPE = cast(str, _VALIDATOR.INVALID_SHAPE)
INVALID_SUBJECT = cast(str, _VALIDATOR.INVALID_SUBJECT)
UNSAFE_CHARACTER = cast(str, _VALIDATOR.UNSAFE_CHARACTER)
UNSUPPORTED_TYPE = cast(str, _VALIDATOR.UNSUPPORTED_TYPE)
validate_title = cast(Callable[[str], str | None], _VALIDATOR.validate_title)

_CHECK_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "check-pr-title.yml"
_LABEL_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "label-pr.yml"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_CHECKOUT = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"


@pytest.mark.parametrize(
    "title",
    [
        "feat: Add title validation",
        "fix(parser): Reject an invalid title",
        "docs!: Rewrite the guide",
        "refactor(release workflow)!: Restore required checks",
    ],
)
def test_accepted_shapes(title: str) -> None:
    assert validate_title(title) is None


@pytest.mark.parametrize("type_name", ALLOWED_TYPES)
def test_every_allowed_type_is_accepted(type_name: str) -> None:
    assert validate_title(f"{type_name}: Accept this title") is None


def test_build_type_is_rejected() -> None:
    assert validate_title("build: Package the release") == UNSUPPORTED_TYPE


@pytest.mark.parametrize(
    ("title", "diagnostic"),
    [
        ("fix:Add spacing", INVALID_SHAPE),
        ("fix:  Add spacing", INVALID_SUBJECT),
        ("fix : Add spacing", INVALID_SHAPE),
        (" fix: Add spacing", INVALID_SHAPE),
        ("fix(scope) : Add spacing", INVALID_SHAPE),
        ("fix(scope)! : Add spacing", INVALID_SHAPE),
        ("fix(scope)!!: Add spacing", INVALID_SHAPE),
        ("fix!!: Add spacing", INVALID_SHAPE),
        ("fix!extra: Add spacing", INVALID_SHAPE),
        ("fix(): Add spacing", INVALID_SCOPE),
        ("fix( scope): Add spacing", INVALID_SCOPE),
        ("fix(scope ): Add spacing", INVALID_SCOPE),
        ("fix: ", INVALID_SUBJECT),
        ("fix:  ", INVALID_SUBJECT),
        ("fix: Add spacing ", INVALID_SUBJECT),
        ("fix: Add spacing.", FINAL_PERIOD),
    ],
)
def test_invalid_structure_has_deterministic_diagnostics(
    title: str, diagnostic: str
) -> None:
    assert validate_title(title) == diagnostic


@pytest.mark.parametrize(
    "space",
    [
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        "　",
    ],
)
def test_scope_boundary_whitespace_is_rejected(space: str) -> None:
    assert validate_title(f"fix({space}scope): Keep boundaries") == INVALID_SCOPE
    assert validate_title(f"fix(scope{space}): Keep boundaries") == INVALID_SCOPE


@pytest.mark.parametrize(
    "space",
    [
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        " ",
        "　",
    ],
)
def test_subject_boundary_whitespace_is_rejected(space: str) -> None:
    assert validate_title(f"fix: {space}Keep boundaries") == INVALID_SUBJECT
    assert validate_title(f"fix: Keep boundaries{space}") == INVALID_SUBJECT


@pytest.mark.parametrize(
    "character",
    [
        "\x00",
        "\x1f",
        "\x7f",
        "\x80",
        "\x9f",
        "؜",
        "‎",
        "‏",
        " ",
        " ",
        "‪",
        "‮",
        "⁦",
        "⁩",
        "﻿",
        "�",
    ],
)
def test_unsafe_characters_are_rejected_anywhere(character: str) -> None:
    assert validate_title(f"fix: Before{character}after") == UNSAFE_CHARACTER


def test_unsafe_character_precedes_other_diagnostics() -> None:
    assert validate_title("build: Invalid.\n") == UNSAFE_CHARACTER


@pytest.mark.parametrize(
    "title",
    [
        "feat(HTTP/API v2): Preserve internal punctuation",
        "fix: Handle déjà vu safely",
        "docs: Explain 日本語 titles",
        "test: Permit commas, semicolons; and question marks?",
        "chore: Permit a Unicode full stop。",
        "perf(scope! #42): Permit punctuation in scope",
    ],
)
def test_unicode_and_internal_punctuation_are_accepted(title: str) -> None:
    assert validate_title(title) is None


def _run_cli(title: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if title is None:
        env.pop("PR_TITLE", None)
    else:
        env["PR_TITLE"] = title
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "title",
    [
        "build: payload%0A::warning::leaked",
        "build: ::warning:: forged annotation",
        "fix: actual\rcontrol",
        "fix: actual\ncontrol",
    ],
)
def test_invalid_cli_input_emits_one_safe_error_annotation(title: str) -> None:
    result = _run_cli(title)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert sum(line.startswith("::error::") for line in output.splitlines()) == 1
    assert "Expected: type: subject" in output
    assert title not in output
    assert "::warning::" not in output


def test_missing_cli_title_emits_one_error_annotation() -> None:
    result = _run_cli(None)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert sum(line.startswith("::error::") for line in output.splitlines()) == 1
    assert "::error::PR title is missing." in output


def test_valid_cli_title_exits_without_output() -> None:
    result = _run_cli("fix(cli): Accept safe input")

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_pr_title_workflow_runs_only_trusted_validator() -> None:
    workflow = _CHECK_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.startswith("name: PR Title\n")
    assert "pull_request_target:" in workflow
    assert "types: [opened, edited, reopened, synchronize]" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: write" not in workflow
    assert "statuses: write" not in workflow
    assert "group: pr-title-${{ github.event.pull_request.number }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert workflow.count("name: PR Title") == 2
    assert _CHECKOUT in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" in workflow
    assert "python3 scripts/check_pr_title.py" in workflow
    assert "github.event.pull_request.head" not in workflow
    assert "uv " not in workflow
    assert "pip " not in workflow


def test_label_workflow_matches_breaking_marker_without_normalizing() -> None:
    workflow = _LABEL_WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$PR_TITLE" =~ ^([a-z]+)(\\([^()]+\\))?!?:\\ [^[:space:]] ]]' in workflow
    assert 'TYPE="${BASH_REMATCH[1]}"' in workflow
    assert 'TYPE="${TYPE// /}"' not in workflow
    assert "|build|" not in workflow
    assert 'test|perf) LABEL="chore"' in workflow


def test_release_restores_pr_title_required_check() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert '{"context": "PR Title", "app_id": 15368}' in workflow
