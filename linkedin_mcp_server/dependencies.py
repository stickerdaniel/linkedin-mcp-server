"""Helpers used by MCP tools after bootstrap gating."""

from fastmcp import Context

from linkedin_mcp_server.bootstrap import ensure_tool_ready_or_raise
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import LinuxBrowserDependencyError
from linkedin_mcp_server.scraping import LinkedInExtractor


def _is_linux_browser_dependency_error(error: Exception) -> bool:
    message = str(error).lower()
    markers = (
        "host system is missing dependencies",
        "install-deps",
        "shared libraries",
        "libnss3",
        "libatk",
    )
    return any(marker in message for marker in markers)


async def get_ready_extractor(
    ctx: Context | None,
    *,
    tool_name: str,
) -> LinkedInExtractor:
    """Run bootstrap gating, then acquire an authenticated extractor."""
    try:
        await ensure_tool_ready_or_raise(tool_name, ctx)
        browser = await get_or_create_browser()
        await ensure_authenticated()
        return LinkedInExtractor(browser.page)
    except Exception as e:
        if isinstance(e, NetworkError) and _is_linux_browser_dependency_error(e):
            raise_tool_error(
                LinuxBrowserDependencyError(
                    "Chromium could not start because required system libraries are missing on this Linux host. Install the needed browser dependencies or use the Docker setup instead."
                ),
                tool_name,
            )
        raise_tool_error(e, tool_name)  # NoReturn
