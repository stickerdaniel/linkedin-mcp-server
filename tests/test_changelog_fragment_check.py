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
    if pages is None:
        pages = [files]
    if pr is None:
        pr = {
            "number": _NUMBER,
            "title": title,
            "changed_files": sum(len(page) for page in pages),
        }
    pr_json.write_text(json.dumps(pr), encoding="utf-8")
    files_json.write_text(json.dumps(pages), encoding="utf-8")
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


_RENOVATE = {"login": "renovate[bot]", "type": "Bot"}
_RENOVATE_TITLE = "fix(deps): update dependency fastmcp to v4"


def _renovate_pr(files: list[dict[str, Any]], user: Any = _RENOVATE) -> dict:
    return {
        "number": _NUMBER,
        "title": _RENOVATE_TITLE,
        "changed_files": len(files),
        "user": user,
    }


def test_renovate_needs_no_fragment(tmp_path: Path) -> None:
    files = [_CODE]
    result = _run(tmp_path, _RENOVATE_TITLE, files, pr=_renovate_pr(files))

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


@pytest.mark.parametrize(
    "user",
    [
        {"login": "renovate[bot]", "type": "User"},
        {"login": "renovate", "type": "Bot"},
        {"login": "stickerdaniel", "type": "User"},
        "renovate[bot]",
    ],
)
def test_only_the_renovate_app_is_exempt(tmp_path: Path, user: Any) -> None:
    files = [_CODE]
    result = _run(tmp_path, _RENOVATE_TITLE, files, pr=_renovate_pr(files, user))

    assert result.returncode == 1
    assert "changelog.d/1234.fix.md" in _errors(result)[0]


@pytest.mark.parametrize(
    ("fragment", "passes"),
    [("changelog.d/1234.fix.md", True), ("changelog.d/1234.feat.md", False)],
)
def test_a_fragment_on_a_renovate_pr_must_match_its_title(
    tmp_path: Path, fragment: str, passes: bool
) -> None:
    files = [_CODE, _file(fragment)]
    result = _run(tmp_path, _RENOVATE_TITLE, files, pr=_renovate_pr(files))

    assert (result.returncode == 0) is passes, result.stdout
    if not passes:
        assert "changelog.d/1234.fix.md" in _errors(result)[0]


def test_a_fragment_renamed_onto_a_renovate_pr_must_match_its_title(
    tmp_path: Path,
) -> None:
    renamed = _file("changelog.d/1234.feat.md", "renamed")
    files = [_CODE, renamed]
    result = _run(tmp_path, _RENOVATE_TITLE, files, pr=_renovate_pr(files))

    assert result.returncode == 1
    assert "changelog.d/1234.fix.md" in _errors(result)[0]


def test_renovate_fragment_is_still_checked(tmp_path: Path) -> None:
    files = [_CODE, _file("changelog.d/1234.fix.md", patch="@@ -0,0 +1 @@\n+")]
    result = _run(tmp_path, _RENOVATE_TITLE, files, pr=_renovate_pr(files))

    assert result.returncode == 1
    [error] = _errors(result)
    assert "is empty" in error


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


def test_fragment_without_text_is_rejected(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_file("changelog.d/1234.feat.md", patch="@@ -0,0 +1,2 @@\n+   \n+\t")],
    )

    assert result.returncode == 1
    assert _errors(result) == [
        "changelog.d/1234.feat.md is empty. Write one user-facing sentence."
    ]


_NO_NEWLINE = "\\ No newline at end of file"


def _not_a_text_file(path: str) -> str:
    return (
        f"{path} must be a text file ending in a newline, "
        "as `towncrier create` writes it."
    )


