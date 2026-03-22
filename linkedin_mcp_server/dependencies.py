"""Dependency injection factories for MCP tools."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    ensure_warmup_complete,
    get_or_create_browser,
    hard_reset_browser,
    record_scrape,
    should_rotate,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_extractor() -> AsyncGenerator[LinkedInExtractor, None]:
    """Acquire the singleton browser, authenticate, and return a ready extractor.

    Proactively rotates the browser context when the scrape counter reaches
    the rotation threshold (LINKEDIN_CONTEXT_ROTATION_THRESHOLD, default 3).
    Records each scrape invocation in a finally block so the counter advances
    even on failure.

    Known LinkedIn exceptions are converted to structured ToolError responses
    via raise_tool_error(); unexpected exceptions propagate as-is.
    """
    try:
        await ensure_warmup_complete()
        if should_rotate():
            logger.info("Context rotation threshold reached — resetting browser")
            await hard_reset_browser()
        browser = await get_or_create_browser()
        await ensure_authenticated()
        extractor = LinkedInExtractor(browser.page)
        try:
            yield extractor
        finally:
            record_scrape()
    except Exception as e:
        raise_tool_error(e, "get_extractor")  # NoReturn
