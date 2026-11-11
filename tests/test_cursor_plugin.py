"""Contract tests for the Cursor plugin package."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import unquote

from test_codex_plugin import _assert_mcp_contract, _load_json, _project_version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "linkedin-mcp-server"
_CURSOR_MANIFEST = _PLUGIN_ROOT / ".cursor-plugin" / "plugin.json"
_CURSOR_MARKETPLACE = _REPO_ROOT / ".cursor-plugin" / "marketplace.json"
_PLUGIN_MCP = _PLUGIN_ROOT / ".mcp.json"
_README = _REPO_ROOT / "README.md"
_DEEPLINK_PREFIX = "https://cursor.com/en/install-mcp?name=linkedin&config="


def test_cursor_manifest_matches_the_repository_release() -> None:
    manifest = _load_json(_CURSOR_MANIFEST)
    assert manifest["name"] == _PLUGIN_ROOT.name
    assert manifest["version"] == _project_version()
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


def test_cursor_shares_the_plugin_mcp_config() -> None:
    _assert_mcp_contract(_load_json(_PLUGIN_MCP))


def test_readme_cursor_deeplink_matches_the_plugin_mcp_config() -> None:
    readme = _README.read_text(encoding="utf-8")
    match = re.search(
        rf"{re.escape(_DEEPLINK_PREFIX)}([A-Za-z0-9%]+)",
        readme,
    )
    assert match is not None, "README is missing the Cursor MCP install deeplink"
    decoded = json.loads(base64.b64decode(unquote(match.group(1))))
    assert decoded == _load_json(_PLUGIN_MCP)["mcpServers"]["linkedin"]
