"""
LinkedIn account tools for the authenticated user's own profile and applications.

Provides tools to view own profile, check application status, and manage saved jobs.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import parse_person_sections
from linkedin_mcp_server.scraping.fields import ALL_PERSON_SECTION_NAMES

logger = logging.getLogger(__name__)


def register_account_tools(mcp: FastMCP) -> None:
    """Register all account-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get My Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"account", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_profile(
        ctx: Context,
        sections: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the logged-in user's own LinkedIn profile.

        Navigates to /in/me/ which redirects to your profile, then scrapes it.

        Args:
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, skills, certifications,
                    volunteer, projects, publications, courses, recommendations,
                    organizations, interests, honors, languages, contact_info, posts
                Examples: "experience,education,skills", "contact_info"
                Default (None) scrapes only the main profile page.
                Use get_my_profile_full to scrape ALL sections at once.

        Returns:
            Dict with url, sections (name -> raw text), my_username, and optional references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_profile"
            )
            requested, unknown = parse_person_sections(sections)

            logger.info("Scraping own profile (sections=%s)", sections)

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.get_my_profile(requested, callbacks=cb)

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_profile")
        except Exception as e:
            raise_tool_error(e, "get_my_profile")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS * 3,
        title="Get My Full Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"account", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_profile_full(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the logged-in user's COMPLETE LinkedIn profile with ALL sections.

        Scrapes every available section: experience, education, skills,
        certifications, volunteer, projects, publications, courses,
        recommendations, organizations, interests, honors, languages,
        contact_info, and posts.

        This is slower than get_my_profile since it navigates to many pages.
        Use get_my_profile with specific sections if you only need a subset.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (name -> raw text), my_username, and optional references.
            Sections that have no content on your profile will be absent from the result.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_profile_full"
            )

            logger.info("Scraping own full profile (all sections)")

            cb = MCPContextProgressCallback(ctx)
            all_sections = set(ALL_PERSON_SECTION_NAMES)
            result = await extractor.get_my_profile(all_sections, callbacks=cb)

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_profile_full")
        except Exception as e:
            raise_tool_error(e, "get_my_profile_full")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Saved Jobs",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"account", "job"},
        exclude_args=["extractor"],
    )
    async def get_saved_jobs(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the list of jobs you have saved/bookmarked on LinkedIn.

        Returns:
            Dict with url, sections (saved_jobs -> raw text), and job_ids list.
            Use get_job_details with any job_id for full information.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_saved_jobs"
            )
            logger.info("Fetching saved jobs")

            await ctx.report_progress(
                progress=0, total=100, message="Fetching saved jobs"
            )

            result = await extractor.get_saved_jobs()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_saved_jobs")
        except Exception as e:
            raise_tool_error(e, "get_saved_jobs")

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get My Applications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"account", "job"},
        exclude_args=["extractor"],
    )
    async def get_my_applications(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the list of jobs you have applied to on LinkedIn.

        Returns:
            Dict with url, sections (applications -> raw text), and job_ids list.
            Use get_job_details with any job_id for full information.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_applications"
            )
            logger.info("Fetching my applications")

            await ctx.report_progress(
                progress=0, total=100, message="Fetching applications"
            )

            result = await extractor.get_my_applications()

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_applications")
        except Exception as e:
            raise_tool_error(e, "get_my_applications")
