"""Approva's added tools.

Registered alongside upstream's tool modules from ``server.py``. Kept in one
file so a rebase onto upstream never has to merge inside a shared module.

Every write here honours the kill switch before it loads a page and reports
``page_state`` after it acts, per the bot's spec  3.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.approva.composer import (
    ComposerError,
    create_post as composer_create_post,
    inspect_composer as composer_inspect,
)
from linkedin_mcp_server.approva.guard import (
    StopFileError,
    raise_if_stopped,
    read_page_state,
    set_stop,
)
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error

logger = logging.getLogger(__name__)


def _page_and_goto(extractor: Any) -> tuple[Any, Any]:
    """Borrow the extractor's page and its auth-checked navigator.

    Upstream keeps both private. Reaching for them here rather than
    duplicating navigation means the Approva flows inherit upstream's
    login-barrier detection, remember-me handling and proxy redaction for
    free -- and it is the only coupling this fork has to upstream internals.
    """
    page = getattr(extractor, "_page", None)
    goto = getattr(extractor, "_goto_with_auth_checks", None)
    if page is None or goto is None:
        raise ToolError(
            "This build of linkedin-mcp-server does not expose the browser page "
            "the Approva tools need; the fork needs rebasing."
        )
    return page, goto


def register_approva_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register Approva's tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Create or Schedule a LinkedIn Post",
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True},
        tags={"approva", "post", "write"},
        exclude_args=["extractor"],
    )
    async def create_post(
        text: Annotated[
            str,
            Field(description="The post body, exactly as it should appear.",
                  min_length=1, max_length=3000),
        ],
        ctx: Context,
        image_paths: Annotated[
            list[str] | None,
            Field(description="Absolute paths to images to attach, in order."),
        ] = None,
        schedule_at: Annotated[
            str | None,
            Field(description=(
                "Local ISO-8601 time to schedule for, e.g. '2026-09-02T08:30'. "
                "Must be >=10 minutes ahead and on a 5-minute boundary. "
                "Omit to publish immediately."
            )),
        ] = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Publish a post to the authenticated member's own feed, or hand it to
        LinkedIn's native scheduler to publish later.

        Scheduling uses LinkedIn's own scheduler rather than holding the post
        locally, so the machine running this server does not need to be awake
        when the post goes out.

        Refuses while the Approva kill switch file exists. Sets the kill
        switch automatically if LinkedIn shows a login or challenge page after
        the action.

        Returns:
            Dict with status ("posted" or "scheduled"), submitted,
            composer_closed, characters, images_attached, scheduled_for, and
            page_state ("normal" | "login" | "challenge").
        """
        try:
            raise_if_stopped("create_post")

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="create_post"
            )
            page, goto = _page_and_goto(extractor)

            logger.info(
                "create_post: %d chars, %d image(s), schedule_at=%s",
                len(text),
                len(image_paths or []),
                schedule_at,
            )
            await ctx.report_progress(
                progress=0, total=100, message="Opening composer"
            )

            result = await composer_create_post(
                page,
                goto,
                text=text,
                image_paths=image_paths,
                schedule_at=schedule_at,
            )

            state = await read_page_state(page)
            result["page_state"] = state
            if state != "normal":
                # The post may or may not have landed; either way the account
                # is in a state no further automation should touch.
                set_stop(
                    f"create_post saw page_state={state} at {result.get('url')}"
                )

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except StopFileError as e:
            raise ToolError(str(e)) from e
        except ComposerError as e:
            raise ToolError(str(e)) from e
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "create_post")
        except Exception as e:
            raise_tool_error(e, "create_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Inspect the Post Composer",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"approva", "post", "diagnostic"},
        exclude_args=["extractor"],
    )
    async def inspect_composer(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Open the post composer, report every control it exposes, and close it
        without posting.

        A diagnostic: it types nothing and submits nothing, so it is a read as
        far as the account is concerned. Use it to re-pin selectors after
        LinkedIn changes the composer, and as the first check when create_post
        reports that it could not find a control.

        Returns:
            Dict with trigger_matched, dialog_opened, dialogLabel, and the
            buttons / textboxes / inputs the composer renders, each with its
            role, aria-label and visible text.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="inspect_composer"
            )
            page, goto = _page_and_goto(extractor)
            result = await composer_inspect(page, goto)
            result["page_state"] = await read_page_state(page)
            return result
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "inspect_composer")
        except Exception as e:
            raise_tool_error(e, "inspect_composer")  # NoReturn

    register_approva_write_wrappers(mcp, tool_timeout=tool_timeout)


