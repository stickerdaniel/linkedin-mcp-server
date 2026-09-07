"""Contracts for pull request title validation and its workflows."""

from __future__ import annotations

import importlib.util
import json
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
INVALID_PR_DATA = cast(str, _VALIDATOR.INVALID_PR_DATA)
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
        "\N{WORD JOINER}",
        "﻿",
        "�",
    ],
)
def test_unsafe_characters_are_rejected_anywhere(character: str) -> None:
    assert validate_title(f"fix: Before{character}after") == UNSAFE_CHARACTER


@pytest.mark.parametrize(
    "title",
    [
        "fix: ​",
        "fix(​): Subject",
        "fix: Subject.​",
    ],
)
def test_zero_width_space_is_rejected_anywhere(title: str) -> None:
    assert validate_title(title) == UNSAFE_CHARACTER


@pytest.mark.parametrize(
    "title",
    [
        "fix: ​",
        "fix(​): Subject",
        "fix: Subject.​",
    ],
)
def test_zero_width_space_cli_input_is_rejected(title: str) -> None:
    result = _run_cli(title)

    assert result.returncode != 0
    assert f"::error::{UNSAFE_CHARACTER}" in result.stdout
    assert title not in result.stdout + result.stderr


def test_unsafe_character_precedes_other_diagnostics() -> None:
    assert validate_title("build: Invalid.\n") == UNSAFE_CHARACTER


def test_word_joiner_is_globally_unsafe() -> None:
    assert validate_title("fix: Before\N{WORD JOINER}after") == UNSAFE_CHARACTER


@pytest.mark.parametrize(
    "character",
    ["\N{SOFT HYPHEN}", "\N{ZERO WIDTH JOINER}"],
)
def test_format_characters_at_boundaries_are_rejected(character: str) -> None:
    assert validate_title(f"fix({character}scope): Keep boundaries") == INVALID_SCOPE
    assert validate_title(f"fix(scope{character}): Keep boundaries") == INVALID_SCOPE
    assert validate_title(f"fix: {character}Keep boundaries") == INVALID_SUBJECT
    assert validate_title(f"fix: Keep boundaries{character}") == INVALID_SUBJECT


@pytest.mark.parametrize(
    ("title", "diagnostic"),
    [
        ("fix(\N{COMBINING ACUTE ACCENT}): Subject", INVALID_SCOPE),
        ("fix: \N{COMBINING ACUTE ACCENT}", INVALID_SUBJECT),
    ],
)
def test_scope_and_subject_require_substantive_content(
    title: str, diagnostic: str
) -> None:
    assert validate_title(title) == diagnostic


@pytest.mark.parametrize(
    "trailing",
    ["\N{SOFT HYPHEN}", "\N{COMBINING ACUTE ACCENT}"],
)
def test_final_period_uses_last_substantive_codepoint(trailing: str) -> None:
    assert validate_title(f"fix: Do not hide.{trailing}") == FINAL_PERIOD


def test_decomposed_accents_are_accepted() -> None:
    assert (
        validate_title(
            "fix(cafe\N{COMBINING ACUTE ACCENT}): "
            "Handle resume\N{COMBINING ACUTE ACCENT}"
        )
        is None
    )


def test_internal_emoji_zwj_sequence_is_accepted() -> None:
    assert (
        validate_title(
            "fix: Support \N{WOMAN}\N{ZERO WIDTH JOINER}\N{PERSONAL COMPUTER} profiles"
        )
        is None
    )


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


def _run_cli(
    title: str | None, pr_json: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if title is None:
        env.pop("PR_TITLE", None)
    else:
        env["PR_TITLE"] = title
    command = [sys.executable, str(_SCRIPT)]
    if pr_json is not None:
        command.extend(["--pr-json", str(pr_json)])
    return subprocess.run(
        command,
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


def test_cli_reads_current_title_from_pr_json(tmp_path: Path) -> None:
    pr_json = tmp_path / "pull-request.json"
    pr_json.write_text(
        json.dumps({"title": "fix(cli): Read current API data"}), encoding="utf-8"
    )

    result = _run_cli("build: Ignore stale event data", pr_json)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "{}",
        '{"title": null}',
        "[]",
    ],
)
def test_cli_rejects_missing_or_malformed_pr_json(
    tmp_path: Path, contents: str
) -> None:
    pr_json = tmp_path / "pull-request.json"
    pr_json.write_text(contents, encoding="utf-8")

    result = _run_cli("fix: Stale event title", pr_json)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert output.count(f"::error::{INVALID_PR_DATA}") == 1
    assert contents not in output
    assert "Stale event title" not in output


