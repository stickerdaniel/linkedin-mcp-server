"""Dependency injection factories for MCP tools."""

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor


async def get_extractor() -> LinkedInExtractor:
    """Authenticate, acquire the singleton browser, and return a ready extractor.

    Known LinkedIn exceptions are converted to structured ToolError responses
    via raise_tool_error(); unexpected exceptions propagate as-is.
    """
    try:
        browser = await get_or_create_browser()
        await ensure_authenticated()
        return LinkedInExtractor(browser.page)
    except Exception as e:
        raise_tool_error(e, "get_extractor")  # NoReturn
