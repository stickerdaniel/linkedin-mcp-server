"""
LinkedIn company profile scraping tools.

Uses innerText extraction for resilient company data capture
with configurable section selection.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor, parse_company_sections
from linkedin_mcp_server.scraping.extractor import (
    _RATE_LIMITED_ERROR,
    _RATE_LIMITED_MSG,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.sqlite_cache import sqlite_cache
from linkedin_mcp_server.serialization import strip_none

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """Register all company-related tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Company Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
    )
    async def get_company_profile(
        company_name: str,
        ctx: Context,
        sections: str | None = None,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get a specific company's LinkedIn profile.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The about page is always included.
                Available sections: posts, jobs
                Examples: "posts", "posts,jobs"
                Default (None) scrapes only the about page.

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            Includes unknown_sections list when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        try:
            requested, unknown = parse_company_sections(sections)

            logger.info(
                "Scraping company: %s (sections=%s)",
                company_name,
                sections,
            )

            _cache_args = {"company_name": company_name, "sections": sections}
            _cached = sqlite_cache.get_tool("get_company_profile", _cache_args)
            if _cached is not None:
                await ctx.report_progress(progress=100, total=100, message="Complete (cached)")
                return _cached

            await ctx.report_progress(
                progress=0, total=100, message="Starting company profile scrape"
            )

            result = await extractor.scrape_company(company_name, requested)

            if unknown:
                result["unknown_sections"] = unknown

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result = strip_none(result)
            if not result.get("section_errors"):
                sqlite_cache.set_tool("get_company_profile", _cache_args, result, ttl=604800)
            return result

        except Exception as e:
            raise_tool_error(e, "get_company_profile")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get Company Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
    )
    async def get_company_posts(
        company_name: str,
        ctx: Context,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Get recent posts from a company's LinkedIn feed.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            The LLM should parse the raw text to extract individual posts.
        """
        try:
            logger.info("Scraping company posts: %s", company_name)

            _cache_args = {"company_name": company_name}
            _cached = sqlite_cache.get_tool("get_company_posts", _cache_args)
            if _cached is not None:
                await ctx.report_progress(progress=100, total=100, message="Complete (cached)")
                return _cached

            await ctx.report_progress(
                progress=0, total=100, message="Starting company posts scrape"
            )

            url = f"https://www.linkedin.com/company/{company_name}/posts/"
            extracted = await extractor.extract_page(url, section_name="posts")

            sections: dict[str, str] = {}
            references: dict[str, list[Reference]] = {}
            section_errors: dict[str, dict[str, Any]] = {}
            if extracted.text and extracted.text != _RATE_LIMITED_MSG:
                sections["posts"] = extracted.text
                if extracted.references:
                    references["posts"] = extracted.references
            elif extracted.text == _RATE_LIMITED_MSG:
                section_errors["posts"] = _RATE_LIMITED_ERROR
            elif extracted.error:
                section_errors["posts"] = extracted.error

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result = {
                "url": url,
                "sections": sections,
            }
            if references:
                result["references"] = references
            if section_errors:
                result["section_errors"] = section_errors
            result = strip_none(result)
            if not result.get("section_errors"):
                sqlite_cache.set_tool("get_company_posts", _cache_args, result, ttl=21600)
            return result

        except Exception as e:
            raise_tool_error(e, "get_company_posts")  # NoReturn
