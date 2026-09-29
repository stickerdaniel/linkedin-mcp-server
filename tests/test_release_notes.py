"""Contracts for release notes and publication controls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "compose_release_notes.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_TEMPLATE = _REPO_ROOT / ".github" / "RELEASE_NOTES_TEMPLATE.md"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_REPOSITORY = "stickerdaniel/linkedin-mcp-server"
_SHOULD_RELEASE = "steps.check.outputs.should-release == 'true'"
_PULL = f"https://github.com/{_REPOSITORY}/pull"
_COMPARE = f"**Full Changelog**: https://github.com/{_REPOSITORY}/compare"


def _link(number: int) -> str:
    return f"[#{number}]({_PULL}/{number})"


_CHANGELOG = f"""\
# Changelog

Entries start with the release that adopted towncrier.

<!-- towncrier release notes start -->

## 4.27.0 (2026-10-01)

### Features

- Newer feature. ({_link(3)})


## 4.26.0 (2026-09-24)

### Breaking Changes

- Removed a setting. ({_link(2)})

### Bug Fixes

- Fixed a crash. ({_link(1)})


## 4.25.1 (2026-09-01)

No significant changes.
"""

_OWNER_AUTHORS = {"1": "stickerdaniel", "2": "stickerdaniel", "3": "stickerdaniel"}

_INSTALL = "## Install or update\n\nGet v${VERSION} from ${VERSION}.\n"


def _compose(
    tmp_path: Path,
    version: str,
    *,
    changelog: str = _CHANGELOG,
    template: str = _INSTALL,
    fragments: tuple[str, ...] = ("README.md",),
    previous: str = "4.25.0",
    authors: dict[str, str] = _OWNER_AUTHORS,
    first: Any = (),
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "TEMPLATE.md").write_text(template, encoding="utf-8")
    (tmp_path / "pr-authors.json").write_text(json.dumps(authors), encoding="utf-8")
    (tmp_path / "first-contributors.json").write_text(
        json.dumps(first), encoding="utf-8"
    )
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
            "--pr-authors",
            str(tmp_path / "pr-authors.json"),
            "--first-contributors",
            str(tmp_path / "first-contributors.json"),
            "--pyproject",
            str(_PYPROJECT),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output


def _list_pull_requests(
    tmp_path: Path, version: str, changelog: str
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--list-pull-requests",
            "--changelog",
            str(tmp_path / "CHANGELOG.md"),
            "--version",
            version,
            "--repository",
            _REPOSITORY,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_section_between_two_others_composes_the_exact_body(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.26.0")

    assert result.returncode == 0, result.stdout
    assert output.read_text(encoding="utf-8") == (
        "## Breaking Changes\n"
        "\n"
        f"- Removed a setting. ({_link(2)})\n"
        "\n"
        "<details>\n"
        "<summary><b>Bug Fixes (1)</b></summary>\n"
        "\n"
        f"- Fixed a crash. ({_link(1)})\n"
        "\n"
        "</details>\n"
        "\n"
        "## Install or update\n"
        "\n"
        "Get v4.26.0 from 4.26.0.\n"
        "\n"
        "**Contributors:** @stickerdaniel\n"
        "\n"
        f"{_COMPARE}/v4.25.0...v4.26.0\n"
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
        f"{_COMPARE}/v4.25.0...v4.25.1\n"
    )


_HIGHLIGHTS_CHANGELOG = f"""\
# Changelog

<!-- towncrier release notes start -->

## 4.28.0 (2026-10-05)

### Highlights

- **Faster search.** Results arrive sooner. ({_link(12)})

### Features

- Added search. ({_link(12)})
- Added export. ({_link(13)})

### Bug Fixes

- Fixed a crash. ({_link(14)})
- Fixed a hang that
  spanned two lines. ({_link(15)})
