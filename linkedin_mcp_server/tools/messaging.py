"""
LinkedIn messaging tools.

Provides inbox listing, conversation reading, message search, and sending.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    LinkedInScraperException,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import (
    SEND_INTERRUPTED_WARNING,
    refuse_an_invalid_message,
)

logger = logging.getLogger(__name__)


def register_messaging_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all messaging-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
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

        Note: this reads the rendered sidebar, so it sees only what LinkedIn has
        painted and it recovers thread ids by click-visiting rows, which marks
        them read. For a whole-mailbox question -- which threads are unanswered,
        who has gone quiet, reconciling against an external record -- use
        get_conversations instead.
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
        timeout=tool_timeout,
        title="Get Conversations",
        # Genuinely read-only, unlike get_inbox: this reads LinkedIn's own
        # conversations API and never clicks a row, so no thread is marked read.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_conversations(
        ctx: Context,
        cursor: str | None = None,
        category: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read ONE page of conversations (up to 25) from LinkedIn's messaging API.

        Use this, not get_inbox, for anything about the mailbox as a whole:
        which threads are unanswered, who has gone quiet, reconciling against an
        external record. get_inbox returns only what LinkedIn painted into the
        sidebar and click-visits each row to recover its thread id, which marks
        those rows read. This reads the API the web client itself calls, so it
        reaches any page of the mailbox and alters nothing.

        To read more, call again with `cursor` set to the `next_cursor` you were
        given. Paging is recency-first, so page one is the most recent
        conversations. Reconnect work ("who have I fallen out of touch with")
        means paging backwards until `last_activity_iso` is old enough; that is
        deliberately the caller's loop, since only the caller knows when to stop.

        Each conversation carries thread_urn, participants, last_activity_iso,
        read, unread_count, last_message_text and awaiting_my_reply, so
        "have I replied to everyone" is a field rather than an inference.

        Args:
            ctx: FastMCP context for progress reporting
            cursor: next_cursor from a previous call. Omit for the first page.
            category: SERVER-SIDE filter, the only one LinkedIn honours. One of
                INBOX, PRIMARY_INBOX, ARCHIVE, INMAIL, STARRED, SPAM. These
                reach any point in time in a single call. An unknown value is
                rejected rather than passed through, because the API answers one
                with an empty page that would read as "you have none".

        Returns:
            Dict with url and sections (the standard scraping-tool shape), plus
            conversations, count, page_size, next_cursor, at_end and
            zero_reason. `conversations` is the structured answer; `sections`
            carries the same page as readable text for generic consumers.

            **at_end is measured, not inferred**: True means the server returned
            FEWER than page_size, so there is no more. False means a full page,
            so there is more. **None means an empty page, which proves nothing
            either way and must never be read as the end.**

            zero_reason explains an empty page: "verified-empty" (a positive
            control confirmed the mailbox is empty) or "after-cursor" (the page
            after the last one). An empty page that cannot be explained raises
            rather than returning, so this never answers "no conversations" on
            the strength of a silent failure.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_conversations"
            )
            logger.info("Reading conversations page (cursor=%s)", bool(cursor))

            await ctx.report_progress(
                progress=0, total=100, message="Reading conversations"
            )

            result = await extractor.get_conversations(
                cursor=cursor,
                category=category,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_conversations")
        except Exception as e:
            raise_tool_error(e, "get_conversations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Conversation",
        # Not read-only, though it reads: resolving a username enumerates the
        # inbox by click-visiting rows, and LinkedIn marks a visited row as read.
        # The docstring below has always said so. An unread message the user has
        # not seen is state, and losing it is not something a reader should do.
        annotations={"openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_conversation(
        ctx: Context,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: Annotated[int, Field(ge=0)] = 0,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read a specific messaging conversation.

        Provide either linkedin_username or thread_id to identify the conversation.

        When looked up by linkedin_username, resolution searches the messaging
        inbox for the participant's display name and click-visits every
        matching row to capture its thread ID — LinkedIn's sidebar has no
        anchor hrefs or thread-id attributes, so this is the only available
        path. Each visit selects the row in the LinkedIn UI and may mark it
        as read. Pass thread_id directly to skip this enumeration.

        Args:
            ctx: FastMCP context for progress reporting
            linkedin_username: LinkedIn username of the conversation participant; a full profile URL is accepted too
            thread_id: LinkedIn messaging thread ID
            index: 0-based selector for which thread to open when the
                participant has multiple threads (e.g. an organic 1-on-1 plus
                an InMail). Ignored when thread_id is provided. To enumerate
                thread IDs first, call search_conversations.

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
                "Fetching conversation: username=%s, thread_id=%s, index=%d",
                linkedin_username,
                thread_id,
                index,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading conversation"
            )

            result = await extractor.get_conversation(
                linkedin_username=linkedin_username,
                thread_id=thread_id,
                index=index,
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
        timeout=tool_timeout,
        title="Search Conversations",
        # Same reason as `get_conversation`: enumerating result rows selects them
        # in LinkedIn's UI, which can mark them read. Its own `limit` argument is
        # documented in those terms.
        annotations={"openWorldHint": True},
        tags={"messaging", "search"},
        exclude_args=["extractor"],
    )
    async def search_conversations(
        keywords: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search messages by keyword.

        Args:
            keywords: Search keywords to filter conversations
            ctx: FastMCP context for progress reporting
            limit: Maximum number of search-result rows to enumerate as
                conversation references (1-50, default 20). Each enumeration
                selects the row in LinkedIn's UI and may mark it as read, so
                a low cap is preferable for noisy queries.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_conversations"
            )
            logger.info(
                "Searching conversations: keywords='%s', limit=%d", keywords, limit
            )

            await ctx.report_progress(
                progress=0, total=100, message="Searching messages"
            )

            result = await extractor.search_conversations(keywords, limit=limit)

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
        timeout=tool_timeout,
        title="Send Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
        exclude_args=["extractor"],
    )
    async def send_message(
        linkedin_username: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        profile_urn: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Compose and send a new message to a LinkedIn user.

        Profile-based targeting opens LinkedIn's compose flow. It is not a safe
        reply path for an existing recruiter/InMail or messaging thread: it may
        create a separate DM even after you inspect that thread with
        get_conversation or search_conversations. Those tools only read an
        existing thread; they do not send a reply. Until a thread-targeted send
        path is available, do not treat profile-based send_message as a reply.

        The recipient must be directly messageable from the profile page. If
        LinkedIn does not expose a normal Message action, use connect_with_person
        first, then retry send_message only after the connection request is
        accepted. Recipient authorization comes from validating one
        recipient-specific Message action carrying the target URN, then following
        its browser navigation and pinning the exact final route. Visible profile
        links or recipient URNs in the composer are optional corroboration; any
        contradiction fails closed. No Voyager or other private API is used. This
        is a write operation when confirm_send is True.

        Args:
            linkedin_username: LinkedIn username of the recipient; a full profile URL is accepted too
            message: Single-line message text to send. C0 control characters and
                DEL are rejected, including CR, LF, and tab.
            confirm_send: Must be True to send the message
            ctx: FastMCP context for progress reporting
            profile_urn: Optional profile URN (e.g. ACoAAB...) to verify against
                the URN exposed by the loaded profile before opening its Message
                action. It never bypasses recipient verification. Obtain via
                get_person_profile. Note: inbox may not always show all messages;
                use search_conversations as a fallback.

        Returns:
            Dict with url, status, message, recipient_selected, sent, and
            retry_safe. ``sent`` is true only after the submitted message's DOM
            node gains a different opaque event ID; this does not claim delivery
            or read status. It is false both where nothing was submitted and
            where the outcome is unknown. ``retry_safe`` separates the two: it
            is false from the moment a submission is attempted, and calling
            again while it is
            false can deliver the message twice.
        """
        try:
            # Answered before a session is acquired. Caller-owned message
            # validation needs no browser, and acquiring one can spend a login
            # attempt and come back as an authentication error instead of the
            # refusal the caller can act on. Inside the `try` because building
            # the refusal normalizes the recipient, and an unusable one raises
            # `InvalidReferenceError`; outside, that error would skip
            # `raise_tool_error` and reach the caller masked by
            # `mask_error_details` instead of naming the correction.
            refusal = refuse_an_invalid_message(linkedin_username, message)
            if refusal is not None:
                return refusal
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="send_message"
            )
            logger.info(
                "Sending message to %s (confirm_send=%s)",
                linkedin_username,
                confirm_send,
            )

            await ctx.report_progress(progress=0, total=100, message="Sending message")

            result = await extractor.send_message(
                linkedin_username,
                message,
                confirm_send=confirm_send,
                profile_urn=profile_urn,
            )

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                # The send has already answered, and this notification is the
                # last await inside FastMCP's `anyio.fail_after()`. A deadline
                # landing here discards a result that may say the send was
                # confirmed, and nothing can hand it back afterwards, so the
                # log line is all that is left. Quiet where the result says a
                # retry is safe, because then there is nothing to warn about.
                if result.get("retry_safe") is False:
                    logger.warning(SEND_INTERRUPTED_WARNING)
                raise

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "send_message")
        except Exception as e:
            raise_tool_error(e, "send_message")  # NoReturn
