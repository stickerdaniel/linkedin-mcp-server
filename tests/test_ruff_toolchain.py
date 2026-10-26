from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RUFF_PRE_COMMIT = "https://github.com/astral-sh/ruff-pre-commit"


def _ruff_requirement() -> Requirement:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    requirements = [Requirement(raw) for raw in pyproject["dependency-groups"]["dev"]]
    ruff = [
        requirement
        for requirement in requirements
        if canonicalize_name(requirement.name) == "ruff"
    ]
    assert len(ruff) == 1, f"pyproject.toml contains {len(ruff)} Ruff dependencies"
    return ruff[0]


def _locked_ruff_version() -> Version:
    lock = tomllib.loads((_REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == "ruff"]
    assert len(packages) == 1, f"uv.lock contains {len(packages)} Ruff packages"
    return Version(packages[0]["version"])


def _hook_ruff_version() -> Version:
    config = yaml.safe_load(
        (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    repositories = [
        repository
        for repository in config["repos"]
        if repository["repo"] == _RUFF_PRE_COMMIT
    ]
    assert len(repositories) == 1, (
        f".pre-commit-config.yaml contains {len(repositories)} Ruff repositories"
    )
    return Version(repositories[0]["rev"].removeprefix("v"))


def test_ruff_versions_match_across_the_toolchain() -> None:
    requirement = _ruff_requirement()
    specifiers = list(requirement.specifier)
    assert len(specifiers) == 1 and specifiers[0].operator == "==", (
        f"Ruff must have one exact development pin, found {requirement}"
    )

    declared = Version(specifiers[0].version)
    locked = _locked_ruff_version()
    hooked = _hook_ruff_version()

    assert declared == locked == hooked, (
        f"Ruff versions drifted: pyproject={declared}, lock={locked}, "
        f"pre-commit={hooked}"
    )


def test_ruff_format_keeps_its_previous_file_scope(tmp_path: Path) -> None:
    pyproject_path = _REPO_ROOT / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    assert pyproject["tool"]["ruff"]["format"]["exclude"] == ["*.md"]

    config = yaml.safe_load(
        (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    repository = next(
        repository
        for repository in config["repos"]
        if repository["repo"] == _RUFF_PRE_COMMIT
    )
    formatter = next(
        hook for hook in repository["hooks"] if hook["id"] == "ruff-format"
    )
    assert formatter["types_or"] == ["python", "pyi", "jupyter"]

    markdown = tmp_path / "scope.md"
    original = "```python\nvalue=  1\n```\n"
    markdown.write_text(original, encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(pyproject_path),
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert markdown.read_text(encoding="utf-8") == original
