"""Contracts for the changelog fragment gate in the PR Title workflow."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_changelog_fragment.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "check-pr-title.yml"
_NUMBER = 1234
_SENTENCE = "@@ -0,0 +1 @@\n+Search results keep their order."


def _file(
    name: str, status: str = "added", patch: str | None = _SENTENCE
) -> dict[str, Any]:
    entry: dict[str, Any] = {"filename": name, "status": status}
    if patch is not None:
        entry["patch"] = patch
    return entry


_CODE = _file("linkedin_mcp_server/server.py", "modified", "@@ -1 +1 @@\n-a\n+b")


def _run(
    tmp_path: Path,
    title: str,
    files: list[dict[str, Any]],
    *,
    pages: Any = None,
    pr: Any = None,
) -> subprocess.CompletedProcess[str]:
    pr_json = tmp_path / "pull-request.json"
    files_json = tmp_path / "pull-request-files.json"
    pr_json.write_text(
        json.dumps({"number": _NUMBER, "title": title} if pr is None else pr),
        encoding="utf-8",
    )
    files_json.write_text(
        json.dumps([files] if pages is None else pages), encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--pr-json",
            str(pr_json),
            "--files-json",
            str(files_json),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _errors(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [
        line.removeprefix("::error::")
        for line in result.stdout.splitlines()
        if line.startswith("::error::")
    ]


def test_feat_without_fragment_names_the_exact_path(tmp_path: Path) -> None:
    result = _run(tmp_path, "feat: Add company search", [_CODE])

    assert result.returncode == 1
    [error] = _errors(result)
    assert "changelog.d/1234.feat.md" in error
    assert "towncrier create" in error


@pytest.mark.parametrize(
    ("title", "fragment"),
    [
        ("feat: Add company search", "changelog.d/1234.feat.md"),
        ("fix: Keep search order", "changelog.d/1234.fix.md"),
        ("fix(deps): Raise vulnerable floors", "changelog.d/1234.fix.md"),
    ],
)
def test_required_fragment_present_passes(
    tmp_path: Path, title: str, fragment: str
) -> None:
    result = _run(tmp_path, title, [_CODE, _file(fragment)])

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_fix_deps_requires_a_fix_fragment(tmp_path: Path) -> None:
    result = _run(tmp_path, "fix(deps): Raise vulnerable floors", [_CODE])

    assert result.returncode == 1
    assert "changelog.d/1234.fix.md" in _errors(result)[0]


@pytest.mark.parametrize(
    "title",
    [
        "feat!: Replace the public contract",
        "fix(scope)!: Change the error shape",
        "docs!: Remove an old workflow",
        "refactor(config)!: Change the contract",
    ],
)
def test_breaking_marker_requires_breaking_fragment(tmp_path: Path, title: str) -> None:
    other_types = [_file("changelog.d/1234.feat.md"), _file("changelog.d/1234.fix.md")]

    missing = _run(tmp_path, title, [_CODE, *other_types])
    present = _run(tmp_path, title, [_CODE, _file("changelog.d/1234.breaking.md")])

    assert missing.returncode == 1
    [error] = _errors(missing)
    assert "changelog.d/1234.breaking.md" in error
    assert present.returncode == 0, present.stdout


@pytest.mark.parametrize(
    "title",
    ["fix: Handle bangs!", "fix(parser!): Handle punctuation"],
)
def test_bang_outside_the_marker_is_not_breaking(tmp_path: Path, title: str) -> None:
    breaking_only = _run(tmp_path, title, [_file("changelog.d/1234.breaking.md")])
    fix = _run(tmp_path, title, [_file("changelog.d/1234.fix.md")])

    assert breaking_only.returncode == 1
    assert "changelog.d/1234.fix.md" in _errors(breaking_only)[0]
    assert fix.returncode == 0, fix.stdout


def test_another_pull_requests_fragment_does_not_count(tmp_path: Path) -> None:
    result = _run(
        tmp_path, "feat: Add company search", [_file("changelog.d/999.feat.md")]
    )

    assert result.returncode == 1
    [error] = _errors(result)
    assert "changelog.d/1234.feat.md" in error


@pytest.mark.parametrize("status", ["modified", "removed"])
def test_fragment_that_is_not_added_does_not_count(tmp_path: Path, status: str) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_file("changelog.d/1234.feat.md", status)],
    )

    assert result.returncode == 1
    assert any("changelog.d/1234.feat.md" in error for error in _errors(result))


@pytest.mark.parametrize("patch", ["@@ -0,0 +1,2 @@\n+   \n+\t", None])
def test_fragment_without_text_is_rejected(tmp_path: Path, patch: str | None) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_file("changelog.d/1234.feat.md", patch=patch)],
    )

    assert result.returncode == 1
    assert _errors(result) == [
        "changelog.d/1234.feat.md is empty. Write one user-facing sentence."
    ]


@pytest.mark.parametrize(
    "name",
    [
        "changelog.d/1234.feature.md",
        "changelog.d/1234.feat.1.md",
        "changelog.d/1234.feat",
        "changelog.d/notes.md",
        "changelog.d/sub/1234.feat.md",
        "changelog.d/+orphan.feat.md",
    ],
)
def test_malformed_extra_fails_beside_a_valid_fragment(
    tmp_path: Path, name: str
) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_file("changelog.d/1234.feat.md"), _file(name)],
    )

    assert result.returncode == 1
    [error] = _errors(result)
    assert error.startswith(f"{name} is not a fragment name.")


def test_malformed_fragment_fails_an_exempt_title(tmp_path: Path) -> None:
    result = _run(tmp_path, "docs: Explain setup", [_file("changelog.d/notes.md")])

    assert result.returncode == 1
    [error] = _errors(result)
    assert error.startswith("changelog.d/notes.md is not a fragment name.")


@pytest.mark.parametrize(
    "title",
    [
        "docs: Explain setup",
        "style: Reformat",
        "refactor: Split the module",
        "test: Cover the gate",
        "chore: Bump version to 4.26.0",
        "chore(deps): Update pytest",
        "perf: Cache the parse",
        "ci: Pin an action",
    ],
)
def test_exempt_titles_need_no_fragment(tmp_path: Path, title: str) -> None:
    result = _run(tmp_path, title, [_CODE])

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_invalid_title_adds_no_requirement(tmp_path: Path) -> None:
    # The preceding step fails the job for the title itself.
    result = _run(tmp_path, "feat: Ends with a period.", [_CODE])

    assert result.returncode == 0, result.stdout


def test_version_bump_may_delete_every_fragment(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "chore: Bump version to 4.26.0",
        [
            _file("CHANGELOG.md", "modified"),
            _file("changelog.d/1076.fix.md", "removed", "@@ -1 +0,0 @@\n-Old."),
            _file("changelog.d/weird-name.md", "removed", "@@ -1 +0,0 @@\n-Old."),
        ],
    )

    assert result.returncode == 0, result.stdout


def test_fragment_readme_is_not_a_fragment(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "docs: Explain fragments",
        [_file("changelog.d/README.md", "modified")],
    )

    assert result.returncode == 0, result.stdout


def test_fragment_on_a_later_page_counts(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [],
        pages=[[_CODE, _CODE], [_file("changelog.d/1234.feat.md")]],
    )

    assert result.returncode == 0, result.stdout


def test_unsafe_file_name_is_not_echoed(tmp_path: Path) -> None:
    name = "changelog.d/x\n::warning::forged.md"
    result = _run(tmp_path, "docs: Explain setup", [_file(name)])

    assert result.returncode == 1
    assert "::warning::" not in result.stdout + result.stderr
    assert _errors(result)[0].startswith("a file with an unsafe name")


@pytest.mark.parametrize(
    ("pr", "pages"),
    [
        ({"title": "docs: Explain setup"}, [[]]),
        ({"number": "1234", "title": "docs: Explain setup"}, [[]]),
        ({"number": True, "title": "docs: Explain setup"}, [[]]),
        ({"number": _NUMBER, "title": None}, [[]]),
        ([], [[]]),
    ],
)
def test_malformed_pull_request_data_fails(tmp_path: Path, pr: Any, pages: Any) -> None:
    result = _run(tmp_path, "", [], pr=pr, pages=pages)

    assert result.returncode == 1
    assert _errors(result) == ["Unable to read current pull request data."]


@pytest.mark.parametrize(
    "pages",
    [
        {"files": []},
        [_file("changelog.d/1234.feat.md")],
        [[{"status": "added"}]],
        [[{"filename": "a.md", "status": None}]],
        [[{"filename": "a.md", "status": "added", "patch": 1}]],
    ],
)
def test_malformed_files_data_fails(tmp_path: Path, pages: Any) -> None:
    result = _run(tmp_path, "docs: Explain setup", [], pages=pages)

    assert result.returncode == 1
    assert _errors(result) == ["Unable to read the pull request's changed files."]


def test_missing_input_files_fail(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--pr-json",
            str(tmp_path / "missing.json"),
            "--files-json",
            str(tmp_path / "missing-files.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert _errors(result) == ["Unable to read current pull request data."]


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["check"]["steps"]


def test_pr_title_workflow_runs_the_gate_after_the_title() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert workflow["jobs"]["check"]["name"] == "PR Title"
    assert [step["name"] for step in _steps()] == [
        "Checkout the trusted workflow revision",
        "Fetch current pull request",
        "Fetch changed files",
        "Validate current pull request title",
        "Require a changelog fragment",
    ]


def test_changed_files_are_fetched_as_every_page() -> None:
    fetch = _steps()[2]

    assert fetch["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
    }
    assert fetch["run"].startswith("set -euo pipefail\n")
    assert "--paginate --slurp" in fetch["run"]
    assert (
        '"repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/files?per_page=100"'
        in (fetch["run"])
    )
    assert '> "$RUNNER_TEMP/pull-request-files.json"' in fetch["run"]
    assert "--jq" not in fetch["run"]


def test_no_step_after_the_fetches_holds_a_token() -> None:
    gate = _steps()[4]
    assert all("env" not in step for step in _steps()[3:])

    assert "env" not in gate
    assert "secrets" not in json.dumps(gate)
    assert gate["run"].split() == [
        "python3",
        "scripts/check_changelog_fragment.py",
        "\\",
        "--pr-json",
        '"$RUNNER_TEMP/pull-request.json"',
        "\\",
        "--files-json",
        '"$RUNNER_TEMP/pull-request-files.json"',
    ]
