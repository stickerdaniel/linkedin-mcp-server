"""
Progress callbacks for MCP tools.

Provides callback implementations that log progress for LinkedIn scraping operations.
"""

import logging
from typing import Any

from linkedin_scraper.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class MCPProgressCallback(ProgressCallback):
    """Callback that logs progress for MCP tools."""

    async def on_start(self, scraper_type: str, url: str) -> None:
        """Log when scraping starts."""
        logger.info(f"Starting {scraper_type} scrape: {url}")

    async def on_progress(self, message: str, percent: int) -> None:
        """Log progress updates."""
        logger.debug(f"Progress ({percent}%): {message}")

    async def on_complete(self, scraper_type: str, result: Any) -> None:
        """Log when scraping completes."""
        logger.info(f"Completed {scraper_type} scrape")

    async def on_error(self, error: Exception) -> None:
        """Log errors during scraping."""
        logger.error(f"Scrape error: {error}")


class SilentCallback(ProgressCallback):
    """Callback that produces no output - useful for background operations."""

    pass