@pytest.mark.parametrize(
    "patch",
    [
        # How the files API shows an added symlink to AGENTS.md (d17cca15).
        f"@@ -0,0 +1 @@\n+AGENTS.md\n{_NO_NEWLINE}",
        f"@@ -0,0 +1,2 @@\n+Search results keep\n+their order.\n{_NO_NEWLINE}",
        None,
    ],
    ids=["symlink", "no-final-newline", "no-patch"],
)
def test_added_fragment_that_is_not_a_text_file_is_rejected(
    tmp_path: Path, patch: str | None
) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_CODE, _file("changelog.d/1234.feat.md", patch=patch)],
    )

    assert result.returncode == 1
    assert _errors(result) == [_not_a_text_file("changelog.d/1234.feat.md")]


@pytest.mark.parametrize(
    "patch",
    [
        f"@@ -1 +1 @@\n-Old text.\n{_NO_NEWLINE}\n+Old text.",
        f"@@ -1,2 +1 @@\n Old text.\n-Dropped line.\n{_NO_NEWLINE}",
    ],
    ids=["line-rewritten", "last-line-dropped"],
)
def test_modified_fragment_gaining_its_final_newline_passes(
    tmp_path: Path, patch: str
) -> None:
    result = _run(
        tmp_path,
        "docs: Explain setup",
        [_file("changelog.d/1076.fix.md", "modified", patch)],
    )

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "patch",
    [
        f"@@ -1 +1 @@\n-Old text.\n+New text.\n{_NO_NEWLINE}",
        f"@@ -1,2 +1,2 @@\n-Old text.\n+New text.\n Kept line.\n{_NO_NEWLINE}",
        None,
    ],
    ids=["changed-line", "context-line", "no-patch"],
)
def test_modified_fragment_that_is_not_a_text_file_is_rejected(
    tmp_path: Path, patch: str | None
) -> None:
    result = _run(
        tmp_path,
        "docs: Explain setup",
        [_file("changelog.d/1076.fix.md", "modified", patch)],
    )

    assert result.returncode == 1
    assert _errors(result) == [_not_a_text_file("changelog.d/1076.fix.md")]


# How the PR files API shows a gitlink (submodule), from pytorch/pytorch
# pull requests 17184 (added) and 192366 (modified).
_ADDED_GITLINK = (
    "@@ -0,0 +1 @@\n+Subproject commit 58cbf0ee1310fc42df6e8e244db023d4da052e4d"
)
_MODIFIED_GITLINK = (
    "@@ -1 +1 @@\n"
    "-Subproject commit d03662f0984f652b60e7ddce53d3868002275197\n"
    "+Subproject commit 97bf890db679505a14dfe547a5e77bb2bd05dc90"
)


@pytest.mark.parametrize(
    "patch", [_ADDED_GITLINK, _MODIFIED_GITLINK], ids=["added", "modified"]
)
def test_gitlink_named_as_own_fragment_is_rejected(tmp_path: Path, patch: str) -> None:
    result = _run(
        tmp_path,
        "feat: Add company search",
        [_CODE, _file("changelog.d/1234.feat.md", patch=patch)],
    )

    assert result.returncode == 1
    assert _errors(result) == [_not_a_text_file("changelog.d/1234.feat.md")]


def test_gitlink_fragment_fails_an_exempt_title(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "docs: Explain setup",
        [_file("changelog.d/1076.fix.md", "modified", _MODIFIED_GITLINK)],
    )

    assert result.returncode == 1
    assert _errors(result) == [_not_a_text_file("changelog.d/1076.fix.md")]


_DIRECTORY_LINK = f"@@ -0,0 +1 @@\n+notes\n{_NO_NEWLINE}"
_REMOVED_README = _file("changelog.d/README.md", "removed", "@@ -1 +0,0 @@\n-Old.")


def test_fragment_directory_replaced_by_a_link_fails(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "docs: Explain setup",
        [_file("changelog.d", patch=_DIRECTORY_LINK), _REMOVED_README],
    )

    assert result.returncode == 1
    assert _errors(result) == [
        "The fragment directory changelog.d must stay a directory."
    ]


def test_removed_fragment_directory_entry_passes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "docs: Explain setup",
        [_file("changelog.d", "removed", "@@ -1 +0,0 @@\n-notes")],
    )

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