- Raised a dependency floor. ({_link(16)})
- Kept the order, see [#17](https://example.test/pull/17). ({_link(13)})


## 4.27.0 (2026-10-01)

### Features

- Newer feature. ({_link(3)})
"""

_HIGHLIGHTS_AUTHORS = {
    "12": "ConnorMoss02",
    "13": "stickerdaniel",
    "14": "Ymx1ZQ",
    "15": "ConnorMoss02",
    "16": "renovate[bot]",
}


def test_highlights_features_and_folded_fixes_compose_the_exact_body(
    tmp_path: Path,
) -> None:
    result, output = _compose(
        tmp_path,
        "4.28.0",
        changelog=_HIGHLIGHTS_CHANGELOG,
        previous="4.27.0",
        authors=_HIGHLIGHTS_AUTHORS,
        # Matched without regard to case, like the owner.
        first=["ymx1zq"],
    )

    assert result.returncode == 0, result.stdout
    assert output.read_text(encoding="utf-8") == (
        "## Highlights\n"
        "\n"
        f"- **Faster search.** Results arrive sooner. ({_link(12)} by @ConnorMoss02)\n"
        "\n"
        "## Features\n"
        "\n"
        f"- Added search. ({_link(12)} by @ConnorMoss02)\n"
        f"- Added export. ({_link(13)})\n"
        "\n"
        "<details>\n"
        "<summary><b>Bug Fixes (4)</b></summary>\n"
        "\n"
        f"- Fixed a crash. ({_link(14)} by @Ymx1ZQ)\n"
        "- Fixed a hang that\n"
        f"  spanned two lines. ({_link(15)} by @ConnorMoss02)\n"
        f"- Raised a dependency floor. ({_link(16)})\n"
        f"- Kept the order, see [#17](https://example.test/pull/17). ({_link(13)})\n"
        "\n"
        "</details>\n"
        "\n"
        "## Install or update\n"
        "\n"
        "Get v4.28.0 from 4.28.0.\n"
        "\n"
        # The owner comes last, after everyone in order of first appearance.
        "**Contributors:** @ConnorMoss02, @Ymx1ZQ (first contribution 🎉), "
        "@stickerdaniel\n"
        "\n"
        f"{_COMPARE}/v4.27.0...v4.28.0\n"
    )


def test_owner_listed_as_a_first_contributor_is_an_error(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.26.0", first=["StickerDaniel"])

    assert result.returncode == 1
    assert not output.exists()
    assert result.stdout == (
        "::error::The repository owner is listed as a first contributor.\n"
    )


@pytest.mark.parametrize("login", ["octocat", "renovate[bot]"])
def test_first_contributor_outside_the_section_is_an_error(
    tmp_path: Path, login: str
) -> None:
    authors = {**_OWNER_AUTHORS, "1": "renovate[bot]"}

    result, output = _compose(tmp_path, "4.26.0", authors=authors, first=[login])

    assert result.returncode == 1
    assert not output.exists()
    assert result.stdout == (
        "::error::A first contributor is not among this release's contributors.\n"
    )


@pytest.mark.parametrize(
    ("first", "message"),
    [
        (["-dash"], "A first contributor is not a valid GitHub login."),
        (
            ["evil\n::warning::forged"],
            "A first contributor is not a valid GitHub login.",
        ),
        ([7], "A first contributor is not a valid GitHub login."),
        (
            {"stickerdaniel": True},
            "The first contributors are not a JSON array of logins.",
        ),
    ],
)
def test_malformed_first_contributors_are_an_error_and_not_echoed(
    tmp_path: Path, first: Any, message: str
) -> None:
    result, output = _compose(tmp_path, "4.26.0", first=first)

    assert result.returncode == 1
    assert not output.exists()
    assert result.stdout == f"::error::{message}\n"


def test_fixes_only_section_stays_open(tmp_path: Path) -> None:
    changelog = _CHANGELOG.replace(
        "## 4.25.1 (2026-09-01)\n\nNo significant changes.\n",
        f"## 4.25.1 (2026-09-01)\n\n### Bug Fixes\n\n- Fixed a hang. ({_link(4)})\n",
    )

    result, output = _compose(
        tmp_path, "4.25.1", changelog=changelog, authors={"4": "Ymx1ZQ"}
    )

    assert result.returncode == 0, result.stdout
    assert output.read_text(encoding="utf-8") == (
        "## Bug Fixes\n"
        "\n"
        f"- Fixed a hang. ({_link(4)} by @Ymx1ZQ)\n"
        "\n"
        "## Install or update\n"
        "\n"
        "Get v4.25.1 from 4.25.1.\n"
        "\n"
        "**Contributors:** @Ymx1ZQ\n"
        "\n"
        f"{_COMPARE}/v4.25.0...v4.25.1\n"
    )


def test_pull_request_without_an_author_is_an_error(tmp_path: Path) -> None:
    result, output = _compose(tmp_path, "4.26.0", authors={"3": "stickerdaniel"})

    assert result.returncode == 1
    assert not output.exists()
    assert (
        "::error::The pull request authors have no entry for #2, #1." in result.stdout
    )


@pytest.mark.parametrize(
    "login",
    ["two words", "-dash", "a" * 40, "renovate[bot]x", "evil\n::warning::forged"],
)
def test_invalid_login_is_an_error_and_not_echoed(tmp_path: Path, login: str) -> None:
    result, output = _compose(
        tmp_path, "4.26.0", authors={**_OWNER_AUTHORS, "1": login}
    )

    assert result.returncode == 1
    assert not output.exists()
    assert result.stdout == "::error::The author of #1 is not a valid GitHub login.\n"


def test_list_pull_requests_prints_each_linked_number_once(tmp_path: Path) -> None:
    result = _list_pull_requests(tmp_path, "4.28.0", _HIGHLIGHTS_CHANGELOG)

    assert result.returncode == 0, result.stdout
    assert result.stdout == "12\n13\n14\n15\n16\n"


def test_list_pull_requests_needs_the_section(tmp_path: Path) -> None:
    result = _list_pull_requests(tmp_path, "4.29.0", _HIGHLIGHTS_CHANGELOG)

    assert result.returncode == 1
    assert "::error::CHANGELOG.md has no section for 4.29.0." in result.stdout


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
    assert "use `mcp-server-linkedin@4.26.0` instead." in body
    assert (
        "\n\n```bash\ndocker pull stickerdaniel/linkedin-mcp-server:latest\n```\n\n"
        in body
    )
    # The template's own H3 headings are neither promoted nor folded.
    install = template.replace("${VERSION}", "4.26.0").strip()
    assert f"\n\n{install}\n\n" in body
    for heading in ("uvx", "Docker", "the MCP Bundle"):
        assert f"\n\n### Update with {heading}\n\n" in body
    assert body.count("<details>") == 1
    assert body.index("<summary><b>Bug Fixes (1)</b></summary>") < body.index(
        "## Install or update"
    )
    assert body.index("## Install or update") < body.index("**Contributors:** ")
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

    # The author lookup reads pull requests with the job's own token.
    assert check["permissions"] == {"contents": "read", "pull-requests": "read"}
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


_NEW_VERSION_OUTPUT = "${{ steps.check.outputs.new-version }}"
_TOKEN_EXPRESSION = "${{ github.token }}"

_RELEASED_CHANGELOG = f"""\
# Changelog

<!-- towncrier release notes start -->

## 4.26.0 (2026-09-24)

### Features

- Newer feature. ({_link(2)})
- Another feature. ({_link(4)})

### Bug Fixes

- Newer fix. ({_link(3)})
- Raised a floor. ({_link(5)})


## 4.25.0 (2026-09-01)

### Bug Fixes

- Older fix. ({_link(1)})
"""

# Only the new section's pull requests; a lookup of #1 would fail the step.
_RELEASED_LOGINS = {
    2: "stickerdaniel",
    3: "ConnorMoss02",
    4: "Ymx1ZQ",
    5: "renovate[bot]",
}

# Each author's merged pull requests: (total_count, numbers returned), plus
# an optional incomplete_results flag. The owner and the bot are absent, so
# searching for either fails the step.
_RELEASED_SEARCHES = {
    # #1 was merged in 4.25.0, so this is not a first contribution.
    "ConnorMoss02": (2, [3, 1]),
    "Ymx1ZQ": (1, [4]),
}

# The repository's own towncrier configuration, which names the fix category.
_TOWNCRIER = (
    "\n[tool.towncrier]"
    + _PYPROJECT.read_text(encoding="utf-8").split("\n[tool.towncrier]", 1)[1]
)


# Answers the two calls the step makes from canned API responses, running the
# step's own --jq expression through jq. gh prints a string result raw.
_FAKE_GH = """\
import json, os, subprocess, sys

with open(sys.argv[1], encoding="utf-8") as file:
    responses = json.load(file)
args = sys.argv[2:]
if not os.environ.get("GH_TOKEN"):
    sys.exit("gh: no GH_TOKEN")
key = jq = None
if len(args) == 4 and args[0] == "api" and args[2] == "--jq":
    key, jq = args[1], args[3]
elif (
    len(args) == 10
    and args[:5] == ["api", "-X", "GET", "search/issues", "-f"]
    and args[6:9] == ["-f", "per_page=100", "--jq"]
):
    key, jq = args[5], args[9]
if key not in responses:
    sys.exit(f"gh: HTTP 404: Not Found ({' '.join(args)})")
answer = subprocess.run(
    ["jq", "-r", jq],
    input=json.dumps(responses[key]),
    capture_output=True,
    text=True,
    check=True,
)
sys.stdout.write(answer.stdout)
"""


def _fake_gh(
    bin_dir: Path,
    logins: dict[int, str],
    searches: dict[str, tuple[Any, ...]],
) -> None:
    """Put a `gh` on PATH that knows these pull requests and searches."""
    assert shutil.which("jq"), "the fake gh evaluates the step's --jq with jq"
    responses: dict[str, Any] = {
        f"repos/{_REPOSITORY}/pulls/{number}": {
            "number": number,
            "user": {"login": login},
        }
        for number, login in logins.items()
    }
    for login, (total, numbers, *incomplete) in searches.items():
        responses[f"q=repo:{_REPOSITORY} is:pr is:merged author:{login}"] = {
            "total_count": total,
            "incomplete_results": bool(incomplete and incomplete[0]),
            "items": [{"number": number} for number in numbers],
        }
    (bin_dir / "responses.json").write_text(json.dumps(responses), encoding="utf-8")
    (bin_dir / "fake_gh.py").write_text(_FAKE_GH, encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "fake_gh.py"}" '
        f'"{bin_dir / "responses.json"}" "$@"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)


def _git(cwd: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
    )


def _run_compose_step(
    tmp_path: Path,
    *,
    tag_on_origin: bool,
    logins: dict[int, str] = _RELEASED_LOGINS,
    searches: dict[str, tuple[Any, ...]] = _RELEASED_SEARCHES,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the workflow's own compose step in a repo that just bumped 4.26.0."""
    step = _step(_workflow()["jobs"]["check-version-bump"], "Compose release notes")
    assert step["env"]["VERSION"] == _NEW_VERSION_OUTPUT
    assert step["env"]["GH_TOKEN"] == _TOKEN_EXPRESSION
    step_env = {
        key: value.replace(_NEW_VERSION_OUTPUT, "4.26.0").replace(
            _TOKEN_EXPRESSION, "test-token"
        )
        for key, value in step["env"].items()
    }
    assert not any("${{" in value for value in step_env.values()), step_env

    # The step calls plain python3; point it at this interpreter.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python3.chmod(0o755)
    _fake_gh(bin_dir, logins, searches)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@example.test",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.test",
    }

    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(origin), env=env)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", env=env)
    _git(repo, "remote", "add", "origin", str(origin), env=env)

    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "demo"\nversion = "4.25.0"\n{_TOWNCRIER}',
        encoding="utf-8",
    )
    _git(repo, "add", "pyproject.toml", env=env)
    _git(repo, "commit", "--quiet", "-m", "chore: Release 4.25.0", env=env)
    _git(repo, "tag", "v4.25.0", env=env)
    if tag_on_origin:
        _git(repo, "push", "--quiet", "origin", "refs/tags/v4.25.0", env=env)

    pyproject.write_text(
        f'[project]\nname = "demo"\nversion = "4.26.0"\n{_TOWNCRIER}',
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(_RELEASED_CHANGELOG, encoding="utf-8")
    (repo / ".github").mkdir()
    shutil.copy(_TEMPLATE, repo / ".github" / "RELEASE_NOTES_TEMPLATE.md")
    (repo / "scripts").mkdir()
    shutil.copy(_SCRIPT, repo / "scripts" / "compose_release_notes.py")
    (repo / "changelog.d").mkdir()
    (repo / "changelog.d" / "README.md").write_text("Fragments.\n", encoding="utf-8")
    _git(repo, "add", ".", env=env)
    _git(repo, "commit", "--quiet", "-m", "chore: Bump version to 4.26.0", env=env)

    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    result = subprocess.run(
        # What Actions runs for a step without an explicit shell.
        ["bash", "-e", str(script)],
        cwd=repo,
        env={
            **env,
            **step_env,
            "GITHUB_REPOSITORY": _REPOSITORY,
            "RUNNER_TEMP": str(runner_temp),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, repo / "RELEASE_NOTES.md"


@pytest.mark.parametrize(
    ("searches", "contributors"),
    [
        (
            _RELEASED_SEARCHES,
            "@Ymx1ZQ (first contribution 🎉), @ConnorMoss02, @stickerdaniel",
        ),
        # More merged pull requests than one page shows cannot all be here.
        (
            {**_RELEASED_SEARCHES, "Ymx1ZQ": (101, [4])},
            "@Ymx1ZQ, @ConnorMoss02, @stickerdaniel",
        ),
        # A search that timed out may have missed an older pull request.
        (
            {**_RELEASED_SEARCHES, "Ymx1ZQ": (1, [4], True)},
            "@Ymx1ZQ, @ConnorMoss02, @stickerdaniel",
        ),
        # A search that found nothing proves nothing.
        (
            {**_RELEASED_SEARCHES, "Ymx1ZQ": (0, [])},
            "@Ymx1ZQ, @ConnorMoss02, @stickerdaniel",
        ),
    ],
    ids=[
        "first-contribution",
        "more-than-one-page",
        "incomplete-search",
        "empty-search",
    ],
)
def test_compose_step_writes_the_new_versions_notes(
    tmp_path: Path, searches: dict[str, tuple[Any, ...]], contributors: str
) -> None:
    result, output = _run_compose_step(tmp_path, tag_on_origin=True, searches=searches)

    assert result.returncode == 0, result.stdout + result.stderr
    install = _TEMPLATE.read_text(encoding="utf-8").replace("${VERSION}", "4.26.0")
    assert "$" not in install
    assert output.read_text(encoding="utf-8") == (
        "## Features\n"
        "\n"
        f"- Newer feature. ({_link(2)})\n"
        f"- Another feature. ({_link(4)} by @Ymx1ZQ)\n"
        "\n"
        "<details>\n"
        "<summary><b>Bug Fixes (2)</b></summary>\n"
        "\n"
        f"- Newer fix. ({_link(3)} by @ConnorMoss02)\n"
        f"- Raised a floor. ({_link(5)})\n"
        "\n"
        "</details>\n"
        "\n"
        f"{install.strip()}\n"
        "\n"
        f"**Contributors:** {contributors}\n"
        "\n"
        f"{_COMPARE}/v4.25.0...v4.26.0\n"
    )


def test_compose_step_fails_when_a_search_fails(tmp_path: Path) -> None:
    result, output = _run_compose_step(
        tmp_path,
        tag_on_origin=True,
        searches={"ConnorMoss02": _RELEASED_SEARCHES["ConnorMoss02"]},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "gh: HTTP 404: Not Found (api -X GET search/issues -f "
        f"q=repo:{_REPOSITORY} is:pr is:merged author:Ymx1ZQ"
    ) in result.stderr
    assert not output.exists()


def test_compose_step_fails_when_an_author_lookup_fails(tmp_path: Path) -> None:
    logins = {
        number: login for number, login in _RELEASED_LOGINS.items() if number != 3
    }

    result, output = _run_compose_step(tmp_path, tag_on_origin=True, logins=logins)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"gh: HTTP 404: Not Found (api repos/{_REPOSITORY}/pulls/3" in (
        result.stderr
    )
    assert not output.exists()


def test_compose_step_needs_the_previous_tag_on_origin(tmp_path: Path) -> None:
    result, output = _run_compose_step(tmp_path, tag_on_origin=False)

    # `git ls-remote --exit-code` answers 2 when the ref is missing.
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Previous version: 4.25.0" in result.stdout
    assert not output.exists()


_PROTECTION_POLICY = {
    "required_status_checks": {
        "strict": False,
        "checks": [
            {"context": "lint-and-check", "app_id": 15368},
            {"context": "test", "app_id": 15368},
            {"context": "PR Title", "app_id": 15368},
        ],
    },
    "enforce_admins": True,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 0,
    },
    "restrictions": None,
}

_PROTECTION_FAKE_GH = """\
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
body = sys.stdin.read() if "--input" in args else ""
with open(os.environ["API_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"method": method, "body": body}) + "\\n")
if not os.environ.get("GH_TOKEN"):
    sys.exit("gh: no GH_TOKEN")
if args[:2] != ["api", "repos/" + os.environ["GITHUB_REPOSITORY"] + "/branches/main/protection"]:
    sys.exit("unexpected API endpoint")
state_path = Path(os.environ["API_STATE"])
policy = json.loads(state_path.read_text())
if os.environ.get("FAIL_BEFORE") == "1" and os.environ.get("FAIL_METHOD") == method:
    sys.exit("gh: HTTP 403: Forbidden")
if method == "GET":
    if policy is None or os.environ.get("FAIL_METHOD") == method:
        sys.exit("gh: HTTP 404: Not Found")
    result = subprocess.run(
        ["jq", "-r", args[args.index("--jq") + 1]],
        input=json.dumps(policy), capture_output=True, text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
elif method == "DELETE":
    state_path.write_text("null")
elif method == "PUT":
    state_path.write_text(json.dumps(json.loads(body)))
else:
    sys.exit("unexpected method")
# A failed response can follow a successful server-side mutation.
if os.environ.get("FAIL_METHOD") == method:
    sys.exit("gh: HTTP 500: response lost")
"""


def _run_protection_steps(
    tmp_path: Path,
    policy: dict[str, Any] | None,
    *,
    fail_method: str = "",
    fail_before: bool = False,
    reject_push: bool = False,
    unwritable_output: bool = False,
    restore_overrides: dict[str, str] | None = None,
) -> tuple[
    dict[str, subprocess.CompletedProcess[str]], list[dict[str, Any]], Any, Path
]:
    """Run the real protection/sync/tag scripts, in YAML order, against local fakes."""
    assert shutil.which("jq"), "the fake gh evaluates the workflow's real --jq"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_gh.py").write_text(_PROTECTION_FAKE_GH, encoding="utf-8")
    for name, command in {
        "gh": f'"{sys.executable}" "{bin_dir / "fake_gh.py"}"',
        "python3": f'"{sys.executable}"',
    }.items():
        shim = bin_dir / name
        shim.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    state = tmp_path / "policy.json"
    state.write_text(json.dumps(policy), encoding="utf-8")
    api_log = tmp_path / "api.jsonl"
    output = tmp_path / "github-output"
    if unwritable_output:
        output.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release-test@example.test",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release-test@example.test",
        "GITHUB_REPOSITORY": _REPOSITORY,
        "GITHUB_OUTPUT": str(output),
        "API_LOG": str(api_log),
        "API_STATE": str(state),
        "FAIL_METHOD": fail_method,
        "FAIL_BEFORE": "1" if fail_before else "0",
        "VERSION": "4.26.1",
        "LC_ALL": "C",
    }
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--quiet", str(origin), env=env)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", env=env)
    _git(repo, "remote", "add", "origin", str(origin), env=env)
    files = (
        "manifest.json",
        "docker-compose.yml",
        "server.json",
        "plugins/linkedin-mcp-server/.codex-plugin/plugin.json",
        "plugins/linkedin-mcp-server/.mcp.json",
    )
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".", env=env)
    _git(repo, "commit", "--quiet", "-m", "Initial release", env=env)
    _git(repo, "push", "--quiet", "origin", "main", env=env)
    if reject_push:
        (repo / "peer.txt").write_text("Peer commit\n", encoding="utf-8")
        _git(repo, "add", "peer.txt", env=env)
        _git(repo, "commit", "--quiet", "-m", "Peer update", env=env)
        _git(repo, "push", "--quiet", "origin", "main", env=env)
        _git(repo, "reset", "--hard", "HEAD~1", env=env)
    for name in files:
        (repo / name).write_text("after\n", encoding="utf-8")

    outputs: dict[str, dict[str, str]] = {}

    def resolve(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            expression = match[1].strip()
            if expression == "github.repository":
                return _REPOSITORY
            if expression.startswith("secrets."):
                return "test-token"
            parts = expression.split(".")
            if len(parts) == 4 and parts[0] == "steps" and parts[2] == "outputs":
                return outputs.get(parts[1], {}).get(parts[3], "")
            raise AssertionError(f"Unsupported expression: {expression}")

        return re.sub(r"\$\{\{(.*?)\}\}", replace, value)

    names = {
        "Read branch protection",
        "Remove branch protection (temporary)",
        "Commit version updates",
        "Restore branch protection",
        "Create release tag",
    }
    results: dict[str, subprocess.CompletedProcess[str]] = {}
    failed = False
    for step in _workflow()["jobs"]["prepare-release"]["steps"]:
        name = step.get("name")
        if name not in names:
            continue
        condition = step.get("if", "success()")
        if condition not in {"success()", "always()"}:
            raise AssertionError(f"Unsupported condition: {condition}")
        if failed and condition != "always()":
            continue
        step_env = {key: resolve(value) for key, value in step.get("env", {}).items()}
        if name == "Restore branch protection":
            step_env.update(restore_overrides or {})
        result = subprocess.run(
            ["bash", "-e"],
            input=resolve(step["run"]),
            cwd=repo,
            env={**env, **step_env},
            capture_output=True,
            text=True,
            timeout=15,
        )
        results[name] = result
        failed |= result.returncode != 0
        if result.returncode == 0 and "id" in step and output.is_file():
            outputs[step["id"]] = dict(
                line.split("=", 1) for line in output.read_text().splitlines()
            )
    calls = [json.loads(line) for line in api_log.read_text().splitlines()]
    return results, calls, json.loads(state.read_text()), repo


def _published_test_tag(repo: Path) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo.parent / "origin.git"),
            "show-ref",
            "--verify",
            "--quiet",
            "refs/tags/v4.26.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


