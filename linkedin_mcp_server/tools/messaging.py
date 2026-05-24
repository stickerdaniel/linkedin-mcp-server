"""
LinkedIn messaging tools.

Provides inbox listing, conversation reading, message search, and sending.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def register_messaging_tools(mcp: FastMCP) -> None:
    """Register all messaging-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Inbox",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_inbox(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List recent conversations from the LinkedIn messaging inbox.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations to load (1-50, default 20)

        Returns:
            Dict with url, sections (inbox -> raw text), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_inbox"
            )
            logger.info("Fetching inbox (limit=%d)", limit)

            await ctx.report_progress(
                progress=0, total=100, message="Loading messaging inbox"
            )

            result = await extractor.get_inbox(limit=limit)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_inbox")
        except Exception as e:
            raise_tool_error(e, "get_inbox")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Conversation",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_conversation(
        ctx: Context,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read a specific messaging conversation.

        Provide either thread_id or linkedin_username to identify the conversation.
        If both are provided, thread_id is used.

        Args:
            ctx: FastMCP context for progress reporting
            thread_id: LinkedIn messaging thread ID
            linkedin_username: LinkedIn username of the conversation participant

        Returns:
            Dict with url, sections (conversation -> raw text), and optional references.
        """
        if not linkedin_username and not thread_id:
            raise_tool_error(
                LinkedInScraperException(
                    "Provide at least one of linkedin_username or thread_id"
                ),
                "get_conversation",
            )

        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_conversation"
            )
            logger.info(
                "Fetching conversation: username=%s, thread_id=%s",
                linkedin_username,
                thread_id,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading conversation"
            )

            result = await extractor.get_conversation(
                linkedin_username=linkedin_username,
                thread_id=thread_id,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_conversation")
        except Exception as e:
            raise_tool_error(e, "get_conversation")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Search Conversations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "search"},
        exclude_args=["extractor"],
    )
    async def search_conversations(
        keywords: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search messages by keyword.

        Args:
            keywords: Search keywords to filter conversations
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (search_results -> raw text), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_conversations"
            )
            logger.info("Searching conversations: keywords='%s'", keywords)

            await ctx.report_progress(
                progress=0, total=100, message="Searching messages"
            )

            result = await extractor.search_conversations(keywords)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_conversations")
        except Exception as e:
            raise_tool_error(e, "search_conversations")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Send Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
        exclude_args=["extractor"],
    )
    async def send_message(
        message: str,
        confirm_send: bool,
        ctx: Context,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        profile_urn: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a message to a LinkedIn user.

        Preferred path is thread_id (existing conversation). If thread_id is not
        provided, the tool falls back to profile-based routing via linkedin_username
        (and optionally profile_urn). This is a write operation when confirm_send
        is True.

        Args:
            linkedin_username: LinkedIn username of the recipient (fallback path)
            message: The message text to send
            confirm_send: Must be True to send the message
            ctx: FastMCP context for progress reporting
            thread_id: Optional LinkedIn messaging thread ID (preferred path)
            profile_urn: Optional profile URN (e.g. ACoAAB...) to construct the
                compose URL directly. Providing this bypasses the Message-button
                lookup and is more reliable when available. Obtain via
                get_person_profile. Note: inbox may not always show all
                messages; use search_conversations as a fallback.

        Returns:
            Dict with url, status, message, recipient_selected, and sent.
        """
        if not thread_id and not linkedin_username:
            raise_tool_error(
                LinkedInScraperException(
                    "Provide at least one of thread_id or linkedin_username"
                ),
                "send_message",
            )

        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="send_message"
            )
            logger.info(
                "Sending message: username=%s, thread_id=%s (confirm_send=%s)",
                linkedin_username,
                thread_id,
                confirm_send,
            )

            await ctx.report_progress(progress=0, total=100, message="Sending message")

            result = await extractor.send_message(
                linkedin_username,
                message,
                confirm_send=confirm_send,
                thread_id=thread_id,
                profile_urn=profile_urn,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "send_message")
        except Exception as e:
            raise_tool_error(e, "send_message")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Star Conversation",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
        exclude_args=["extractor"],
    )
    async def star_conversation(
        thread_id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Mark a LinkedIn messaging conversation as starred (favorite).

        Opens the conversation by thread_id and toggles the Star action in the
        conversation header or overflow menu.

        Args:
            thread_id: LinkedIn messaging thread ID
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, thread_id, and starred.
            Statuses: starred, already_starred, star_unavailable, star_failed.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="star_conversation"
            )
            logger.info("Starring conversation thread_id=%s", thread_id)

            await ctx.report_progress(
                progress=0,
                total=100,
                message="Starring conversation",
            )

            result = await extractor.star_conversation(thread_id)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "star_conversation")
        except Exception as e:
            raise_tool_error(e, "star_conversation")  # NoReturn
