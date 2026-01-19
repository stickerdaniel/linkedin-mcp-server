"""
LinkedIn company profile scraping tools.

Provides MCP tools for extracting company information from LinkedIn
with comprehensive error handling.
"""

import logging
from typing import Any, Dict

from fastmcp import Context, FastMCP
from linkedin_scraper import CompanyPostsScraper, CompanyScraper
from mcp.types import ToolAnnotations

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """
    Register all company-related tools with the MCP server.

    Args:
        mcp: The MCP server instance
    """

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Company Profile",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_company_profile(company_name: str, ctx: Context) -> Dict[str, Any]:
        """
        Get a specific company's LinkedIn profile.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            ctx: FastMCP context for progress reporting

        Returns:
            Structured data from the company's profile including:
            - linkedin_url, name, about_us, website, phone
            - headquarters, founded, industry, company_type, company_size
            - specialties, headcount
            - showcase_pages: List of showcase pages (linkedin_url, name, followers)
            - affiliated_companies: List of affiliated companies
            - employees: List of employees (name, designation, linkedin_url)
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            # Construct LinkedIn URL from company name
            linkedin_url = f"https://www.linkedin.com/company/{company_name}/"

            logger.info(f"Scraping company: {linkedin_url}")

            browser = await get_or_create_browser()
            scraper = CompanyScraper(
                browser.page, callback=MCPContextProgressCallback(ctx)
            )
            company = await scraper.scrape(linkedin_url)

            return company.to_dict()

        except Exception as e:
            return handle_tool_error(e, "get_company_profile")

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Company Posts",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_company_posts(
        company_name: str, ctx: Context, limit: int = 10
    ) -> Dict[str, Any]:
        """
        Get recent posts from a company's LinkedIn feed.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            ctx: FastMCP context for progress reporting
            limit: Maximum number of posts to return (default: 10)

        Returns:
            Dict with posts list containing:
            - linkedin_url, urn, text, posted_date
            - reactions_count, comments_count, reposts_count
            - image_urls: List of image URLs
            - video_url: Video URL if present
            - article_url: Article URL if present
        """
        try:
            # Validate session before scraping
            await ensure_authenticated()

            # Construct LinkedIn URL from company name
            linkedin_url = f"https://www.linkedin.com/company/{company_name}/"

            logger.info(f"Scraping company posts: {linkedin_url} (limit: {limit})")

            browser = await get_or_create_browser()
            scraper = CompanyPostsScraper(
                browser.page, callback=MCPContextProgressCallback(ctx)
            )
            posts = await scraper.scrape(linkedin_url, limit=limit)

            return {"posts": [post.to_dict() for post in posts], "count": len(posts)}

        except Exception as e:
            return handle_tool_error(e, "get_company_posts")