@pytest.mark.parametrize("strict", [False, True])
def test_release_restores_the_existing_strict_policy(
    tmp_path: Path, strict: bool
) -> None:
    policy = json.loads(json.dumps(_PROTECTION_POLICY))
    policy["required_status_checks"]["strict"] = strict

    results, calls, restored, repo = _run_protection_steps(tmp_path, policy)

    assert [call["method"] for call in calls] == ["GET", "DELETE", "PUT"]
    assert all(result.returncode == 0 for result in results.values()), results
    assert restored == policy
    assert restored["required_status_checks"]["strict"] is strict
    assert json.loads(calls[-1]["body"]) == policy
    assert _published_test_tag(repo)


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {},
        *(
            {"required_status_checks": {"strict": value}}
            for value in (
                None,
                "false",
                "true",
                "yes",
                "",
                0,
                1,
                [],
                {},
            )
        ),
    ],
)
def test_release_leaves_unreadable_strict_policy_untouched(
    tmp_path: Path, policy: dict[str, Any] | None
) -> None:
    results, calls, restored, repo = _run_protection_steps(tmp_path, policy)

    assert any(result.returncode != 0 for result in results.values())
    assert [call["method"] for call in calls] == ["GET"]
    assert restored == policy
    assert results["Restore branch protection"].returncode == 0
    assert not _published_test_tag(repo)


