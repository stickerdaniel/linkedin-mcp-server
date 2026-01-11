"""
Progress callbacks for MCP tools.

Provides callback implementations that log progress for LinkedIn scraping operations
and report progress to MCP clients via FastMCP Context.
"""

from typing import Any

from fastmcp import Context
from linkedin_scraper.callbacks import ProgressCallback


class MCPContextProgressCallback(ProgressCallback):
    """Callback that reports progress to MCP clients via FastMCP Context."""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def on_start(self, scraper_type: str, url: str) -> None:
        """Report start to MCP client."""
        await self.ctx.report_progress(
            progress=0, total=100, message=f"Starting {scraper_type}"
        )

    async def on_progress(self, message: str, percent: int) -> None:
        """Report progress to MCP client."""
        await self.ctx.report_progress(progress=percent, total=100, message=message)

    async def on_complete(self, scraper_type: str, result: Any) -> None:
        """Report completion to MCP client."""
        await self.ctx.report_progress(progress=100, total=100, message="Complete")