@pytest.mark.parametrize(
    "name",
    [
        "changelog.d/1234.feature.md",
        "changelog.d/1234.feat.1.md",
        "changelog.d/1234.feat",
        "changelog.d/notes.md",
        "changelog.d/sub/1234.feat.md",
        "changelog.d/+orphan.feat.md",
        # towncrier reads the number as an int, so this collides with 1234.
        "changelog.d/01234.feat.md",
        "changelog.d/0.fix.md",
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


@pytest.mark.parametrize("name", ["changelog.d/notes.md", "changelog.d/01076.fix.md"])
def test_malformed_fragment_fails_an_exempt_title(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, "docs: Explain setup", [_file(name)])

    assert result.returncode == 1
    [error] = _errors(result)
    assert error.startswith(f"{name} is not a fragment name.")


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
        pages=[
            [_CODE, _file("README.md", "modified")],
            [_file("changelog.d/1234.feat.md")],
        ],
    )

    assert result.returncode == 0, result.stdout


def _code_files(start: int, count: int) -> list[dict[str, Any]]:
    return [
        _file(f"src/module_{index}.py", "modified", "@@ -1 +1 @@\n-a\n+b")
        for index in range(start, start + count)
    ]


def _pages(files: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [files[index : index + 100] for index in range(0, len(files), 100)]


def _pr(changed_files: int, title: str = "docs: Explain setup") -> dict[str, Any]:
    return {"number": _NUMBER, "title": title, "changed_files": changed_files}


_TOO_MANY = (
    "This pull request changes more than 3000 files, which the files API "
    "cannot list in full. Split it into smaller pull requests."
)
_INCOMPLETE = (
    "The changed files returned for this pull request do not match its "
    "changed_files count. Rerun the check."
)


def test_more_files_than_the_api_lists_fails_an_exempt_title(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "", [], pr=_pr(3001), pages=_pages(_code_files(0, 3000)))

    assert result.returncode == 1
    assert _errors(result) == [_TOO_MANY]


def test_every_file_the_api_lists_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, "", [], pr=_pr(3000), pages=_pages(_code_files(0, 3000)))

    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_missing_middle_page_fails_an_exempt_title(tmp_path: Path) -> None:
    pages = _pages(_code_files(0, 300))
    del pages[1]

    result = _run(tmp_path, "", [], pr=_pr(300), pages=pages)

    assert result.returncode == 1
    assert _errors(result) == [_INCOMPLETE]


@pytest.mark.parametrize("changed_files", [2, 3])
def test_duplicate_file_fails_an_exempt_title(
    tmp_path: Path, changed_files: int
) -> None:
    # With 3 declared, the repeated entry stands in for a file never returned.
    first, second = _code_files(0, 2)
    pages = [[first, second], [second]]

    result = _run(tmp_path, "", [], pr=_pr(changed_files), pages=pages)

    assert result.returncode == 1
    assert _errors(result) == [_INCOMPLETE]


def test_unsafe_file_name_is_not_echoed(tmp_path: Path) -> None:
    name = "changelog.d/x\n::warning::forged.md"
    result = _run(tmp_path, "docs: Explain setup", [_file(name)])

    assert result.returncode == 1
    assert "::warning::" not in result.stdout + result.stderr
    assert _errors(result)[0].startswith("a file with an unsafe name")


@pytest.mark.parametrize(
    ("pr", "pages"),
    [
        ({"title": "docs: Explain setup", "changed_files": 0}, [[]]),
        ({**_pr(0), "number": "1234"}, [[]]),
        ({**_pr(0), "number": True}, [[]]),
        ({**_pr(0), "title": None}, [[]]),
        ({"number": _NUMBER, "title": "docs: Explain setup"}, [[]]),
        (_pr(-1), [[]]),
        (_pr(True), [[]]),
        ({**_pr(0), "changed_files": "0"}, [[]]),
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