def test_release_does_not_delete_protection_without_saving_capture(
    tmp_path: Path,
) -> None:
    results, calls, restored, repo = _run_protection_steps(
        tmp_path, _PROTECTION_POLICY, unwritable_output=True
    )

    assert any(result.returncode != 0 for result in results.values())
    assert [call["method"] for call in calls] == ["GET"]
    assert restored == _PROTECTION_POLICY
    assert results["Restore branch protection"].returncode == 0
    assert not _published_test_tag(repo)


@pytest.mark.parametrize("failure", ["GET", "DELETE", "PUT", "sync"])
def test_release_protection_failures_stop_tagging(tmp_path: Path, failure: str) -> None:
    results, calls, restored, repo = _run_protection_steps(
        tmp_path,
        _PROTECTION_POLICY,
        fail_method=failure,
        reject_push=failure == "sync",
    )

    assert any(result.returncode != 0 for result in results.values())
    assert restored == _PROTECTION_POLICY
    assert not _published_test_tag(repo)
    assert [call["method"] for call in calls] == (
        ["GET"] if failure == "GET" else ["GET", "DELETE", "PUT"]
    )
    if failure == "sync":
        assert "[rejected]" in results["Commit version updates"].stderr
        assert results["Restore branch protection"].returncode == 0
    if failure == "PUT":
        assert results["Restore branch protection"].returncode != 0


