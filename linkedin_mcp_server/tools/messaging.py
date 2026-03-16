"""
LinkedIn messaging/inbox scraping tools.

Uses innerText extraction for resilient inbox and conversation data capture.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)


def register_messaging_tools(mcp: FastMCP) -> None:
    """Register all messaging-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Conversations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging"},
    )
    async def get_conversations(
        ctx: Context,
        limit: int = 20,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        List conversations from your LinkedIn messaging inbox.

        Navigates to the messaging inbox, scrolls to load conversations,
        and extracts the conversation list with participant names, last
        message previews, timestamps, and thread identifiers.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations to load (default 20).
                   Controls how far the inbox is scrolled.

        Returns:
            Dict with url, sections (inbox -> raw text), and optional references.
            The LLM should parse the raw text to extract individual conversations,
            participant names, message previews, and thread IDs from the URLs in
            the references.
        """
        try:
            logger.info("Scraping messaging inbox (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Starting inbox scrape"
            )

            result = await extractor.scrape_conversations(limit=limit)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "get_conversations")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Conversation Messages",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging"},
    )
    async def get_conversation_messages(
        thread_id: str,
        ctx: Context,
        limit: int = 50,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get messages from a specific LinkedIn conversation thread.

        Navigates to the conversation, scrolls up to load older messages,
        and extracts all visible messages with sender names, timestamps,
        and message text.

        Args:
            thread_id: LinkedIn messaging thread identifier. This can be a
                       numeric thread ID or a profile-based identifier
                       (e.g., "2-YWIxMjM0NTY3ODkw" or similar).
                       Found in conversation URLs or returned by get_conversations.
            ctx: FastMCP context for progress reporting
            limit: Maximum number of scroll iterations to load older messages
                   (default 50). Higher values load more history.

        Returns:
            Dict with url, sections (messages -> raw text), and optional references.
            The LLM should parse the raw text to extract individual messages,
            sender names, timestamps, and message content.
        """
        try:
            logger.info(
                "Scraping conversation thread: %s (limit=%d)",
                thread_id,
                limit,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting conversation scrape"
            )

            result = await extractor.scrape_conversation_messages(
                thread_id, limit=limit
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "get_conversation_messages")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Search Conversations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "search"},
    )
    async def search_conversations(
        query: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Search LinkedIn messaging conversations by keyword.

        Uses LinkedIn's messaging search to find conversations matching
        the query. Returns matching conversation previews.

        Args:
            query: Search keywords to find in conversations
                   (e.g., a person's name, topic, or keyword)
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (search_results -> raw text), and optional references.
            The LLM should parse the raw text to extract matching conversations.
        """
        try:
            logger.info("Searching conversations: query='%s'", query)

            await ctx.report_progress(
                progress=0, total=100, message="Starting conversation search"
            )

            result = await extractor.scrape_conversation_search(query)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "search_conversations")  # NoReturn
