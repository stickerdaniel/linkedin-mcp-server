"""Contract tests for the Cursor plugin package."""

from __future__ import annotations

import base64
import json
import re
import tomllib
from pathlib import Path
from packaging.version import Version

from test_codex_plugin import _assert_mcp_contract, _load_json, _released_version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "linkedin-mcp-server"
_CURSOR_MANIFEST = _PLUGIN_ROOT / ".cursor-plugin" / "plugin.json"
_CURSOR_MARKETPLACE = _REPO_ROOT / ".cursor-plugin" / "marketplace.json"
_PLUGIN_MCP = _PLUGIN_ROOT / ".mcp.json"
_README = _REPO_ROOT / "README.md"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_DEEPLINK_PREFIX = (
    "cursor://anysphere.cursor-deeplink/mcp/install?name=linkedin&config="
)


def _cursor_install_config_b64() -> str:
    mcp = _load_json(_PLUGIN_MCP)
    payload = {"linkedin": mcp["mcpServers"]["linkedin"]}
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def test_cursor_manifest_matches_the_repository_release() -> None:
    manifest = _load_json(_CURSOR_MANIFEST)
    assert manifest["name"] == _PLUGIN_ROOT.name
    assert manifest["version"] == _released_version()
    assert Version(manifest["version"]) <= Version(
        tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"]
    )
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["skills"] == "./skills/"
    assert manifest["logo"] == "assets/icon.svg"
    assert "variables" not in manifest
    assert "enabled" not in json.dumps(manifest)


def test_marketplace_points_at_the_plugin_directory() -> None:
    marketplace = _load_json(_CURSOR_MARKETPLACE)
    assert marketplace["name"] == "linkedin-mcp-server"
    assert marketplace["owner"]["name"] == "Daniel Sticker"
    assert len(marketplace["plugins"]) == 1
    entry = marketplace["plugins"][0]
    assert entry["name"] == "linkedin-mcp-server"
    assert entry["source"] == "plugins/linkedin-mcp-server"


def test_cursor_shares_the_version_pinned_mcp_config() -> None:
    _assert_mcp_contract(_load_json(_PLUGIN_MCP), _released_version())


def test_readme_cursor_deeplink_matches_pinned_mcp_config() -> None:
    readme = _README.read_text(encoding="utf-8")
    match = re.search(
        rf"{re.escape(_DEEPLINK_PREFIX)}([A-Za-z0-9+/=]+)",
        readme,
    )
    assert match is not None, "README is missing the Cursor MCP install deeplink"
    decoded = json.loads(base64.b64decode(match.group(1)))
    assert decoded == {"linkedin": _load_json(_PLUGIN_MCP)["mcpServers"]["linkedin"]}


def test_release_workflow_updates_and_commits_cursor_manifest() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    cursor_manifest = "plugins/linkedin-mcp-server/.cursor-plugin/plugin.json"
    assert workflow.count(cursor_manifest) >= 3
    assert '"Cursor plugin": [cursor_plugin["version"]]' in workflow
    assert "Update Codex and Cursor plugin versions" in workflow
