"""Tools to list your connections and, with explicit approval, remove some.

``remove_connections`` modifies LinkedIn and cannot be undone by this server:
reconnecting needs a new invitation that the other person must accept. It runs
only while the server has ``MCP_LINKEDIN_WRITE_ENABLED=true`` and only with
``confirm=true``; ``dryRun=true`` checks each person without removing anyone.
"""

from __future__ import annotations

from typing import Any

import logging
from datetime import UTC, datetime

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import (
    get_ready_connection_remover,
    handle_auth_error,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.profile_edit import settings
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore

logger = logging.getLogger(__name__)

# At most this many removals per call: each takes a few page loads, and the
# pace stays human. Call again for the next batch.
MAX_REMOVALS_PER_CALL = 5
PACING_SECONDS = 4.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def register_connection_removal_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register connection listing and approval-gated removal."""

    @mcp.tool(
        timeout=tool_timeout,
        title="List My Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "connections"},
    )
    async def list_my_connections(ctx: Context) -> dict[str, Any]:
        """
        List all of your 1st-degree connections with their usernames. Read-only.

        Loads LinkedIn's connections page until the stated total is reached;
        ``complete`` says whether every connection was read.
        """
        try:
            remover = await get_ready_connection_remover(
                ctx, tool_name="list_my_connections"
            )
            return await remover.list_connections()
        except ProfileEditError as e:
            return e.to_result()
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "list_my_connections")
        except Exception as e:
            raise_tool_error(e, "list_my_connections")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Remove Connections",
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"connections", "actions"},
    )
    async def remove_connections(
        ctx: Context,
        usernames: list[str] = Field(
            min_length=1,
            max_length=MAX_REMOVALS_PER_CALL,
            description="Usernames (or profile URLs) from list_my_connections.",
        ),
        confirm: bool = False,
        dryRun: bool = False,
    ) -> dict[str, Any]:
        """
        Removes the given people from your connections. This modifies external state and cannot be undone here.

        Call only with people the user explicitly chose to remove. Requires
        confirm=true and a server started with MCP_LINKEDIN_WRITE_ENABLED=true,
        unless dryRun=true, which only checks that each person is a
        connection and that the remove option is there. Removes one person at
        a time, then reloads the list to verify they are gone; stops at the
        first failure. At most 5 people per call.

        Args:
            ctx: FastMCP context
            usernames: usernames or profile URLs from list_my_connections
            confirm: must be true for a real removal; the user's approval
            dryRun: check without removing anyone
        """
        store = ProfileEditStore(settings.edits_root())
        if not dryRun:
            if confirm is not True:
                return ProfileEditError(
                    ProfileEditErrorCode.CONFIRMATION_REQUIRED,
                    "remove_connections removes people from your network and needs confirm=true after the user approved the list.",
                ).to_result()
            if not settings.writes_enabled():
                return ProfileEditError(
                    ProfileEditErrorCode.WRITES_DISABLED
                ).to_result()
        results: list[dict[str, Any]] = []
        try:
            remover = await get_ready_connection_remover(
                ctx, tool_name="remove_connections"
            )
            for i, username in enumerate(usernames):
                if i:
                    await remover._session.delay(PACING_SECONDS)
                try:
                    result = await remover.remove(username, dry_run=dryRun)
                except ProfileEditError as e:
                    result = {
                        "username": username,
                        "status": "FAILED",
                        "verified": False,
                        **e.to_result(),
                    }
                results.append(result)
                store.audit(
                    at=_now(),
                    tool="remove_connections",
                    field=result["username"],
                    result=result["status"],
                    verified=result["verified"],
                    **({"error": result["error"]} if "error" in result else {}),
                )
                if not result["verified"]:
                    break
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "remove_connections")
        except (RateLimitError, AccountRestrictedError) as e:
            return {
                **ProfileEditError(
                    ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=str(e)[:200]
                ).to_result(),
                "results": results,
            }
        except Exception as e:
            raise_tool_error(e, "remove_connections")  # NoReturn
        done = {r["username"] for r in results}
        return {
            "dryRun": dryRun,
            "results": results
            + [
                {"username": u, "status": "NOT_ATTEMPTED", "verified": False}
                for u in usernames
                if u not in done
                and not any(r["username"] == u.lower() for r in results)
            ],
            "allDone": len(results) == len(usernames)
            and all(r["verified"] for r in results),
        }
