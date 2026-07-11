"""MCP meta tools: health, ping, and linkedin_* aliases."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from linkedin_mcp_server import __version__
from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.bootstrap import (
    SetupState,
    browser_setup_ready,
    browsers_path,
    get_bootstrap_state,
    get_runtime_policy,
    initialize_bootstrap,
)
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.drivers.browser import get_profile_dir, profile_exists
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    portable_cookie_path,
    runtime_profiles_root,
    source_state_path,
)

logger = logging.getLogger(__name__)

# Legacy scraper tools registered without the linkedin_ prefix.
LEGACY_TOOL_NAMES: tuple[str, ...] = (
    "get_person_profile",
    "get_my_profile",
    "connect_with_person",
    "get_sidebar_profiles",
    "search_people",
    "get_company_profile",
    "get_company_posts",
    "search_companies",
    "get_company_employees",
    "get_job_details",
    "search_jobs",
    "get_inbox",
    "get_conversation",
    "search_conversations",
    "send_message",
    "get_feed",
    "close_session",
)

META_PING_TIMEOUT_SECONDS = 10.0
META_HEALTH_TIMEOUT_SECONDS = 30.0
META_TOOL_NAMES: tuple[str, ...] = ("linkedin_health", "linkedin_ping")


def _session_auth_ready(profile_dir) -> bool:
    if not (
        profile_exists(profile_dir)
        and portable_cookie_path(profile_dir).exists()
        and source_state_path(profile_dir).exists()
    ):
        return False
    try:
        get_authentication_source()
    except Exception:
        return False
    return True


def _build_storage_paths(profile_dir) -> dict[str, str]:
    auth_root = profile_dir.expanduser().resolve().parent
    return {
        "auth_root": str(auth_root),
        "profile_dir": str(profile_dir.expanduser().resolve()),
        "cookies_json": str(portable_cookie_path(profile_dir)),
        "source_state_json": str(source_state_path(profile_dir)),
        "runtime_profiles_dir": str(runtime_profiles_root(profile_dir)),
        "patchright_browsers_dir": str(browsers_path()),
        "playwright_browsers_path": str(browsers_path()),
    }


def build_health_payload() -> dict[str, Any]:
    """Return server health without launching browser tools."""
    initialize_bootstrap()
    config = get_config()
    profile_dir = get_profile_dir()
    bootstrap = get_bootstrap_state()
    auth_ready = _session_auth_ready(profile_dir)
    browser_ready = browser_setup_ready()

    warnings: list[str] = []
    if not browser_ready:
        warnings.append(
            "Patchright Chromium is not installed or browser metadata is stale. "
            "First scraper tool call triggers background install."
        )
    if not auth_ready:
        warnings.append(
            "No valid LinkedIn session. Run `mcp-server-linkedin --login` on the host."
        )
    if bootstrap.last_error:
        warnings.append(f"Bootstrap last error: {bootstrap.last_error}")

    payload: dict[str, Any] = {
        "ok": auth_ready and (browser_ready or bootstrap.setup_state is SetupState.READY),
        "server": "linkedin-mcp",
        "version": __version__,
        "transport": config.server.transport,
        "mask_error_details": True,
        "runtime_policy": get_runtime_policy().value,
        "runtime_id": get_runtime_id(),
        "bootstrap": {
            "setup_state": bootstrap.setup_state.value,
            "auth_state": bootstrap.auth_state.value,
            "browser_ready": browser_ready,
            "auth_ready": auth_ready,
        },
        "session": {
            "profile_exists": profile_exists(profile_dir),
            "cookies_present": portable_cookie_path(profile_dir).exists(),
            "source_state_present": source_state_path(profile_dir).exists(),
            "auth_ready": auth_ready,
            "lifecycle": (
                "Host login via --login writes ~/.linkedin-mcp/profile plus "
                "cookies.json and source-state.json. Managed runtimes reuse the "
                "source profile; Docker/container runtimes derive a fresh Linux "
                "profile from exported cookies on startup. close_session (or "
                "linkedin_close_session) closes the in-process browser; persistent "
                "auth remains on disk until --logout."
            ),
        },
        "storage": _build_storage_paths(profile_dir),
        "timeouts_seconds": {
            "tool_default": config.server.tool_timeout_seconds,
            "browser_page_default_ms": config.browser.default_timeout,
        },
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


async def build_ping_payload(mcp: FastMCP) -> dict[str, Any]:
    """Return capability discovery payload for linkedin_ping."""
    config = get_config()
    tools = await mcp.list_tools(run_middleware=False)
    tool_list = [
        {"name": tool.name, "description": tool.description or ""}
        for tool in sorted(tools, key=lambda t: t.name)
    ]
    legacy_names = set(LEGACY_TOOL_NAMES)
    prefixed_aliases = [
        t["name"] for t in tool_list if t["name"].startswith("linkedin_")
    ]

    return {
        "ok": True,
        "pong": True,
        "server": "linkedin-mcp",
        "version": __version__,
        "transport": config.server.transport,
        "tools": tool_list,
        "tool_count": len(tool_list),
        "capabilities": {
            "playwright_profile_persistence": True,
            "patchright_chromium": True,
            "stdio_default": config.server.transport == "stdio",
            "sequential_tool_execution": True,
            "legacy_tool_names": sorted(legacy_names),
            "prefixed_aliases": prefixed_aliases,
            "meta_tools": ["linkedin_health", "linkedin_ping"],
            "default_tool_timeout_seconds": config.server.tool_timeout_seconds,
        },
        "storage": _build_storage_paths(get_profile_dir()),
    }


def register_tool_aliases(mcp: FastMCP) -> None:
    """Register linkedin_* aliases alongside legacy tool names."""
    # LocalProvider.list_tools/get_tool are async; during sync server setup we
    # read the already-registered components directly from the local provider.
    components = mcp.local_provider._components
    tools_by_name = {
        component.name: component
        for component in components.values()
        if isinstance(component, Tool)
    }
    existing = set(tools_by_name)

    for name in LEGACY_TOOL_NAMES:
        alias = f"linkedin_{name}"
        if name not in existing or alias in existing:
            continue
        try:
            mcp.add_tool(Tool.from_tool(tools_by_name[name], name=alias))
            existing.add(alias)
        except Exception:
            logger.exception("Failed to register linkedin_* alias for %s", name)


def register_meta_tools(
    mcp: FastMCP,
    *,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> None:
    """Register linkedin_health and linkedin_ping meta tools."""
    del tool_timeout  # meta tools use fixed shorter timeouts

    @mcp.tool(
        name="linkedin_health",
        timeout=META_HEALTH_TIMEOUT_SECONDS,
        title="LinkedIn MCP Health",
        annotations={"readOnlyHint": True},
        tags={"meta", "health"},
    )
    async def linkedin_health() -> dict[str, Any]:
        """Return server version, storage paths, and browser/session readiness."""
        return build_health_payload()

    @mcp.tool(
        name="linkedin_ping",
        timeout=META_PING_TIMEOUT_SECONDS,
        title="LinkedIn MCP Ping",
        annotations={"readOnlyHint": True},
        tags={"meta", "ping"},
    )
    async def linkedin_ping() -> dict[str, Any]:
        """Return server metadata, registered tools, and capabilities."""
        return await build_ping_payload(mcp)
