"""
LinkedIn post search and post engagement tools.

``search_posts`` performs LinkedIn's global content search (the "Posts" results
tab) using innerText extraction, so informal "we're hiring" / "Buscamos ..."
posts can be found before a formal job listing is published. It mirrors
search_people: build a /search/results/content/ URL, scroll to load results,
and return the raw innerText for the LLM to parse, plus post-permalink
references.

The other three tools write. ``react_to_post`` adds a reaction,
``comment_on_post`` publishes a comment and ``repost_post`` reshares, all
against one post permalink of the kind ``search_posts`` and ``get_feed`` return
as ``references[...]`` entries of kind ``feed_post``. They return an action
status rather than ``{url, sections}``; see ``post_action_result``.

Only the reaction fires without a confirmation flag, and that asymmetry is
deliberate rather than an oversight. A reaction is one control the owner can
un-click, while a comment and a repost are content published under their name
that a retry duplicates. ``react_to_post`` also refuses to click a control that
is already pressed, because on LinkedIn that click *removes* a reaction.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.contracts import (
    POST_ACTION_INTERRUPTED_WARNING,
    FilterValidationError,
    refuse_invalid_post_text,
)
from linkedin_mcp_server.scraping.identifiers import normalize_post_reference

logger = logging.getLogger(__name__)

Reaction = Literal["like", "celebrate", "support", "love", "insightful", "funny"]


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post search and post engagement tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "search"},
        exclude_args=["extractor"],
    )
    async def search_posts(
        keywords: str,
        ctx: Context,
        date_posted: str | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 3,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search LinkedIn posts/content globally by keyword (the "Posts" tab).

        Use this to catch informal hiring posts ("we're hiring", "Buscamos
        ...", "estamos contratando", "join our team") that often appear before
        a formal job listing exists. This is global content search, distinct
        from get_feed (your own home feed) and get_company_posts (one
        company's page).

        Args:
            keywords: Search keywords (e.g., "Buscamos Unity", "AI automation hiring")
            ctx: FastMCP context for progress reporting
            date_posted: Optional recency filter. One of "past-24h",
                "past-week", "past-month"; the "past_24_hours" / "past_week" /
                "past_month" spellings used by search_jobs are accepted too.
                Omit for any time.
            max_pages: Scroll depth as result "pages" of ~5 scrolls each
                (1-10, default 3). Content search is an infinite scroll, so
                this caps how far the page is scrolled rather than fetching
                discrete pages.

        Returns:
            Dict with url, sections (search_results -> raw text), and optional
            references (post authors, companies, linked jobs, and kind
            "feed_post" permalinks read from the page's payload responses —
            /feed/update/<urn>/ or /posts/<slug>, both valid permalinks) and
            section_errors. The DOM carries no per-post permalink anchors;
            captured permalinks are not aligned to result order. The LLM
            should parse the raw text to extract each post's author,
            headline/role, company, body, posted date, and reaction/comment
            counts.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_posts"
            )
            logger.info(
                "Searching posts: keywords='%s', date_posted='%s', max_pages=%d",
                keywords,
                date_posted,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting post search"
            )

            try:
                result = await extractor.search_posts(
                    keywords,
                    date_posted=date_posted,
                    max_pages=max_pages,
                )
            except FilterValidationError as e:
                # Validation messages carry actionable detail; surface them as
                # ToolError so mask_error_details doesn't reduce them to a
                # generic "Error calling tool 'search_posts'".
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            # Already a properly formatted client-facing error; do not log it
            # as "Unexpected error" via raise_tool_error.
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_posts")
        except Exception as e:
            raise_tool_error(e, "search_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="React To Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def react_to_post(
        post: str,
        ctx: Context,
        reaction: Reaction = "like",
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        React to one LinkedIn post, as the authenticated user.

        This is a write operation and is publicly attributed: reactions are
        visible on the post and can appear in other members' feeds. It takes no
        confirmation flag because a reaction is the one engagement the owner can
        withdraw from the same control that set it.

        Reacting is never a toggle here. If this account has already reacted,
        the tool returns ``already_reacted`` without clicking, because clicking a
        pressed reaction control on LinkedIn removes the reaction rather than
        changing it. To change or remove an existing reaction, do it in LinkedIn.

        Args:
            post: Permalink of one post, in either shape ``references`` returns
                for ``kind: "feed_post"`` — ``/feed/update/<urn>/`` or
                ``/posts/<slug>``. An absolute URL on any locale subdomain and a
                bare ``urn:li:{ugcPost,share,activity}:<id>`` are accepted too.
            ctx: FastMCP context for progress reporting
            reaction: Which reaction to add. One of like, celebrate, support,
                love, insightful, funny. "like" clicks the post's own reaction
                control directly; every other value has to open the reaction
                picker, which can refuse with ``reaction_picker_unavailable`` or
                ``reaction_picker_changed`` if LinkedIn's picker is not the six
                controls this tool knows how to index.

        Returns:
            Dict with url, status, message, acted, retry_safe and reaction.
            ``acted`` is true only after the reaction control reported itself as
            pressed; it does not claim anybody saw the reaction. ``retry_safe``
            is false from the moment a click is dispatched, and a retry while it
            is false can remove a reaction that did land.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="react_to_post"
            )
            logger.info("Reacting to post %s with %s", post, reaction)

            await ctx.report_progress(progress=0, total=100, message="Opening post")

            result = await extractor.react_to_post(post, reaction=reaction)

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                if result.get("retry_safe") is False:
                    logger.warning(POST_ACTION_INTERRUPTED_WARNING)
                raise

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "react_to_post")
        except Exception as e:
            raise_tool_error(e, "react_to_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Comment On Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def comment_on_post(
        post: str,
        comment: str,
        confirm_comment: bool,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Publish a comment on one LinkedIn post, as the authenticated user.

        This is a write operation when confirm_comment is True, and it is public
        and attributed: the comment carries this account's name and can notify
        the post's author and other commenters. Call it first with
        confirm_comment=False to check that the post loads and offers a comment
        editor without typing anything.

        The comment is confirmed by finding the exact text rendered inside that
        post afterwards. A ``comment_unconfirmed`` status means the submit was
        dispatched and the text never appeared, which is not the same as a
        failure — open the post before retrying.

        Args:
            post: Permalink of one post, in either shape ``references`` returns
                for ``kind: "feed_post"``. See react_to_post for the accepted
                forms.
            comment: Text to publish. Newlines are allowed; every other control
                character is rejected before a browser is touched.
            confirm_comment: Must be True to publish the comment
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, message, acted and retry_safe. ``acted`` is
            true only when the submitted text was found rendered on the post.
            ``retry_safe`` is false from the moment the submit is dispatched;
            retrying while it is false can publish the comment twice.
        """
        try:
            # Answered before a session is acquired, for the reason
            # send_message gives: caller-owned text needs no browser, and
            # acquiring one can spend a login attempt and return an
            # authentication error in place of the refusal the caller can act
            # on. Inside the `try` because normalizing the permalink raises
            # `InvalidReferenceError`, which has to reach `raise_tool_error` to
            # keep its correction instead of being masked.
            refusal = refuse_invalid_post_text(
                normalize_post_reference(post), comment, field="comment"
            )
            if refusal is not None:
                return refusal

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="comment_on_post"
            )
            logger.info(
                "Commenting on post %s (confirm_comment=%s)", post, confirm_comment
            )

            await ctx.report_progress(progress=0, total=100, message="Opening post")

            result = await extractor.comment_on_post(
                post,
                comment,
                confirm_comment=confirm_comment,
            )

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                # Same last-await hazard send_message documents: this
                # notification is the final await inside FastMCP's
                # `anyio.fail_after()`, and a deadline landing here discards a
                # result that may say the comment was published. Quiet when the
                # result says a retry is safe.
                if result.get("retry_safe") is False:
                    logger.warning(POST_ACTION_INTERRUPTED_WARNING)
                raise

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "comment_on_post")
        except Exception as e:
            raise_tool_error(e, "comment_on_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Repost Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"post", "actions"},
        exclude_args=["extractor"],
    )
    async def repost_post(
        post: str,
        confirm_repost: bool,
        ctx: Context,
        commentary: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Reshare one LinkedIn post to this account's own feed.

        This is a write operation when confirm_repost is True, and it publishes
        to this account's feed under its own name. Call it first with
        confirm_repost=False to check that the post loads and offers a repost
        control without opening any menu.

        Omit commentary for a bare repost, which publishes immediately once the
        menu item is clicked. Pass commentary to repost with your own text,
        which opens LinkedIn's composer instead.

        Which menu item reposts immediately is decided by position, so the tool
        refuses with ``repost_menu_changed`` unless LinkedIn's repost menu is
        exactly the two items it knows. That refusal is deliberate: the wrong
        index publishes to this account's feed.

        Args:
            post: Permalink of one post, in either shape ``references`` returns
                for ``kind: "feed_post"``. See react_to_post for the accepted
                forms.
            confirm_repost: Must be True to publish the repost
            ctx: FastMCP context for progress reporting
            commentary: Optional text to publish above the reshared post.
                Newlines are allowed; every other control character is rejected
                before a browser is touched. Omit for a bare repost.

        Returns:
            Dict with url, status, message, acted and retry_safe. A bare repost
            is confirmed by the post's own controls no longer rendering the same
            text, and a repost with commentary by that text appearing; neither
            claims anybody saw it. ``retry_safe`` is false from the moment the
            repost is dispatched, and retrying while it is false can repost
            twice.
        """
        try:
            # Before a session, as in comment_on_post above.
            if commentary is not None:
                refusal = refuse_invalid_post_text(
                    normalize_post_reference(post), commentary, field="commentary"
                )
                if refusal is not None:
                    return refusal

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="repost_post"
            )
            logger.info(
                "Reposting %s (confirm_repost=%s, with_commentary=%s)",
                post,
                confirm_repost,
                commentary is not None,
            )

            await ctx.report_progress(progress=0, total=100, message="Opening post")

            result = await extractor.repost_post(
                post,
                confirm_repost=confirm_repost,
                commentary=commentary,
            )

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                if result.get("retry_safe") is False:
                    logger.warning(POST_ACTION_INTERRUPTED_WARNING)
                raise

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "repost_post")
        except Exception as e:
            raise_tool_error(e, "repost_post")  # NoReturn
