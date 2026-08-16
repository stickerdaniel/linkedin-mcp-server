"""
LinkedIn company profile scraping tools.

Uses innerText extraction for resilient company data capture
with configurable section selection.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import parse_company_sections
from linkedin_mcp_server.scraping.extractor import (
    _RATE_LIMITED_MSG,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.link_metadata import Reference

logger = logging.getLogger(__name__)


def register_company_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all company-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_profile(
        company_name: str,
        ctx: Context,
        sections: str | None = None,
        extractor: Any | None = None,
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

            When the about section is included, references["about"] may
            include a {kind: "company_urn", value: "<numeric-id>"} entry —
            present whenever the page exposes the "See all employees" link
            (typically all but the smallest companies). The value is the
            numeric id LinkedIn's people-search uses in its currentCompany
            URL facet; plain-text company names are silently ignored by
            that facet.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_profile"
            )
            requested, unknown = parse_company_sections(sections)

            logger.info(
                "Scraping company: %s (sections=%s)",
                company_name,
                sections,
            )

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.scrape_company(
                company_name, requested, callbacks=cb
            )

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_profile")
        except Exception as e:
            raise_tool_error(e, "get_company_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Posts",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_posts(
        company_name: str,
        ctx: Context,
        extractor: Any | None = None,
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
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_posts"
            )
            logger.info("Scraping company posts: %s", company_name)

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
                section_errors["posts"] = rate_limited_section_error()
            elif extracted.error:
                section_errors["posts"] = extracted.error

            await ctx.report_progress(progress=100, total=100, message="Complete")

            result: dict[str, Any] = {
                "url": url,
                "sections": sections,
            }
            if references:
                result["references"] = references
            if section_errors:
                result["section_errors"] = section_errors
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_posts")
        except Exception as e:
            raise_tool_error(e, "get_company_posts")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search Companies",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "search"},
        exclude_args=["extractor"],
    )
    async def search_companies(
        keywords: str,
        ctx: Context,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 1,
        start_page: Annotated[int, Field(ge=1, le=100)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for companies on LinkedIn.

        To collect more companies than one call returns, loop: call with
        start_page=1; if result_counts.stopped_by == "max_pages" there is more,
        so call again with the same keywords and start_page increased by
        max_pages (1 -> 4 -> 7 with max_pages=3); stop when stopped_by ==
        "linkedin_end_of_list". Each call is independent — nothing is
        remembered between them — and the page ranges abut exactly, so no
        company is fetched twice.

        Args:
            keywords: Search keywords (e.g., "fintech", "anthropic", "electric vehicles")
            ctx: FastMCP context for progress reporting
            max_pages: How many result pages to load, 10 companies each
                (1-10, default 1). Each page is a separate navigation, so depth
                costs time roughly linearly: about four seconds per page. The
                walk stops as soon as a page returns no new company, so asking
                for 10 pages of a narrow query costs only the pages that exist.
            start_page: Which results page to begin at (1-100, default 1). Use
                it to continue a previous call instead of re-fetching what you
                already have: start_page=1 with max_pages=3 covers pages 1-3,
                start_page=4 covers 4-6. Costs nothing extra — a deep start is
                one navigation like any other.

        Returns:
            Dict with url, sections (search_results -> raw text), optional
            references, and result_counts {rows_seen, returned, stopped_by} —
            stopped_by is "max_pages" when the depth budget ran out (there is
            more; call again with start_page += max_pages, or raise max_pages),
            "linkedin_end_of_list" when LinkedIn had nothing further (stop
            looping — a further start_page returns nothing), or "error" when a
            page failed (see section_errors; retry the same start_page).
            The LLM should parse the raw text to extract individual companies and their pages.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_companies"
            )
            logger.info(
                "Searching companies: keywords='%s', max_pages=%d, start_page=%d",
                keywords,
                max_pages,
                start_page,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting company search"
            )

            result = await extractor.search_companies(
                keywords, max_pages=max_pages, start_page=start_page
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_companies")
        except Exception as e:
            raise_tool_error(e, "search_companies")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Employees",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_company_employees(
        company_name: str,
        ctx: Context,
        keywords: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List employees at a company from the LinkedIn /people/ page, including
        the demographics aggregate that this view exposes: where employees
        live, where they studied, and a function breakdown (Engineering, Sales,
        Operations, etc.). The demographics are unique to this tool.

        For filtered search by network degree (1st/2nd/3rd) or location, prefer
        search_people with current_company set to the company URN id. That path
        also returns more result pages than the /people/ tab.

        The optional keywords filter narrows results by name, title, or skill.

        company_name must be the exact LinkedIn URL slug (the path segment after
        /company/), not the display name. LinkedIn assigns unique slugs and the
        display name often does not match. For example, the AI lab Anthropic
        lives at /company/anthropicresearch/, not /company/anthropic/. If you
        are unsure of the slug, call search_companies first and pick the slug
        from the returned references.

        Args:
            company_name: LinkedIn company URL slug (e.g., "docker", "anthropicresearch", "microsoft")
            ctx: FastMCP context for progress reporting
            keywords: Optional filter by name, job title, or skill (e.g., "engineer", "sales")

        Returns:
            Dict with url, sections (employees -> raw text), and optional references.
            References include /in/ profile paths for listed employees.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_company_employees"
            )
            logger.info(
                "Scraping company employees: %s (keywords=%s)", company_name, keywords
            )

            await ctx.report_progress(
                progress=0, total=100, message="Loading company employees"
            )

            result = await extractor.get_company_employees(
                company_name, keywords=keywords
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_company_employees")
        except Exception as e:
            raise_tool_error(e, "get_company_employees")  # NoReturn
