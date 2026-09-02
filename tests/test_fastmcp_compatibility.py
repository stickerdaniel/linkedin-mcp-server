"""Keep published FastMCP metadata compatible with the registered tools."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent


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
