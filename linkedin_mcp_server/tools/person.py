"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor, parse_person_sections
from linkedin_mcp_server.scraping.sqlite_cache import sqlite_cache
from linkedin_mcp_server.serialization import strip_none

logger = logging.getLogger(__name__)


def register_person_tools(mcp: FastMCP) -> None:
    """Register all person-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Person Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
    )
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, contact_info, posts, recommendations, skills, certifications, projects, volunteer, publications
                Examples: "experience,education", "contact_info", "skills,certifications", "recommendations"
                Default (None) scrapes only the main profile page.

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            Sections may be absent if extraction yielded no content for that page.
            Includes unknown_sections list when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        try:
            requested, unknown = parse_person_sections(sections)

            logger.info(
                "Scraping profile: %s (sections=%s)",
                linkedin_username,
                sections,
            )

            _cache_args = {"linkedin_username": linkedin_username, "sections": sections}
            _cached = sqlite_cache.get_tool("get_person_profile", _cache_args)
            if _cached is not None:
                await ctx.report_progress(progress=100, total=100, message="Complete (cached)")
                return _cached

            await ctx.report_progress(
                progress=0, total=100, message="Starting person profile scrape"
            )

            result = await extractor.scrape_person(linkedin_username, requested)

            if unknown:
                result["unknown_sections"] = unknown

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result = strip_none(result)
            sqlite_cache.set_tool("get_person_profile", _cache_args, result, ttl=604800)
            return result

        except Exception as e:
            raise_tool_error(e, "get_person_profile")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Search People",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search"},
    )
    async def search_people(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "software engineer", "recruiter at Google")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "New York", "Remote")

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            The LLM should parse the raw text to extract individual people and their profiles.
        """
        try:
            logger.info(
                "Searching people: keywords='%s', location='%s'",
                keywords,
                location,
            )

            _cache_args = {"keywords": keywords, "location": location}
            _cached = sqlite_cache.get_tool("search_people", _cache_args)
            if _cached is not None:
                await ctx.report_progress(progress=100, total=100, message="Complete (cached)")
                return _cached

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            result = await extractor.search_people(keywords, location)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result = strip_none(result)
            sqlite_cache.set_tool("search_people", _cache_args, result, ttl=14400)
            return result

        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn
