"""
LinkedIn search tools for people and companies.

Provides MCP tools for searching LinkedIn to find potential connections
and companies to follow.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.automation import (
    CompanySearchAutomation,
    PeopleSearchAutomation,
)
from linkedin_mcp_server.drivers.browser import ensure_authenticated
from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.storage import SearchCacheRepository, SearchResult

logger = logging.getLogger(__name__)


def register_search_tools(mcp: FastMCP) -> None:
    """
    Register all search-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search People",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def search_people(
        ctx: Context,
        keywords: str | None = None,
        title: str | None = None,
        company: str | None = None,
        location: str | None = None,
        industry: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn.

        Use this to find potential connections based on various criteria
        like job title, company, location, or industry.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: General search keywords (e.g., "DevOps Engineer")
            title: Job title filter (e.g., "Software Engineer")
            company: Company name filter (e.g., "Google")
            location: Location filter (e.g., "San Francisco")
            industry: Industry filter (e.g., "Technology")
            limit: Maximum number of results (default: 25, max: 100)

        Returns:
            Dictionary containing:
            - query: The search parameters used
            - results: List of people with name, url, headline, location
            - count: Number of results returned
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            # Clamp limit
            limit = min(max(1, limit), 100)

            await ctx.report_progress(10, 100, "Searching for people...")
            automation = PeopleSearchAutomation()
            results = await automation.execute(
                keywords=keywords,
                title=title,
                company=company,
                location=location,
                industry=industry,
                limit=limit,
            )

            # Cache results for future reference
            if results.get("results"):
                await ctx.report_progress(80, 100, "Caching results...")
                cache_repo = SearchCacheRepository()
                query_str = f"{keywords or ''} {title or ''} {company or ''} {location or ''}".strip()

                for result in results["results"]:
                    try:
                        await cache_repo.cache_result(
                            SearchResult(
                                url=result["url"],
                                name=result["name"],
                                title=result.get("headline"),
                                location=result.get("location"),
                                search_query=query_str,
                                result_type="person",
                            )
                        )
                    except Exception as e:
                        logger.debug(f"Failed to cache result: {e}")

            await ctx.report_progress(100, 100, "Search complete")
            return results

        except Exception as e:
            return handle_tool_error(e, "search_people")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search Companies",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def search_companies(
        ctx: Context,
        keywords: str | None = None,
        industry: str | None = None,
        company_size: str | None = None,
        location: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """
        Search for companies on LinkedIn.

        Use this to find companies to follow or research based on
        various criteria like industry, size, or location.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: Search keywords (e.g., "AI startup")
            industry: Industry filter (e.g., "Technology")
            company_size: Size filter (e.g., "1-10", "51-200", "1001-5000")
            location: Location filter (e.g., "San Francisco Bay Area")
            limit: Maximum number of results (default: 25, max: 100)

        Returns:
            Dictionary containing:
            - query: The search parameters used
            - results: List of companies with name, url, industry, location
            - count: Number of results returned
        """
        try:
            await ctx.report_progress(0, 100, "Validating session...")
            await ensure_authenticated()

            # Clamp limit
            limit = min(max(1, limit), 100)

            await ctx.report_progress(10, 100, "Searching for companies...")
            automation = CompanySearchAutomation()
            results = await automation.execute(
                keywords=keywords,
                industry=industry,
                company_size=company_size,
                location=location,
                limit=limit,
            )

            # Cache results for future reference
            if results.get("results"):
                await ctx.report_progress(80, 100, "Caching results...")
                cache_repo = SearchCacheRepository()
                query_str = (
                    f"{keywords or ''} {industry or ''} {location or ''}".strip()
                )

                for result in results["results"]:
                    try:
                        await cache_repo.cache_result(
                            SearchResult(
                                url=result["url"],
                                name=result["name"],
                                title=result.get("headline"),  # industry
                                location=result.get("location"),
                                search_query=query_str,
                                result_type="company",
                            )
                        )
                    except Exception as e:
                        logger.debug(f"Failed to cache result: {e}")

            await ctx.report_progress(100, 100, "Search complete")
            return results

        except Exception as e:
            return handle_tool_error(e, "search_companies")
