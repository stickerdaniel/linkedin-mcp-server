"""Dependency injection factories for MCP tools."""

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.scraping import LinkedInExtractor


async def get_extractor() -> LinkedInExtractor:
    """Authenticate, acquire the singleton browser, and return a ready extractor."""
    await ensure_authenticated()
    browser = await get_or_create_browser()
    return LinkedInExtractor(browser.page)
