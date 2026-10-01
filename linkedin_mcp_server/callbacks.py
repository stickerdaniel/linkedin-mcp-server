"""
Progress callbacks for MCP tools.

Provides callback implementations that report progress for LinkedIn page reads
to MCP clients via FastMCP Context.
"""

from typing import Any

from fastmcp import Context


class ProgressCallback:
    """Base callback class for progress tracking."""

    async def on_start(self, subject: str, url: str) -> None:
        pass

    async def on_progress(self, message: str, percent: int) -> None:
        pass

    async def on_complete(self, subject: str, result: Any) -> None:
        pass

    async def on_error(self, error: Exception) -> None:
        pass


class MCPContextProgressCallback(ProgressCallback):
    """Callback that reports progress to MCP clients via FastMCP Context."""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def on_start(self, subject: str, url: str) -> None:
        """Report start to MCP client."""
        await self.ctx.report_progress(
            progress=0, total=100, message=f"Starting {subject}"
        )

    async def on_progress(self, message: str, percent: int) -> None:
        """Report progress to MCP client."""
        await self.ctx.report_progress(progress=percent, total=100, message=message)

    async def on_complete(self, subject: str, result: Any) -> None:
        """Report completion to MCP client."""
        await self.ctx.report_progress(progress=100, total=100, message="Complete")

    async def on_error(self, error: Exception) -> None:
        """Report error to MCP client."""
        await self.ctx.report_progress(progress=0, total=100, message=f"Error: {error}")
