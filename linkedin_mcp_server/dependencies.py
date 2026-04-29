"""Helpers used by MCP tools after bootstrap gating."""

from fastmcp import Context
from fastmcp.dependencies import CurrentContext

from linkedin_mcp_server.bootstrap import ensure_tool_ready_or_raise
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor

_CURRENT_CONTEXT = CurrentContext()


async def get_ready_extractor(
    ctx: Context | None,
    *,
    tool_name: str,
) -> LinkedInExtractor:
    """Run bootstrap gating, then acquire an authenticated extractor."""
    try:
        await ensure_tool_ready_or_raise(tool_name, ctx)
        browser = await get_or_create_browser()
        await ensure_authenticated()
        return LinkedInExtractor(browser.page)
    except Exception as e:
        raise_tool_error(e, tool_name)  # NoReturn


async def get_person_profile_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for get_person_profile."""
    return await get_ready_extractor(ctx, tool_name="get_person_profile")


async def get_search_people_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for search_people."""
    return await get_ready_extractor(ctx, tool_name="search_people")


async def get_company_profile_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for get_company_profile."""
    return await get_ready_extractor(ctx, tool_name="get_company_profile")


async def get_company_posts_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for get_company_posts."""
    return await get_ready_extractor(ctx, tool_name="get_company_posts")


async def get_job_details_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for get_job_details."""
    return await get_ready_extractor(ctx, tool_name="get_job_details")


async def get_search_jobs_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for search_jobs."""
    return await get_ready_extractor(ctx, tool_name="search_jobs")


async def get_linkedin_page_extractor(
    ctx: Context = _CURRENT_CONTEXT,
) -> LinkedInExtractor:
    """Resolve the extractor dependency for get_linkedin_page."""
    return await get_ready_extractor(ctx, tool_name="get_linkedin_page")
