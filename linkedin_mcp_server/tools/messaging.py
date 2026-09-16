"""
LinkedIn messaging tools.

Provides inbox listing, conversation reading, message search, and sending.
"""

import logging
import os
from typing import Annotated, Any, Literal

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

# Which messaging implementation `get_inbox` uses when the caller does not say.
# Set LINKEDIN_MESSAGING_BACKEND to "voyager", "dom" or "auto" (default).
# "auto" prefers Voyager and falls back to the DOM scrape, so a LinkedIn-side
# change degrades instead of failing.
_BACKEND_ENV = "LINKEDIN_MESSAGING_BACKEND"


def _default_backend() -> str:
    value = (os.environ.get(_BACKEND_ENV) or "auto").strip().lower()
    if value not in {"auto", "voyager", "dom"}:
        logger.warning(
            "%s=%r is not one of auto/voyager/dom; using auto",
            _BACKEND_ENV,
            value,
        )
        return "auto"
    return value


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
        limit: Annotated[int, Field(ge=1, le=500)] = 20,
        backend: Literal["default", "auto", "voyager", "dom"] = "default",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List recent conversations from the LinkedIn messaging inbox.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations to load (1-500, default 20)
            backend: Which implementation to use.
                "voyager" reads LinkedIn's own conversations API: it reaches the
                whole mailbox and clicks nothing.
                "dom" scrapes the rendered sidebar: it sees only what LinkedIn
                painted (observed ~16-17 rows) and click-visits each row to
                recover its thread id, which MARKS THOSE ROWS READ.
                "auto" prefers Voyager and falls back to "dom" on failure.
                "default" (the default) defers to the LINKEDIN_MESSAGING_BACKEND
                environment variable, itself defaulting to "auto".

        Returns:
            Dict with url, sections (inbox -> raw text), and optional references.
            The Voyager backend additionally returns `conversations` (structured,
            with thread_urn/read/unread_count/last_activity_at), `backend`,
            `pages_fetched`, and `exhausted`. **Check `exhausted` before treating
            the result as a complete mailbox**: False means the walk stopped on
            `limit` and more conversations exist.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_inbox"
            )
            chosen = _default_backend() if backend == "default" else backend
            logger.info("Fetching inbox (limit=%d, backend=%s)", limit, chosen)

            await ctx.report_progress(
                progress=0, total=100, message="Loading messaging inbox"
            )

            result = await extractor.get_inbox(limit=limit, backend=chosen)

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
        title="Get All Conversations",
        # Genuinely read-only, unlike get_inbox: this reads LinkedIn's own
        # conversations API and never clicks a row, so no thread is marked read.
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"messaging", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_all_conversations(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
        max_pages: Annotated[int, Field(ge=1, le=200)] = 60,
        cursor: str | None = None,
        quiet_for_days: Annotated[int | None, Field(ge=1)] = None,
        awaiting_reply_only: bool = False,
        category: str | None = None,
        page_size: Annotated[int, Field(ge=1, le=25)] = 25,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Page the entire messaging mailbox and return structured conversations.

        Use this, not get_inbox, whenever the question is about the mailbox as a
        whole — "which threads are unanswered", "have I replied to everyone",
        reconciling against an external record. get_inbox returns only what
        LinkedIn has painted into the sidebar (observed: ~16-17 rows) and
        click-visits each row to recover its thread id, which marks those rows
        read. This reads the conversations API the web client itself calls, so
        it reaches the whole mailbox and alters nothing.

        Each conversation carries thread_urn (the thread id), participants,
        last_activity_at, last_read_at, read, unread_count and categories
        (the Focused/Other mailbox), so "unanswered" can be computed directly.

        Paging is recency-ordered, so page one is the most recent conversations.
        Pass the returned next_cursor back in as `cursor` to continue from where
        a previous call stopped, rather than re-walking from the top.

        Reconnect work ("who have I fallen out of touch with") is what
        quiet_for_days is for. It is necessarily a LONG walk: LinkedIn ignores
        lastUpdatedBefore, so there is no way to jump to a date server-side and
        dormant threads sit behind every recent one. `category` is the one
        filter the server does honour, so prefer it when it fits the question. Filters narrow what is RETURNED, never
        what is walked, and `scanned` reports how many were examined so a
        filtered-empty result is distinguishable from an empty mailbox.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum conversations to return (1-2000, default 200)
            max_pages: Safety cap on cursor pages to walk (1-200, default 60)
            cursor: next_cursor from a previous call, to resume the walk
            quiet_for_days: only return threads with no activity for this many
                days — the reconnect filter
            awaiting_reply_only: only return threads whose newest message is
                theirs, so a reply is owed
            category: SERVER-SIDE filter, the only one LinkedIn actually honours.
                One of INBOX, PRIMARY_INBOX, ARCHIVE, INMAIL, STARRED, SPAM.
                These jump anywhere in time in a single call. An unknown value
                is rejected rather than passed through, because the API answers
                one with an empty page rather than an error.
            page_size: rows per request, max 25 (measured). Above 25 the API
                returns EMPTY rather than an error, so this is clamped.

        Returns:
            Dict with conversations, count, pages_fetched, and exhausted.
            **exhausted is the field that matters for any reconciliation**: when
            it is False the walk stopped on limit or max_pages and the mailbox
            holds more than was returned, so the result must not be treated as a
            complete census.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_all_conversations"
            )
            logger.info(
                "Paging all conversations (limit=%d, max_pages=%d)", limit, max_pages
            )

            await ctx.report_progress(
                progress=0, total=100, message="Paging conversations"
            )

            result = await extractor.get_all_conversations(
                limit=limit,
                max_pages=max_pages,
                cursor=cursor,
                quiet_for_days=quiet_for_days,
                awaiting_reply_only=awaiting_reply_only,
                category=category,
                page_size=page_size,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_all_conversations")
        except Exception as e:
            raise_tool_error(e, "get_all_conversations")  # NoReturn

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
