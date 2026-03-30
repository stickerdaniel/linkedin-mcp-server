"""Helpers used by MCP tools after bootstrap gating."""

from fastmcp import Context

from linkedin_mcp_server.bootstrap import ensure_tool_ready_or_raise
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor


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
        raise_tool_error(e, tool_name)  # NoReturn
