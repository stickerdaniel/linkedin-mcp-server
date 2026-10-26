"""Contract tests for the opt-in Codex and Claude Code plugin package."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any, Callable

import pytest
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "linkedin-mcp-server"
_PLUGIN_MANIFEST = _PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
_PLUGIN_MCP = _PLUGIN_ROOT / ".mcp.json"
_MARKETPLACE = _REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
_CLAUDE_MANIFEST = _PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
_CLAUDE_MARKETPLACE = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _project_version() -> str:
    project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


def _released_version() -> str:
    return _load_json(_REPO_ROOT / "manifest.json")["version"]


# Every host runs the newest release, so no release rewrites this file. The
# explicit stdio transport keeps a TRANSPORT variable in the user's
# environment from switching a stdio client to HTTP.
def _assert_mcp_contract(mcp: dict[str, Any]) -> None:
    assert set(mcp) == {"mcpServers"}
    assert set(mcp["mcpServers"]) == {"linkedin"}
    server = mcp["mcpServers"]["linkedin"]
    assert server["command"] == "uvx"
    assert server["args"] == ["mcp-server-linkedin@latest", "--transport", "stdio"]
    assert server["env"] == {"UV_HTTP_TIMEOUT": "300"}
    assert "enabled" not in json.dumps(mcp)


def test_marketplace_is_opt_in_and_resolves_the_plugin() -> None:
    marketplace = _load_json(_MARKETPLACE)
    assert marketplace["name"] == "linkedin-mcp-server"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry["name"] == "linkedin-mcp-server"
    assert entry["source"] == {
        "source": "local",
        "path": "./plugins/linkedin-mcp-server",
    }
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert "enabled" not in json.dumps(marketplace)


def test_manifest_matches_the_repository_release() -> None:
    manifest = _load_json(_PLUGIN_MANIFEST)
    assert manifest["name"] == _PLUGIN_ROOT.name
    assert manifest["version"] == _released_version()
    assert Version(manifest["version"]) <= Version(_project_version())
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert "enabled" not in json.dumps(manifest)


def test_mcp_command_runs_the_latest_release() -> None:
    _assert_mcp_contract(_load_json(_PLUGIN_MCP))


def test_release_workflow_updates_and_commits_every_plugin_file() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    for path in (
        "plugins/linkedin-mcp-server/.codex-plugin/plugin.json",
        "plugins/linkedin-mcp-server/.claude-plugin/plugin.json",
        "plugins/linkedin-mcp-server/.cursor-plugin/plugin.json",
    ):
        assert workflow.count(path) >= 3
    assert '"Codex plugin": [plugin["version"]]' in workflow
    assert '"Claude Code plugin": [claude_plugin["version"]]' in workflow
    assert '"Cursor plugin": [cursor_plugin["version"]]' in workflow


def test_claude_code_marketplace_resolves_the_plugin() -> None:
    marketplace = _load_json(_CLAUDE_MARKETPLACE)
    assert marketplace["name"] == "linkedin-mcp-server"
    assert [(p["name"], p["source"]) for p in marketplace["plugins"]] == [
        ("linkedin-mcp-server", "./plugins/linkedin-mcp-server")
    ]


def test_claude_code_manifest_matches_the_codex_manifest() -> None:
    # Both hosts read the same plugin directory, so only the version can drift.
    claude = _load_json(_CLAUDE_MANIFEST)
    codex = _load_json(_PLUGIN_MANIFEST)
    assert claude["name"] == codex["name"]
    assert claude["version"] == _released_version()


def test_plugin_icon_is_the_release_icon() -> None:
    assert (_PLUGIN_ROOT / "assets" / "icon.svg").read_bytes() == (
        _REPO_ROOT / "assets" / "icons" / "icon.svg"
    ).read_bytes()


def test_skill_keeps_unrelated_work_and_writes_out_of_scope() -> None:
    skill = (_PLUGIN_ROOT / "skills" / "linkedin-mcp" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "Use only for an explicit LinkedIn request" in skill
    assert "Do not call LinkedIn tools for unrelated" in skill
    assert "exact recipient and action" in skill
    assert "Never enable the plugin or its MCP server" in skill
    assert "host's plugin configuration" in skill


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc["mcpServers"]["linkedin"].__setitem__("enabled", True),
        lambda doc: doc["mcpServers"]["linkedin"]["args"].__setitem__(
            0, "mcp-server-linkedin@0.0.0"
        ),
        lambda doc: doc["mcpServers"]["linkedin"]["args"].__delitem__(slice(1, None)),
    ],
)
def test_malformed_or_forced_runtime_is_rejected(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    candidate = copy.deepcopy(_load_json(_PLUGIN_MCP))
    mutate(candidate)
    with pytest.raises(AssertionError):
        _assert_mcp_contract(candidate)