def test_cli_rejects_missing_pr_json_file(tmp_path: Path) -> None:
    result = _run_cli("fix: Stale event title", tmp_path / "missing.json")

    assert result.returncode != 0
    assert f"::error::{INVALID_PR_DATA}" in result.stdout
    assert "missing.json" not in result.stdout + result.stderr


def test_pr_title_workflow_is_automatic_required_check() -> None:
    workflow = _CHECK_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.startswith("name: PR Title\n")
    assert workflow.count("name: PR Title") == 2
    assert workflow.count("pull_request_target:") == 1
    assert workflow.count("types: [opened, edited, reopened, synchronize]") == 1
    assert "issue_comment:" not in workflow
    assert "types: [created]" not in workflow
    assert "workflow_dispatch:" not in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "pull-requests: write" not in workflow
    assert "group: pr-title-${{ github.event.pull_request.number }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert _CHECKOUT in workflow
    assert "ref: ${{ github.workflow_sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "github.event.pull_request.head" not in workflow
    assert "github.head_ref" not in workflow
    assert "refs/pull/" not in workflow
    assert "uv " not in workflow
    assert "pip " not in workflow


def test_pr_title_workflow_fetches_current_api_data() -> None:
    workflow = _CHECK_WORKFLOW.read_text(encoding="utf-8")

    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "PR_NUMBER: ${{ github.event.pull_request.number }}" in workflow
    assert '"repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"' in workflow
    assert '> "$RUNNER_TEMP/pull-request.json"' in workflow
    assert (
        'python3 scripts/check_pr_title.py --pr-json "$RUNNER_TEMP/pull-request.json"'
        in workflow
    )
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" not in workflow
    assert "github.event.pull_request.title" not in workflow

    validator_step = workflow.split(
        "- name: Validate current pull request title", maxsplit=1
    )[1]
    assert "GH_TOKEN" not in validator_step
    assert "secrets.GITHUB_TOKEN" not in validator_step


def test_pr_title_workflow_has_no_fallback_or_status_layer() -> None:
    workflow = _CHECK_WORKFLOW.read_text(encoding="utf-8")

    assert "issue_comment:" not in workflow
    assert "github.event.comment" not in workflow
    assert "/check-pr-title" not in workflow
    assert "statuses: write" not in workflow
    assert "/statuses/" not in workflow
    assert "HEAD_SHA" not in workflow
    assert "head_sha" not in workflow
    assert '["head"]["sha"]' not in workflow
    assert "Publish pending PR Title status" not in workflow
    assert "Publish final PR Title status" not in workflow


def test_pr_title_workflow_documents_sha_like_branch_recovery() -> None:
    workflow = _CHECK_WORKFLOW.read_text(encoding="utf-8")

    assert "suppresses pull_request_target for SHA-like source branch names" in workflow
    assert "Rename the source branch" in workflow


def test_label_workflow_matches_breaking_marker_without_normalizing() -> None:
    workflow = _LABEL_WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$PR_TITLE" =~ ^([a-z]+)(\\([^()]+\\))?!?:\\ [^[:space:]] ]]' in workflow
    assert 'TYPE="${BASH_REMATCH[1]}"' in workflow
    assert 'TYPE="${TYPE// /}"' not in workflow
    assert "|build|" not in workflow
    assert 'test|perf) LABEL="chore"' in workflow


def test_label_workflow_uses_current_title_and_per_pr_concurrency() -> None:
    workflow = _LABEL_WORKFLOW.read_text(encoding="utf-8")

    assert "group: label-pr-${{ github.event.pull_request.number }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "set -euo pipefail" in workflow
    assert '"repos/${REPO}/pulls/${PR_NUMBER}" > "$PR_JSON"' in workflow
    assert "--paginate --slurp" in workflow
    assert (
        '"repos/${REPO}/issues/${PR_NUMBER}/labels?per_page=100" > "$LABELS_JSON"'
        in workflow
    )
    assert "< <(" not in workflow
    assert "PR_TITLE: ${{ github.event.pull_request.title }}" not in workflow
    assert "github.event.pull_request.title" not in workflow


def test_label_workflow_removes_only_attached_stale_labels() -> None:
    workflow = _LABEL_WORKFLOW.read_text(encoding="utf-8")

    assert "ATTACHED_DERIVED=" in workflow
    assert 'if is_attached "$stale"; then' in workflow
    assert '[ -n "$LABEL" ] && ! is_attached "$LABEL"' in workflow
    assert '--remove-label "$stale"' in workflow
    assert "|| true" not in workflow
    assert "2>/dev/null" not in workflow
    assert "Labels outside this fixed derived set are untouched." in workflow


def test_release_restores_pr_title_required_check() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert '{"context": "PR Title", "app_id": 15368}' in workflow
