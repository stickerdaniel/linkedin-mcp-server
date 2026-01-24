"""
LinkedIn outreach tools for connection requests and follows.

Provides MCP tools for automating outreach actions with safety controls
including rate limiting and action tracking.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.automation import (
    CompanyFollowAutomation,
    ConnectionRequestAutomation,
)
from linkedin_mcp_server.drivers.browser import ensure_authenticated
from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.safety import (
    OutreachPausedError,
    RateLimitExceededError,
    get_rate_limiter,
)
from linkedin_mcp_server.storage import (
    ActionRepository,
    ActionStatus,
    ActionType,
    OutreachAction,
    OutreachStateRepository,
)

logger = logging.getLogger(__name__)


def register_outreach_tools(mcp: FastMCP) -> None:
    """
    Register all outreach-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Send Connection Request",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def send_connection_request(
        ctx: Context,
        profile_url: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """
        Send a connection request to a LinkedIn profile.

        This action is rate-limited to protect your account. Daily limits
        are enforced and delays are applied between actions.

        Args:
            ctx: FastMCP context for progress reporting
            profile_url: Full LinkedIn profile URL (e.g., https://www.linkedin.com/in/username/)
            message: Optional personalized message (max 300 characters)

        Returns:
            Dictionary containing:
            - status: success, already_connected, already_pending, rate_limited, failed
            - profile_url: The profile URL
            - profile_name: Name of the person (if successful)
            - message: Description of the result
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            # Check rate limits
            rate_limiter = get_rate_limiter()
            await ctx.report_progress(10, 100, "Checking rate limits...")

            try:
                await rate_limiter.check_limit(ActionType.CONNECTION_REQUEST)
            except OutreachPausedError:
                return {
                    "status": "paused",
                    "message": "Outreach is paused. Use resume_outreach to continue.",
                }
            except RateLimitExceededError as e:
                return {
                    "status": "rate_limit_exceeded",
                    "message": str(e),
                    "limit": e.limit,
                    "current": e.current,
                }

            # Check for duplicate action
            action_repo = ActionRepository()
            existing = await action_repo.get_action_by_target_url(
                profile_url, ActionType.CONNECTION_REQUEST
            )
            if existing and existing.status == ActionStatus.SUCCESS:
                return {
                    "status": "already_sent",
                    "profile_url": profile_url,
                    "message": "Connection request already sent to this profile",
                }

            # Create action record
            action = OutreachAction(
                action_type=ActionType.CONNECTION_REQUEST,
                target_url=profile_url,
                message=message,
                status=ActionStatus.PENDING,
            )
            action = await action_repo.create_action(action)

            # Execute the connection request
            await ctx.report_progress(30, 100, "Sending connection request...")
            automation = ConnectionRequestAutomation()
            result = await automation.execute(profile_url=profile_url, message=message)

            # Update action status
            if result["status"] == "success":
                await action_repo.update_action_status(action.id, ActionStatus.SUCCESS)
                await rate_limiter.record_action(ActionType.CONNECTION_REQUEST, True)
                rate_limiter.reset_backoff()
            elif result["status"] == "rate_limited":
                await action_repo.update_action_status(
                    action.id, ActionStatus.RATE_LIMITED, result["message"]
                )
                await rate_limiter.apply_backoff()
            else:
                await action_repo.update_action_status(
                    action.id, ActionStatus.FAILED, result.get("message")
                )
                await rate_limiter.record_action(ActionType.CONNECTION_REQUEST, False)

            await ctx.report_progress(100, 100, "Complete")
            return result

        except Exception as e:
            return handle_tool_error(e, "send_connection_request")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Send Bulk Connection Requests",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def send_bulk_connection_requests(
        ctx: Context,
        profile_urls: list[str],
        message: str | None = None,
        stop_on_limit: bool = True,
    ) -> dict[str, Any]:
        """
        Send connection requests to multiple profiles with safety controls.

        Automatically applies delays between requests and respects daily limits.
        Will pause between batches to avoid detection.

        Args:
            ctx: FastMCP context for progress reporting
            profile_urls: List of LinkedIn profile URLs
            message: Optional personalized message for all requests (max 300 chars)
            stop_on_limit: Stop when daily limit is reached (default: True)

        Returns:
            Dictionary containing:
            - total: Total profiles processed
            - successful: Number of successful requests
            - failed: Number of failed requests
            - skipped: Number skipped (already connected/pending/sent)
            - results: List of individual results
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            rate_limiter = get_rate_limiter()
            action_repo = ActionRepository()

            results = []
            successful = 0
            failed = 0
            skipped = 0

            total = len(profile_urls)

            for i, profile_url in enumerate(profile_urls):
                progress = int((i / total) * 100)
                await ctx.report_progress(
                    progress, 100, f"Processing {i + 1}/{total}..."
                )

                # Check if paused
                try:
                    await rate_limiter.check_limit(ActionType.CONNECTION_REQUEST)
                except OutreachPausedError:
                    results.append(
                        {
                            "profile_url": profile_url,
                            "status": "skipped",
                            "message": "Outreach paused",
                        }
                    )
                    skipped += 1
                    break
                except RateLimitExceededError:
                    if stop_on_limit:
                        results.append(
                            {
                                "profile_url": profile_url,
                                "status": "skipped",
                                "message": "Daily limit reached",
                            }
                        )
                        skipped += 1
                        break
                    else:
                        results.append(
                            {
                                "profile_url": profile_url,
                                "status": "skipped",
                                "message": "Daily limit reached",
                            }
                        )
                        skipped += 1
                        continue

                # Check for duplicate
                existing = await action_repo.get_action_by_target_url(
                    profile_url, ActionType.CONNECTION_REQUEST
                )
                if existing and existing.status == ActionStatus.SUCCESS:
                    results.append(
                        {
                            "profile_url": profile_url,
                            "status": "skipped",
                            "message": "Already sent",
                        }
                    )
                    skipped += 1
                    continue

                # Create action record
                action = OutreachAction(
                    action_type=ActionType.CONNECTION_REQUEST,
                    target_url=profile_url,
                    message=message,
                    status=ActionStatus.PENDING,
                )
                action = await action_repo.create_action(action)

                # Execute connection request
                automation = ConnectionRequestAutomation()
                result = await automation.execute(
                    profile_url=profile_url, message=message
                )

                # Update status
                if result["status"] == "success":
                    await action_repo.update_action_status(
                        action.id, ActionStatus.SUCCESS
                    )
                    await rate_limiter.record_action(
                        ActionType.CONNECTION_REQUEST, True
                    )
                    successful += 1
                elif result["status"] in ("already_connected", "already_pending"):
                    await action_repo.update_action_status(
                        action.id, ActionStatus.SKIPPED, result["message"]
                    )
                    skipped += 1
                elif result["status"] == "rate_limited":
                    await action_repo.update_action_status(
                        action.id, ActionStatus.RATE_LIMITED, result["message"]
                    )
                    await rate_limiter.apply_backoff()
                    if stop_on_limit:
                        break
                else:
                    await action_repo.update_action_status(
                        action.id, ActionStatus.FAILED, result.get("message")
                    )
                    await rate_limiter.record_action(
                        ActionType.CONNECTION_REQUEST, False
                    )
                    failed += 1

                results.append(result)

                # Apply delay between actions
                if i < total - 1:
                    await rate_limiter.wait_between_actions()
                    await rate_limiter.wait_for_batch_pause()

            await ctx.report_progress(100, 100, "Complete")

            return {
                "total": total,
                "successful": successful,
                "failed": failed,
                "skipped": skipped,
                "results": results,
            }

        except Exception as e:
            return handle_tool_error(e, "send_bulk_connection_requests")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Follow Company",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def follow_company(
        ctx: Context,
        company_url: str,
    ) -> dict[str, Any]:
        """
        Follow a LinkedIn company.

        Args:
            ctx: FastMCP context for progress reporting
            company_url: Full LinkedIn company URL (e.g., https://www.linkedin.com/company/name/)

        Returns:
            Dictionary containing:
            - status: success, already_following, rate_limited, failed
            - company_url: The company URL
            - company_name: Name of the company (if successful)
            - message: Description of the result
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            rate_limiter = get_rate_limiter()
            await ctx.report_progress(10, 100, "Checking rate limits...")

            try:
                await rate_limiter.check_limit(ActionType.FOLLOW_COMPANY)
            except OutreachPausedError:
                return {
                    "status": "paused",
                    "message": "Outreach is paused. Use resume_outreach to continue.",
                }
            except RateLimitExceededError as e:
                return {
                    "status": "rate_limit_exceeded",
                    "message": str(e),
                }

            # Check for duplicate
            action_repo = ActionRepository()
            existing = await action_repo.get_action_by_target_url(
                company_url, ActionType.FOLLOW_COMPANY
            )
            if existing and existing.status == ActionStatus.SUCCESS:
                return {
                    "status": "already_followed",
                    "company_url": company_url,
                    "message": "Already following this company",
                }

            # Create action record
            action = OutreachAction(
                action_type=ActionType.FOLLOW_COMPANY,
                target_url=company_url,
                status=ActionStatus.PENDING,
            )
            action = await action_repo.create_action(action)

            # Execute follow
            await ctx.report_progress(30, 100, "Following company...")
            automation = CompanyFollowAutomation()
            result = await automation.execute(company_url=company_url)

            # Update action status
            if result["status"] == "success":
                await action_repo.update_action_status(
                    action.id,
                    ActionStatus.SUCCESS,
                )
                action.target_name = result.get("company_name")
                await rate_limiter.record_action(ActionType.FOLLOW_COMPANY, True)
            elif result["status"] == "already_following":
                await action_repo.update_action_status(
                    action.id, ActionStatus.SKIPPED, result["message"]
                )
            else:
                await action_repo.update_action_status(
                    action.id, ActionStatus.FAILED, result.get("message")
                )
                await rate_limiter.record_action(ActionType.FOLLOW_COMPANY, False)

            await ctx.report_progress(100, 100, "Complete")
            return result

        except Exception as e:
            return handle_tool_error(e, "follow_company")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Follow Bulk Companies",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def follow_bulk_companies(
        ctx: Context,
        company_urls: list[str],
        stop_on_limit: bool = True,
    ) -> dict[str, Any]:
        """
        Follow multiple LinkedIn companies with safety controls.

        Automatically applies delays between follows and respects daily limits.

        Args:
            ctx: FastMCP context for progress reporting
            company_urls: List of LinkedIn company URLs
            stop_on_limit: Stop when daily limit is reached (default: True)

        Returns:
            Dictionary containing:
            - total: Total companies processed
            - successful: Number of successful follows
            - failed: Number of failed follows
            - skipped: Number skipped (already following)
            - results: List of individual results
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            rate_limiter = get_rate_limiter()
            action_repo = ActionRepository()

            results = []
            successful = 0
            failed = 0
            skipped = 0

            total = len(company_urls)

            for i, company_url in enumerate(company_urls):
                progress = int((i / total) * 100)
                await ctx.report_progress(
                    progress, 100, f"Processing {i + 1}/{total}..."
                )

                # Check rate limits
                try:
                    await rate_limiter.check_limit(ActionType.FOLLOW_COMPANY)
                except OutreachPausedError:
                    results.append(
                        {
                            "company_url": company_url,
                            "status": "skipped",
                            "message": "Outreach paused",
                        }
                    )
                    skipped += 1
                    break
                except RateLimitExceededError:
                    if stop_on_limit:
                        break
                    skipped += 1
                    continue

                # Check for duplicate
                existing = await action_repo.get_action_by_target_url(
                    company_url, ActionType.FOLLOW_COMPANY
                )
                if existing and existing.status == ActionStatus.SUCCESS:
                    results.append(
                        {
                            "company_url": company_url,
                            "status": "skipped",
                            "message": "Already followed",
                        }
                    )
                    skipped += 1
                    continue

                # Create action record
                action = OutreachAction(
                    action_type=ActionType.FOLLOW_COMPANY,
                    target_url=company_url,
                    status=ActionStatus.PENDING,
                )
                action = await action_repo.create_action(action)

                # Execute follow
                automation = CompanyFollowAutomation()
                result = await automation.execute(company_url=company_url)

                # Update status
                if result["status"] == "success":
                    await action_repo.update_action_status(
                        action.id, ActionStatus.SUCCESS
                    )
                    await rate_limiter.record_action(ActionType.FOLLOW_COMPANY, True)
                    successful += 1
                elif result["status"] == "already_following":
                    await action_repo.update_action_status(
                        action.id, ActionStatus.SKIPPED, result["message"]
                    )
                    skipped += 1
                else:
                    await action_repo.update_action_status(
                        action.id, ActionStatus.FAILED, result.get("message")
                    )
                    await rate_limiter.record_action(ActionType.FOLLOW_COMPANY, False)
                    failed += 1

                results.append(result)

                # Apply delay
                if i < total - 1:
                    await rate_limiter.wait_between_actions()
                    await rate_limiter.wait_for_batch_pause()

            await ctx.report_progress(100, 100, "Complete")

            return {
                "total": total,
                "successful": successful,
                "failed": failed,
                "skipped": skipped,
                "results": results,
            }

        except Exception as e:
            return handle_tool_error(e, "follow_bulk_companies")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Pause Outreach",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def pause_outreach(ctx: Context) -> dict[str, Any]:
        """
        Pause all outreach automation.

        When paused, connection requests and follows will be blocked
        until resume_outreach is called.

        Returns:
            Dictionary with status confirmation
        """
        try:
            state_repo = OutreachStateRepository()
            await state_repo.set_paused(True)

            rate_limiter = get_rate_limiter()
            await rate_limiter.pause()

            return {
                "status": "success",
                "message": "Outreach automation paused",
                "paused": True,
            }
        except Exception as e:
            return handle_tool_error(e, "pause_outreach")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Resume Outreach",
            readOnlyHint=False,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def resume_outreach(ctx: Context) -> dict[str, Any]:
        """
        Resume outreach automation after being paused.

        Returns:
            Dictionary with status confirmation
        """
        try:
            state_repo = OutreachStateRepository()
            await state_repo.set_paused(False)

            rate_limiter = get_rate_limiter()
            await rate_limiter.resume()

            return {
                "status": "success",
                "message": "Outreach automation resumed",
                "paused": False,
            }
        except Exception as e:
            return handle_tool_error(e, "resume_outreach")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Rate Limit Status",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=False,
        )
    )
    async def get_rate_limit_status(ctx: Context) -> dict[str, Any]:
        """
        Get current rate limit status and remaining actions.

        Returns:
            Dictionary containing:
            - paused: Whether outreach is paused
            - date: Current date
            - connection_requests: Used/limit/remaining for connections
            - follows: Used/limit/remaining for follows
            - messages: Used/limit/remaining for messages
            - success_rate: Success rates for today
        """
        try:
            rate_limiter = get_rate_limiter()
            return await rate_limiter.get_status()
        except Exception as e:
            return handle_tool_error(e, "get_rate_limit_status")
