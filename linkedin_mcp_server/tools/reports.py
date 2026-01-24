"""
LinkedIn outreach reporting and statistics tools.

Provides MCP tools for viewing outreach statistics, action history,
and generating personalized connection messages.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.ai import get_message_generator
from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.storage import (
    ActionRepository,
    ActionStatus,
    ActionType,
)

logger = logging.getLogger(__name__)


def register_report_tools(mcp: FastMCP) -> None:
    """
    Register all reporting tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Outreach Stats",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def get_outreach_stats(
        ctx: Context,
        period: Literal["day", "week", "month"] = "day",
    ) -> dict[str, Any]:
        """
        Get outreach statistics for a time period.

        Args:
            ctx: FastMCP context
            period: Time period - "day" (today), "week" (last 7 days), or "month" (last 30 days)

        Returns:
            Dictionary containing:
            - period: The time period
            - start_date: Start of the period
            - end_date: End of the period
            - connection_requests: Total connection requests
            - follows: Total company follows
            - successful_connections: Successful connection requests
            - successful_follows: Successful follows
            - failed_actions: Failed actions
            - daily_breakdown: Day-by-day stats (for week/month)
        """
        try:
            action_repo = ActionRepository()

            if period == "day":
                stats = await action_repo.get_today_stats()
                return {
                    "period": "day",
                    "date": stats.date,
                    "connection_requests": stats.connection_requests,
                    "follows": stats.follows,
                    "messages": stats.messages,
                    "successful_connections": stats.successful_connections,
                    "successful_follows": stats.successful_follows,
                    "failed_actions": stats.failed_actions,
                    "success_rate": {
                        "connections": (
                            f"{stats.successful_connections}/{stats.connection_requests}"
                            if stats.connection_requests > 0
                            else "N/A"
                        ),
                        "follows": (
                            f"{stats.successful_follows}/{stats.follows}"
                            if stats.follows > 0
                            else "N/A"
                        ),
                    },
                }
            elif period == "week":
                return await action_repo.get_weekly_stats()
            else:  # month
                return await action_repo.get_monthly_stats()

        except Exception as e:
            return handle_tool_error(e, "get_outreach_stats")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Action History",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def get_action_history(
        ctx: Context,
        action_type: Literal[
            "connection_request", "follow_company", "message_sent", "all"
        ] = "all",
        status: Literal[
            "pending", "success", "failed", "rate_limited", "skipped", "all"
        ] = "all",
        days: int = 7,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Get history of outreach actions.

        Args:
            ctx: FastMCP context
            action_type: Filter by action type or "all"
            status: Filter by status or "all"
            days: Number of days to look back (default: 7)
            limit: Maximum number of results (default: 50, max: 200)

        Returns:
            Dictionary containing:
            - filters: The filters applied
            - count: Number of actions returned
            - actions: List of action details
        """
        try:
            action_repo = ActionRepository()

            # Parse filters
            type_filter = ActionType(action_type) if action_type != "all" else None
            status_filter = ActionStatus(status) if status != "all" else None
            since = datetime.now() - timedelta(days=days)

            # Clamp limit
            limit = min(max(1, limit), 200)

            # Get actions
            actions = await action_repo.get_actions(
                action_type=type_filter,
                status=status_filter,
                since=since,
                limit=limit,
            )

            return {
                "filters": {
                    "action_type": action_type,
                    "status": status,
                    "days": days,
                },
                "count": len(actions),
                "actions": [a.to_dict() for a in actions],
            }

        except Exception as e:
            return handle_tool_error(e, "get_action_history")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Generate Connection Message",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def generate_connection_message(
        ctx: Context,
        name: str,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        reason: str | None = None,
        sender_role: str | None = None,
        sender_company: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate a personalized connection request message.

        Uses AI to create a message tailored to the person's profile.
        Messages are kept under LinkedIn's 300 character limit.

        Args:
            ctx: FastMCP context
            name: The person's full name
            title: Their job title (improves personalization)
            company: Their company (improves personalization)
            location: Their location
            reason: Why you want to connect (e.g., networking, job_seeking, recruiting)
            sender_role: Your job title (for context)
            sender_company: Your company (for context)

        Returns:
            Dictionary containing:
            - message: The generated message (max 300 chars)
            - personalization_score: How personalized (0-100)
            - template_used: Which template style was used
        """
        try:
            generator = get_message_generator()

            if reason:
                # Use reason-based generation
                result = generator.generate_from_reason(
                    name=name,
                    reason=reason,
                    field=title or "your field",
                    industry=company or "your industry",
                    company=company or "",
                    role=title or "",
                )
            else:
                # Use full profile-based generation
                result = generator.generate(
                    name=name,
                    title=title,
                    company=company,
                    location=location,
                    sender_role=sender_role,
                    sender_company=sender_company,
                )

            return result

        except Exception as e:
            return handle_tool_error(e, "generate_connection_message")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Connection Message Templates",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def get_message_templates(ctx: Context) -> dict[str, Any]:
        """
        Get available connection message templates and reasons.

        Returns a list of connection reasons that can be used with
        generate_connection_message for more targeted messages.

        Returns:
            Dictionary containing:
            - reasons: List of available connection reasons with descriptions
            - example_messages: Sample messages for each reason
        """
        try:
            from linkedin_mcp_server.ai import REASON_TEMPLATES

            reasons = []
            for key, template in REASON_TEMPLATES.items():
                reasons.append(
                    {
                        "reason": key,
                        "template": template,
                        "description": _get_reason_description(key),
                    }
                )

            # Generate example messages
            generator = get_message_generator()
            examples = {}
            for reason in ["networking", "job_seeking", "recruiting", "content"]:
                result = generator.generate_from_reason(
                    name="Alex",
                    reason=reason,
                    field="technology",
                    industry="tech",
                    company="TechCorp",
                    role="Engineer",
                )
                examples[reason] = result["message"]

            return {
                "reasons": reasons,
                "example_messages": examples,
            }

        except Exception as e:
            return handle_tool_error(e, "get_message_templates")


def _get_reason_description(reason: str) -> str:
    """Get a human-readable description for a connection reason."""
    descriptions = {
        "networking": "General professional networking",
        "job_seeking": "You're looking for job opportunities",
        "recruiting": "You're recruiting for positions",
        "learning": "You want to learn from their expertise",
        "collaboration": "You see potential collaboration opportunities",
        "mutual_connection": "You share mutual connections",
        "same_company": "You work at the same company",
        "same_school": "You attended the same school",
        "event": "You met at an event",
        "content": "You appreciate their LinkedIn content",
    }
    return descriptions.get(reason, "General connection")
