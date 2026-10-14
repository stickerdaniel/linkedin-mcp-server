"""
LinkedIn messaging tools.

Provides inbox listing, conversation reading, message search, archiving,
sending, and replying in an existing thread.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.contracts import (
    SEND_INTERRUPTED_WARNING,
    before_the_reply_deadline,
    refuse_an_invalid_message,
    refuse_an_invalid_reply,
)
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_person_identifier,
    normalize_profile_urn,
    normalize_thread_id,
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
        tags={"messaging"},
    )
    @tool_errors
    async def get_inbox(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """
        List recent conversations from the LinkedIn messaging inbox.

        The returned inbox text and result URL come from the ordinary messaging
        inbox. Click-derived conversation references are collected separately
        after requesting the compose page, which avoided inbox auto-opening on
        the measured variant. A row contributes a click-derived reference only
        after its click is followed by an observed different thread path. The
        scan stops at its first unverifiable click. section_errors.inbox
        reports that stop or unavailable scan rows; captured inbox text and
        independently extracted anchors retain their normal handling. A known
        thread_id can bypass row attribution when calling get_conversation.

        Args:
            ctx: FastMCP context for progress reporting
            limit: Maximum number of conversations to load (1-50, default 20)

        Returns:
            Dict with url, sections (inbox -> raw text), optional references, and
            optional section_errors (inbox -> why click-derived references are
            incomplete).
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_inbox")
        logger.info("Fetching inbox (limit=%d)", limit)

        await ctx.report_progress(
            progress=0, total=100, message="Loading messaging inbox"
        )

        result = await extractor.get_inbox(limit=limit)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Conversation",
        # Not read-only, though it reads: resolving a username enumerates the
        # inbox by click-visiting rows, and LinkedIn marks a visited row as read.
        # The docstring below has always said so. An unread message the user has
        # not seen is state, and losing it is not something a reader should do.
        annotations={"openWorldHint": True},
        tags={"messaging"},
    )
    @tool_errors
    async def get_conversation(
        ctx: Context,
        linkedin_username: str | None = None,
        thread_id: str | None = None,
        index: Annotated[int, Field(ge=0)] = 0,
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

        Username resolution scans matching rows from a requested compose page
        first. Its indexable sequence ends before the first unresolved click,
        missing matching click target, or admitted row that fails the existing
        exact display-name check. An index outside that verified prefix is
        refused with its reason. Search is a fallback only when the inbox scan
        has no observed matching result and no such barrier; it never
        substitutes a result after a stopped or gapped inbox scan. Pass a known
        thread_id to bypass username/index resolution.

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
            raise InvalidReferenceError(
                "Provide at least one of linkedin_username or thread_id"
            )
        if thread_id:
            thread_id = normalize_thread_id(thread_id)
        else:
            linkedin_username = normalize_person_identifier(linkedin_username or "")
        extractor = await get_ready_extractor(ctx, tool_name="get_conversation")
        logger.info(
            "Fetching conversation: username=%s, thread_id=%s, index=%d",
            linkedin_username,
            thread_id,
            index,
        )

        await ctx.report_progress(progress=0, total=100, message="Loading conversation")

        result = await extractor.get_conversation(
            linkedin_username=linkedin_username,
            thread_id=thread_id,
            index=index,
        )

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Conversations",
        # Same reason as `get_conversation`: enumerating result rows selects them
        # in LinkedIn's UI, which can mark them read. Its own `limit` argument is
        # documented in those terms.
        annotations={"openWorldHint": True},
        tags={"messaging", "search"},
    )
    @tool_errors
    async def search_conversations(
        keywords: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        """
        Search messages by keyword.

        Click-derived references require an observed different thread path
        after each row click. The first unverifiable click stops further row
        clicks and is reported in section_errors.search_results. Already-read
        text and independently extracted anchors retain their normal handling.
        A result without that diagnostic does not guarantee that every
        conversation was enumerated.

        Args:
            keywords: Search keywords to filter conversations
            ctx: FastMCP context for progress reporting
            limit: Maximum number of search-result rows to enumerate as
                conversation references (1-50, default 20). Each enumeration
                selects the row in LinkedIn's UI and may mark it as read, so
                a low cap is preferable for noisy queries.

        Returns:
            Dict with url, sections (search_results -> raw text), optional
            references, and optional section_errors (search_results -> where
            click-derived references stopped).
        """
        extractor = await get_ready_extractor(ctx, tool_name="search_conversations")
        logger.info("Searching conversations: keywords='%s', limit=%d", keywords, limit)

        await ctx.report_progress(progress=0, total=100, message="Searching messages")

        result = await extractor.search_conversations(keywords, limit=limit)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Archive Conversation",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"messaging", "actions"},
    )
    @tool_errors
    async def archive_conversation(
        thread_id: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Archive one LinkedIn messaging thread, InMail included, by thread ID.

        Opens the thread, which marks it read, and chooses Archive from its
        options menu. A thread LinkedIn already files as archived is left
        alone and reported already_archived, so a repeated call clicks
        nothing. The change is confirmed by loading the thread again.
        Reversible on LinkedIn; a new message moves the thread back to the
        inbox.

        Args:
            thread_id: LinkedIn messaging thread ID, as get_inbox,
                search_conversations or get_conversation return it
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, thread_id, participant (the thread's names, or null)
            and status: "archived" or "already_archived".
        """
        thread_id = normalize_thread_id(thread_id)
        extractor = await get_ready_extractor(ctx, tool_name="archive_conversation")
        logger.info("Archiving conversation: %s", thread_id)

        await ctx.report_progress(progress=0, total=100, message="Opening conversation")

        result = await extractor.archive_conversation(thread_id)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Send Message",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
    )
    @tool_errors
    async def send_message(
        linkedin_username: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
        profile_urn: str | None = None,
    ) -> dict[str, Any]:
        """
        Compose and send a new message to a LinkedIn user.

        Profile-based targeting opens LinkedIn's compose flow. It is not a safe
        reply path for an existing recruiter/InMail or messaging thread: it may
        create a separate DM even after you inspect that thread with
        get_conversation or search_conversations. To answer an existing thread,
        including an InMail from someone who is not a connection, use
        reply_to_conversation with the thread's id instead.

        The recipient must be directly messageable from the profile page. If
        LinkedIn does not expose a normal Message action, use connect_with_person
        first, then retry send_message only after the connection request is
        accepted. A ``status`` of ``enter_to_send_enabled`` means the account
        has LinkedIn's "Press Enter to Send" preference on, which hides the Send
        button; relay the returned instructions to the user, who switches it to
        "Click Send to send" before retrying. The dry run (confirm_send False)
        reports it too. Recipient authorization comes from validating one
        recipient-specific Message action carrying the target URN, then following
        its browser navigation and pinning the exact final route. Visible profile
        links or recipient URNs in the composer are optional corroboration; any
        contradiction fails closed. No Voyager or other private API is used. This
        is a write operation when confirm_send is True.

        ``thread_id`` is present only on ``sent``: the conversation the
        confirmed message was observed in, or null when the page stayed on
        the compose route. ``retry_safe`` stays the authority for whether a
        retry is safe.

        Args:
            linkedin_username: LinkedIn username of the recipient; a full profile URL is accepted too
            message: Message text. LF, CRLF and CR all end a line; an empty line
                separates paragraphs and is kept. Whitespace-only lines count as
                empty, and leading and trailing whitespace of the whole message
                is removed, as LinkedIn does when sending. Tab, other control
                characters, DEL, U+0085, U+2028 and U+2029 are refused.
            confirm_send: Must be True to send the message
            ctx: FastMCP context for progress reporting
            profile_urn: Optional profile URN (e.g. ACoAAB...) to verify against
                the URN exposed by the loaded profile before opening its Message
                action. It never bypasses recipient verification. Obtain via
                get_person_profile. Note: inbox may not always show all messages;
                use search_conversations as a fallback.

        Returns:
            Dict with url, status, message, recipient_selected, sent, and
            retry_safe. ``thread_id`` is present only on ``sent``. ``sent``
            is true only after the thread shows the submitted
            text under a new server message ID (or its DOM node gains a
            different event ID); this does not claim delivery or read status.
            It is false both where nothing was submitted and
            where the outcome is unknown. ``retry_safe`` separates the two: it
            is false from the moment a submission is attempted, and calling
            again while it is
            false can deliver the message twice. A ``status`` of
            ``outcome_unknown`` is that same warning from the transport rather
            than the page: the call's answer never arrived from the browser
            process, so ``sent`` is absent instead of false and only LinkedIn
            itself can say whether the message left.
        """
        # Answered before a session is acquired. Caller-owned message
        # validation needs no browser, and acquiring one can spend a login
        # attempt and come back as an authentication error instead of the
        # refusal the caller can act on.
        refusal = refuse_an_invalid_message(linkedin_username, message)
        if refusal is not None:
            return refusal
        linkedin_username = normalize_person_identifier(linkedin_username)
        if profile_urn is not None:
            profile_urn = normalize_profile_urn(profile_urn)
        extractor = await get_ready_extractor(ctx, tool_name="send_message")
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
            # The send has already answered, and this notification is the
            # last await inside FastMCP's `anyio.fail_after()`. A deadline
            # landing here would discard a result that may say the send was
            # confirmed, so a notification that stalls gives up first.
            with before_the_reply_deadline():
                await ctx.report_progress(progress=100, total=100, message="Complete")
        except BaseException:
            # Cancellation from outside, such as a client that cancels,
            # which nothing can answer, so the log line is all that is
            # left. Quiet where the result says a retry is safe, because
            # then there is nothing to warn about.
            if result.get("retry_safe") is False:
                logger.warning(SEND_INTERRUPTED_WARNING)
            raise

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Reply to Conversation",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"messaging", "actions"},
    )
    @tool_errors
    async def reply_to_conversation(
        thread_id: str,
        message: str,
        confirm_send: bool,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Reply inside an existing LinkedIn messaging thread, InMail included.

        Opens /messaging/thread/<thread_id>/ and types into that thread's own
        reply composer, so the message lands in the same conversation. This is
        the path for answering a recruiter InMail, whose sender may have no
        Message action on their profile. Get the thread_id from get_inbox,
        search_conversations or get_conversation.

        The thread is verified before anything is typed: the page must land on
        exactly the requested thread and stay there, and the composer must be
        the page's one visible editor, inside the thread's main region, with
        one submit button in its own form. Any doubt fails closed. Quick-reply
        buttons an InMail shows ("Yes, interested...", "No thanks...") are
        never clicked; a thread that offers only those has no composer and
        returns composer_unavailable. A draft already in the composer is left
        untouched and returns composer_occupied. A ``status`` of
        ``enter_to_send_enabled`` means LinkedIn's "Press Enter to Send"
        preference hides the Send button; relay the returned instructions as
        for send_message. The dry run reports it too. No Voyager or other
        private API is used. This is a write operation when confirm_send is
        True.

        Args:
            thread_id: LinkedIn messaging thread ID, or a /messaging/thread/<id>/
                reference as another tool returned it
            message: Message text, read as send_message reads it: LF, CRLF and
                CR all end a line, empty lines are kept, and the whole message
                is trimmed. Tab, other control characters, DEL, U+0085, U+2028
                and U+2029 are refused.
            confirm_send: Must be True to send. False is a dry run that verifies
                the thread and composer, types nothing, and says who the reply
                would go to.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, recipient_selected, sent, retry_safe,
            thread_id and participant. ``participant`` is the name shown in the
            thread's title, or null where it could not be read. ``sent`` and
            ``retry_safe`` mean what they mean for send_message: ``sent`` is
            true only after the submitted message's DOM node gains a different
            opaque event ID, and ``retry_safe`` is false from the moment a
            submission is attempted, so calling again while it is false can
            deliver the message twice. A ``status`` of ``outcome_unknown``
            means the browser went away mid-call and only LinkedIn can say
            whether the message left.
        """
        # Answered before a session is acquired, as in send_message.
        refusal = refuse_an_invalid_reply(thread_id, message)
        if refusal is not None:
            return refusal
        extractor = await get_ready_extractor(ctx, tool_name="reply_to_conversation")
        logger.info("Replying in thread %s (confirm_send=%s)", thread_id, confirm_send)

        await ctx.report_progress(progress=0, total=100, message="Sending reply")

        result = await extractor.reply_to_conversation(
            thread_id,
            message,
            confirm_send=confirm_send,
        )

        try:
            await ctx.report_progress(progress=100, total=100, message="Complete")
        except BaseException:
            # Same window as send_message: a deadline here discards a
            # result that may say the reply was confirmed.
            if result.get("retry_safe") is False:
                logger.warning(SEND_INTERRUPTED_WARNING)
            raise

        return result
