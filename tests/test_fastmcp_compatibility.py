"""Keep published FastMCP metadata compatible with the code that uses it."""

from __future__ import annotations

import re
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


#: What the daemon boundary still takes from MCP SDK v1 and httpx. FastMCP 4
#: requires ``mcp>=2``, where the error is ``MCPError`` and ``ClientRequest`` is
#: a union that cannot be constructed, and its HTTP client is ``httpx2``, whose
#: exceptions an ``except httpx...`` does not catch.
_SDK_V1_BOUNDARY = {
    "direct httpx import": re.compile(
        r"^\s*(?:import httpx|from httpx import)\b", re.MULTILINE
    ),
    "McpError import": re.compile(r"\bfrom mcp\.shared\.exceptions import McpError\b"),
    "raw ClientRequest wrapper": re.compile(r"\bmt\.ClientRequest\("),
}


def test_fastmcp_v4_is_excluded_while_the_sdk_v1_boundary_remains() -> None:
    """Temporary guard for #858, retired by the port that removes every marker."""
    sources = sorted((_REPO_ROOT / "linkedin_mcp_server").rglob("*.py"))
    remaining = {
        f"{path.relative_to(_REPO_ROOT).as_posix()}: {marker}"
        for path in sources
        for marker, pattern in _SDK_V1_BOUNDARY.items()
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert remaining, (
        "the SDK v1 boundary has been ported (#858); remove this test and "
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

    # The cap itself, not sampled versions: ``==4.0.10`` or ``<4.1`` exclude
    # both 4.0.0 and 4.99.0 and still install FastMCP 4.
    assert "<4" in {str(spec) for spec in fastmcp.specifier}, sorted(remaining)
