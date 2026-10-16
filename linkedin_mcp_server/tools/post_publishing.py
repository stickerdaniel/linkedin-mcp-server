"""Explicitly confirmed personal-post publishing tools."""

from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor


def register_post_publishing_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register separate preview and publish operations; never auto-publish."""

    @mcp.tool(
        timeout=tool_timeout,
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post"},
    )
    async def prepare_post(text: str, ctx: Context) -> dict[str, Any]:
        """Prepare a 10-minute plain-text personal post preview; does not publish.

        Present the exact text, author and Anyone audience to the user. Only
        call publish_post after the user explicitly approves that preview.
        Images, videos, polls, company posts and other audiences are unsupported.
        """
        extractor = await get_ready_extractor(ctx, tool_name="prepare_post")
        try:
            return await extractor.prepare_post(text)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        timeout=tool_timeout,
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        tags={"post"},
    )
    async def publish_post(
        draft_id: str, approved_text: str, ctx: Context, confirmed: bool = False
    ) -> dict[str, Any]:
        """Publish once ONLY after explicit human approval of the exact preview.

        confirmed defaults to false. Changed/expired/attempted previews are
        refused. Never retry an outcome_unknown; inspect LinkedIn manually.
        Only observed post-permalink navigation confirms publication. Composer
        variants not supported by this version fail closed. No private API used.
        """
        if not confirmed:
            return {"status": "confirmation_required", "published": False}
        extractor = await get_ready_extractor(ctx, tool_name="publish_post")
        try:
            return await extractor.publish_post(
                draft_id, approved_text, confirmed=confirmed
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
