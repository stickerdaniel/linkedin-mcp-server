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
        title="Send Message",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"messaging"},
    )
    async def send_message(
        linkedin_username: str,
        message: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Send a new LinkedIn message to a person.

        Opens a new conversation (or navigates to an existing one) with the
        specified person and sends a message.

        Args:
            linkedin_username: LinkedIn username of the recipient
                              (e.g., "stickerdaniel", "williamhgates")
            message: The message text to send
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with status, url, and confirmation details.
        """
        try:
            logger.info("Sending message to: %s", linkedin_username)

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to conversation"
            )

            result = await extractor.send_message(linkedin_username, message)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "send_message")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Reply to Conversation",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"messaging"},
    )
    async def reply_to_conversation(
        thread_id: str,
        message: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Reply to an existing LinkedIn conversation thread.

        Navigates to the specified thread and sends a reply message.

        Args:
            thread_id: LinkedIn messaging thread identifier.
                       Found in conversation URLs or returned by get_conversations.
            message: The reply message text to send
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with status, url, and confirmation details.
        """
        try:
            logger.info("Replying to thread: %s", thread_id)

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to thread"
            )

            result = await extractor.reply_to_conversation(thread_id, message)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "reply_to_conversation")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Send Connection Request",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"messaging", "networking"},
    )
    async def send_connection_request(
        linkedin_username: str,
        ctx: Context,
        message: str | None = None,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Send a LinkedIn connection request, optionally with a personalized note.

        Navigates to the person's profile, clicks "Connect", optionally adds
        a personal note, and sends the invitation.

        Args:
            linkedin_username: LinkedIn username of the person to connect with
                              (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting
            message: Optional personalized note to include with the connection
                     request (max 300 characters per LinkedIn's limit).
                     If omitted, sends without a note.

        Returns:
            Dict with status, url, and confirmation details.
            Status will be "sent" on success, "already_connected" if already
            connected, or "error" with details on failure.
        """
        try:
            logger.info(
                "Sending connection request to: %s (with_note=%s)",
                linkedin_username,
                message is not None,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to profile"
            )

            result = await extractor.send_connection_request(
                linkedin_username, message=message
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "send_connection_request")  # NoReturn

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
        and extracts a structured conversation list with participant names,
        timestamps, message previews, unread status, and thread IDs.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations to load (default 20).
                   Controls how far the inbox is scrolled.

        Returns:
            Dict with url, conversations (structured list), and sections
            (inbox -> raw text fallback).
            Each conversation has: thread_id, name, timestamp, preview,
            unread (bool), url.
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

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Check Connection Status",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"networking"},
    )
    async def check_connection_status(
        linkedin_username: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Check the connection status with a LinkedIn user.

        Navigates to the person's profile and determines whether you are
        connected, have a pending invitation, or are not connected.

        Args:
            linkedin_username: LinkedIn username to check
                              (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with username, url, status, name, headline.
            Status is one of: connected, pending_sent, pending_received,
            not_connected, unknown, error.
        """
        try:
            logger.info("Checking connection status: %s", linkedin_username)

            await ctx.report_progress(
                progress=0, total=100, message="Checking profile"
            )

            result = await extractor.check_connection_status(linkedin_username)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "check_connection_status")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Pending Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"networking"},
    )
    async def get_pending_invitations(
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        List sent connection invitations that are still pending.

        Navigates to the sent invitations page and extracts all pending
        connection requests with names, headlines, and profile URLs.

        Useful for tracking which connection requests have not yet been
        accepted and for cleaning up stale invitations.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and invitations list. Each invitation has:
            name, username, headline, sent_at, profile_url.
        """
        try:
            logger.info("Scraping pending invitations")

            await ctx.report_progress(
                progress=0, total=100, message="Loading sent invitations"
            )

            result = await extractor.scrape_pending_invitations()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "get_pending_invitations")  # NoReturn