def register_approva_write_wrappers(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Kill-switch-aware wrappers around upstream's existing write tools.

    Upstream already implements the connection and message flows properly --
    state detection, locale independence, note-quota handling. Re-implementing
    them would be worse code and a bigger rebase. What upstream does not do is
    honour Approva's kill switch or report page state, so these wrappers add
    exactly that and delegate the rest.

    Upstream's own ``connect_with_person`` / ``send_message`` remain
    registered and unguarded; the bot's dispatcher must call the ``approva_``
    names. Nothing here creates an approval -- the caller has one or it does
    not reach this code.
    """

    @mcp.tool(
        timeout=tool_timeout,
        title="Send a Connection Request (guarded)",
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True},
        tags={"approva", "person", "write"},
        exclude_args=["extractor"],
    )
    async def approva_connect(
        linkedin_username: Annotated[
            str, Field(description="Username or full profile URL.")
        ],
        ctx: Context,
        note: Annotated[
            str | None,
            Field(description="Optional invite note, at most 300 characters.",
                  max_length=300),
        ] = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a LinkedIn connection request, refusing while the kill switch is set.

        Delegates to upstream's connection flow, which detects already-connected,
        pending and follow-only states before it will write anything.

        Returns:
            Upstream's connect result plus page_state
            ("normal" | "login" | "challenge").
        """
        try:
            raise_if_stopped("approva_connect")
            if note is not None and len(note) > 300:
                raise ToolError(
                    f"Invite note is {len(note)} characters; LinkedIn's limit is 300."
                )

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="approva_connect"
            )
            page, _ = _page_and_goto(extractor)

            result = await extractor.connect_with_person(
                linkedin_username, note=note
            )

            state = await read_page_state(page)
            result["page_state"] = state
            if state != "normal":
                set_stop(f"approva_connect saw page_state={state}")
            return result

        except StopFileError as e:
            raise ToolError(str(e)) from e
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "approva_connect")
        except Exception as e:
            raise_tool_error(e, "approva_connect")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Send a Direct Message (guarded)",
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True},
        tags={"approva", "messaging", "write"},
        exclude_args=["extractor"],
    )
    async def approva_send_message(
        linkedin_username: Annotated[
            str, Field(description="Username or full profile URL of the recipient.")
        ],
        message: Annotated[str, Field(description="Message text.", min_length=1)],
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a LinkedIn direct message, refusing while the kill switch is set.

        Delegates to upstream's messaging flow. Upstream's confirm_send gate is
        passed as True here: in Approva the gate is membership of the write
        queue, which is what the caller had to clear to reach this tool.

        Returns:
            Upstream's message result plus page_state
            ("normal" | "login" | "challenge").
        """
        try:
            raise_if_stopped("approva_send_message")

            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="approva_send_message"
            )
            page, _ = _page_and_goto(extractor)

            result = await extractor.send_message(
                linkedin_username, message, confirm_send=True
            )

            state = await read_page_state(page)
            result["page_state"] = state
            if state != "normal":
                set_stop(f"approva_send_message saw page_state={state}")
            return result

        except StopFileError as e:
            raise ToolError(str(e)) from e
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "approva_send_message")
        except Exception as e:
            raise_tool_error(e, "approva_send_message")  # NoReturn
