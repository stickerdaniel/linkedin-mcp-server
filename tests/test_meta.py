"""Tests for linkedin_mcp_server.tools.meta health, ping, and alias registration."""

import logging

from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from linkedin_mcp_server.bootstrap import SetupState, get_bootstrap_state
from linkedin_mcp_server.tools.meta import (
    LEGACY_TOOL_NAMES,
    META_TOOL_NAMES,
    _session_auth_ready,
    build_health_payload,
    build_ping_payload,
    register_meta_tools,
    register_tool_aliases,
)


def _write_session_files(profile_dir):
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "marker").write_text("profile")
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        source_state_path,
    )

    portable_cookie_path(profile_dir).write_text("[]")
    source_state_path(profile_dir).write_text('{"version": 1}')


class TestBuildHealthPayload:
    def test_includes_browser_warning_when_not_ready(self, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.meta.browser_setup_ready", lambda: False
        )

        payload = build_health_payload()

        assert "warnings" in payload
        assert any("Patchright Chromium" in w for w in payload["warnings"])
        assert payload["bootstrap"]["browser_ready"] is False

    def test_includes_auth_warning_when_session_missing(self, isolate_profile_dir):
        payload = build_health_payload()

        assert "warnings" in payload
        assert any("--login" in w for w in payload["warnings"])
        assert payload["bootstrap"]["auth_ready"] is False
        assert payload["ok"] is False

    def test_includes_bootstrap_last_error_warning(self, monkeypatch):
        state = get_bootstrap_state()
        state.last_error = "install failed"

        payload = build_health_payload()

        assert "warnings" in payload
        assert any("install failed" in w for w in payload["warnings"])

    def test_ok_when_auth_ready_and_browser_ready(self, profile_dir, monkeypatch):
        _write_session_files(profile_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.meta.get_authentication_source",
            lambda: True,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.meta.browser_setup_ready", lambda: True
        )
        state = get_bootstrap_state()
        state.setup_state = SetupState.READY

        payload = build_health_payload()

        assert payload["bootstrap"]["auth_ready"] is True
        assert payload["bootstrap"]["browser_ready"] is True
        assert payload["ok"] is True
        assert "warnings" not in payload

    def test_session_auth_ready_false_when_metadata_lookup_raises(
        self, profile_dir, monkeypatch
    ):
        _write_session_files(profile_dir)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.meta.get_authentication_source",
            lambda: (_ for _ in ()).throw(RuntimeError("bad metadata")),
        )

        assert _session_auth_ready(profile_dir) is False


class TestBuildPingPayload:
    async def test_lists_legacy_and_prefixed_tools(self):
        mcp = FastMCP("test")
        register_meta_tools(mcp)

        @mcp.tool
        async def get_person_profile() -> dict[str, bool]:
            return {"ok": True}

        register_tool_aliases(mcp)

        payload = await build_ping_payload(mcp)

        assert payload["ok"] is True
        assert payload["pong"] is True
        tool_names = {tool["name"] for tool in payload["tools"]}
        assert "get_person_profile" in tool_names
        assert "linkedin_get_person_profile" in tool_names
        assert set(payload["capabilities"]["legacy_tool_names"]) == set(
            LEGACY_TOOL_NAMES
        )
        assert payload["capabilities"]["meta_tools"] == list(META_TOOL_NAMES)


class TestRegisterToolAliases:
    def test_skips_when_legacy_tool_missing(self):
        mcp = FastMCP("test")

        @mcp.tool
        async def get_person_profile() -> dict[str, bool]:
            return {"ok": True}

        register_tool_aliases(mcp)
        tools = {component.name for component in mcp.local_provider._components.values()}

        assert "get_person_profile" in tools
        assert "linkedin_get_person_profile" in tools
        assert "linkedin_get_inbox" not in tools

    def test_skips_when_alias_already_registered(self):
        mcp = FastMCP("test")

        @mcp.tool
        async def get_inbox() -> dict[str, bool]:
            return {"ok": True}

        inbox_tools = [
            component
            for component in mcp.local_provider._components.values()
            if isinstance(component, Tool) and component.name == "get_inbox"
        ]
        assert len(inbox_tools) == 1
        mcp.add_tool(Tool.from_tool(inbox_tools[0], name="linkedin_get_inbox"))

        register_tool_aliases(mcp)
        alias_tools = [
            component
            for component in mcp.local_provider._components.values()
            if isinstance(component, Tool) and component.name == "linkedin_get_inbox"
        ]

        assert len(alias_tools) == 1

    def test_logs_and_continues_when_alias_registration_fails(self, caplog):
        mcp = FastMCP("test")

        @mcp.tool
        async def get_person_profile() -> dict[str, bool]:
            return {"ok": True}

        original_add_tool = mcp.add_tool

        def flaky_add_tool(tool):
            if tool.name == "linkedin_get_person_profile":
                raise RuntimeError("alias registration failed")
            return original_add_tool(tool)

        mcp.add_tool = flaky_add_tool  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR):
            register_tool_aliases(mcp)

        assert any(
            "Failed to register linkedin_* alias for get_person_profile" in record.message
            for record in caplog.records
        )
        tools = {
            component.name
            for component in mcp.local_provider._components.values()
            if isinstance(component, Tool)
        }
        assert "get_person_profile" in tools
        assert "linkedin_get_person_profile" not in tools

    async def test_linkedin_health_tool_returns_payload(self):
        from linkedin_mcp_server.server import create_mcp_server

        mcp = create_mcp_server()
        result = await mcp.call_tool("linkedin_health", {})

        payload = result.structured_content
        assert payload is not None
        assert payload["server"] == "linkedin-mcp"
        assert "storage" in payload
        assert "bootstrap" in payload
