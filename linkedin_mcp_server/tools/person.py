"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor, parse_person_sections

logger = logging.getLogger(__name__)


def register_person_tools(mcp: FastMCP) -> None:
    """Register all person-related tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Person Profile",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
    ) -> dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, contact_info, posts
                Examples: "experience,education", "contact_info", "honors,languages", "posts"
                Default (None) scrapes only the main profile page.

        Returns:
            Dict with url and sections (name -> raw text).
            Sections may be absent if extraction yielded no content for that page.
            Includes unknown_sections list when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        try:
            await ensure_authenticated()

            requested, unknown = parse_person_sections(sections)

            logger.info(
                "Scraping profile: %s (sections=%s)",
                linkedin_username,
                sections,
            )

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting person profile scrape"
            )

            result = await extractor.scrape_person(linkedin_username, requested)

            if unknown:
                result["unknown_sections"] = unknown

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "get_person_profile")  # NoReturn

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search People",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def search_people(
        keywords: str,
        ctx: Context,
        location: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "software engineer", "recruiter at Google")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "New York", "Remote")

        Returns:
            Dict with url and sections (name -> raw text).
            The LLM should parse the raw text to extract individual people and their profiles.
        """
        try:
            await ensure_authenticated()

            logger.info(
                "Searching people: keywords='%s', location='%s'",
                keywords,
                location,
            )

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            result = await extractor.search_people(keywords, location)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn
