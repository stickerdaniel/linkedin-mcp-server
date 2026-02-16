"""
LinkedIn company profile scraping tools.

Uses innerText extraction for resilient company data capture
with configurable section selection.
"""

import logging
from typing import Any, Dict

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor, parse_company_sections

logger = logging.getLogger(__name__)


def register_company_tools(mcp: FastMCP) -> None:
    """Register all company-related tools with the MCP server."""

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Company Profile",
            readOnlyHint=True,
            destructiveHint=False,
            openWorldHint=True,
        )
    )
    async def get_company_profile(
        company_name: str,
        ctx: Context,
        sections: str | None = None,
    ) -> Dict[str, Any]:
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
            Dict with url, sections (name -> raw text), pages_visited, and sections_requested.
            The LLM should parse the raw text in each section.
        """
        try:
            await ensure_authenticated()

            fields = parse_company_sections(sections)

            logger.info(
                "Scraping company: %s (sections=%s)",
                company_name,
                sections,
            )

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting company profile scrape"
            )

            result = await extractor.scrape_company(company_name, fields)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

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
        company_name: str,
        ctx: Context,
    ) -> Dict[str, Any]:
        """
        Get recent posts from a company's LinkedIn feed.

        Args:
            company_name: LinkedIn company name (e.g., "docker", "anthropic", "microsoft")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (name -> raw text), pages_visited, and sections_requested.
            The LLM should parse the raw text to extract individual posts.
        """
        try:
            await ensure_authenticated()

            logger.info("Scraping company posts: %s", company_name)

            browser = await get_or_create_browser()
            extractor = LinkedInExtractor(browser.page)

            await ctx.report_progress(
                progress=0, total=100, message="Starting company posts scrape"
            )

            url = f"https://www.linkedin.com/company/{company_name}/posts/"
            text = await extractor.extract_page(url)

            sections: dict[str, str] = {}
            if text:
                sections["posts"] = text

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return {
                "url": url,
                "sections": sections,
                "pages_visited": [url],
                "sections_requested": ["posts"],
            }

        except Exception as e:
            return handle_tool_error(e, "get_company_posts")