def test_failed_restore_can_leave_protection_missing(tmp_path: Path) -> None:
    results, calls, restored, repo = _run_protection_steps(
        tmp_path, _PROTECTION_POLICY, fail_method="PUT", fail_before=True
    )

    assert results["Restore branch protection"].returncode != 0
    assert "HTTP 403" in results["Restore branch protection"].stderr
    assert [call["method"] for call in calls] == ["GET", "DELETE", "PUT"]
    assert restored is None
    assert not _published_test_tag(repo)


@pytest.mark.parametrize("override", [{"STRICT": "maybe"}, {"PAYLOAD": "not json"}])
def test_release_rejects_invalid_restore_input_before_put(
    tmp_path: Path, override: dict[str, str]
) -> None:
    results, calls, _, repo = _run_protection_steps(
        tmp_path, _PROTECTION_POLICY, restore_overrides=override
    )

    assert results["Restore branch protection"].returncode != 0
    assert [call["method"] for call in calls] == ["GET", "DELETE"]
    assert not _published_test_tag(repo)


# The prepare-release job holds the admin token. Intentional strict-policy
# preservation updates its reviewed digest alongside behavioral coverage.
_PREPARE_RELEASE_SHA256 = (
    "89d91ebde01bbd0780d462c6f3bee01303d197f5af0264fc00298288c2d1e5f8"
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
