"""Keep published FastMCP metadata compatible with the registered tools."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOWER_BOUND_OPERATORS = frozenset({">=", ">", "==", "===", "~="})


def _minimum(requirement: Requirement) -> Version:
    floors = []
    for specifier in requirement.specifier:
        if specifier.operator not in _LOWER_BOUND_OPERATORS:
            continue
        try:
            floors.append(Version(specifier.version))
        except InvalidVersion:
            continue
    assert floors, f"{requirement.name} has no interpretable lower bound: {requirement}"
    return max(floors)


@pytest.mark.parametrize("operator", [">=", ">", "~="])
def test_minimum_understands_lower_bound_operators(operator: str) -> None:
    assert _minimum(Requirement(f"example{operator}2.14.2")) == Version("2.14.2")


def test_minimum_rejects_an_uninterpretable_lower_bound() -> None:
    with pytest.raises(AssertionError, match="no interpretable lower bound"):
        _minimum(Requirement("example==2.14.*"))


def test_security_floors_are_published() -> None:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    runtime = {
        str(canonicalize_name(requirement.name)): requirement
        for requirement in map(Requirement, pyproject["project"]["dependencies"])
    }
    development = {
        str(canonicalize_name(requirement.name)): requirement
        for requirement in map(Requirement, pyproject["dependency-groups"]["dev"])
    }

    expected_runtime = {
        "cryptography": Version("50.0.1"),
        "fastmcp": Version("3.4.7"),
        "mcp": Version("1.28.1"),
        "pydantic-settings": Version("2.14.2"),
        "starlette": Version("1.3.1"),
    }
    for name, floor in expected_runtime.items():
        assert _minimum(runtime[name]) >= floor
    assert _minimum(development["aiohttp"]) >= Version("3.14.3")


def test_supported_local_architectures_are_published() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "ARM64 (Apple silicon) on macOS" in readme
    assert "64-bit Windows" in readme
    assert "no longer publish Intel macOS or 32-bit Windows wheels" in readme
    assert "Docker images remain supported on Linux AMD64 and ARM64" in readme


def test_fastmcp_v4_is_excluded_while_exclude_args_is_used() -> None:
    """FastMCP 4 removed the ``exclude_args`` decorator argument."""
    tool_sources = (_REPO_ROOT / "linkedin_mcp_server" / "tools").glob("*.py")
    legacy_sources = [
        path.name
        for path in tool_sources
        if "exclude_args=" in path.read_text(encoding="utf-8")
    ]
    assert legacy_sources, (
        "exclude_args has been migrated; remove this compatibility test and "
        "review the FastMCP upper bound"
    )

    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    fastmcp = next(
        Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
        if canonicalize_name(Requirement(raw).name) == "fastmcp"
    )

    assert Version("4.0.0") not in fastmcp.specifier
    assert Version("4.99.0") not in fastmcp.specifier
