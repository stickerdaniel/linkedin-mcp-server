"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import asyncio
import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.dependencies import Depends

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import LinkedInExtractor, parse_person_sections

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
            requested, unknown = parse_person_sections(sections)

            logger.info(
                "Scraping profile: %s (sections=%s)",
                linkedin_username,
                sections,
            )

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
            Dict with url and sections (name -> raw text).
            The LLM should parse the raw text to extract individual people and their profiles.
        """
        try:
            logger.info(
                "Searching people: keywords='%s', location='%s'",
                keywords,
                location,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            result = await extractor.search_people(keywords, location)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS * 3,  # 更长超时，因为需要获取多个档案
        title="Search People with Past Company Filter",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search", "advanced"},
    )
    async def search_people_with_past_company(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        past_companies: str | None = None,
        current_title: str | None = None,
        max_results: int = 10,
        extractor: LinkedInExtractor = Depends(get_extractor),
    ) -> dict[str, Any]:
        """
        Search for people with advanced filtering by past company and current title.

        This tool performs a two-step search:
        1. Search for people using keywords and location
        2. Filter results by checking their experience for past companies and current title

        Args:
            keywords: Search keywords (e.g., "founder", "CEO", "software engineer")
            ctx: FastMCP context for progress reporting
            location: Optional location filter (e.g., "Beijing", "Shanghai", "New York")
            past_companies: Comma-separated list of company names to match in experience
                (e.g., "Alibaba,ByteDance,Tencent", "Google,Meta,Amazon")
            current_title: Optional filter for current job title
                (e.g., "founder", "CEO", "CTO") - case insensitive partial match
            max_results: Maximum number of matching profiles to return (default: 10)
            extractor: LinkedInExtractor instance

        Returns:
            Dict with:
            - search_url: The LinkedIn search URL used
            - total_checked: Number of profiles checked
            - matching_profiles: List of profiles matching all criteria
            - partial_matches: List of profiles matching some criteria
            - filters: The filters applied

        Example:
            keywords="founder", location="Beijing", past_companies="Alibaba,ByteDance", current_title="founder"
            This will find founders in Beijing who previously worked at Alibaba or ByteDance.
        """
        try:
            logger.info(
                "Advanced people search: keywords='%s', location='%s', past_companies='%s', current_title='%s'",
                keywords,
                location,
                past_companies,
                current_title,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting advanced people search"
            )

            # Parse past companies
            past_company_list = []
            if past_companies:
                past_company_list = [c.strip().lower() for c in past_companies.split(",")]

            # Step 1: Search for people
            await ctx.report_progress(
                progress=10, total=100, message="Searching for initial candidates"
            )
            search_result = await extractor.search_people(keywords, location)

            await ctx.report_progress(
                progress=30, total=100, message="Analyzing search results"
            )

            # Extract profile URLs from search results
            search_text = search_result.get("sections", {}).get("search_results", "")
            profile_urls = _extract_profile_urls(search_text)

            logger.info("Found %d profiles in search results", len(profile_urls))

            # Step 2: Check each profile for past company and current title
            matching_profiles = []
            partial_matches = []
            total_checked = 0

            for idx, url in enumerate(profile_urls[:max_results * 3]):  # Check more than needed
                if len(matching_profiles) >= max_results:
                    break

                try:
                    # Extract username from URL
                    username = _extract_username_from_url(url)
                    if not username:
                        continue

                    await ctx.report_progress(
                        progress=30 + int((idx / len(profile_urls)) * 60),
                        total=100,
                        message=f"Checking profile {idx + 1}/{len(profile_urls[:max_results * 3])}: {username}"
                    )

                    # Get detailed profile with experience
                    profile_result = await extractor.scrape_person(
                        username, requested_sections={"experience"}
                    )

                    total_checked += 1

                    # Parse the profile
                    profile_data = _parse_profile_for_filters(
                        profile_result, past_company_list, current_title
                    )

                    if profile_data["matches_all"]:
                        matching_profiles.append(profile_data)
                        logger.info("Found matching profile: %s", username)
                    elif profile_data["matches_partial"]:
                        partial_matches.append(profile_data)

                    # Small delay to avoid rate limiting
                    await asyncio.sleep(1.5)

                except Exception as e:
                    logger.warning("Failed to check profile %s: %s", url, e)
                    continue

            await ctx.report_progress(progress=100, total=100, message="Search complete")

            return {
                "search_url": search_result.get("url"),
                "total_checked": total_checked,
                "filters": {
                    "keywords": keywords,
                    "location": location,
                    "past_companies": past_company_list,
                    "current_title": current_title,
                    "max_results": max_results,
                },
                "matching_profiles": matching_profiles[:max_results],
                "partial_matches": partial_matches[:5],  # Include some partial matches for reference
            }

        except Exception as e:
            raise_tool_error(e, "search_people_with_past_company")  # NoReturn


def _extract_profile_urls(search_text: str) -> list[str]:
    """Extract LinkedIn profile URLs from search results text."""
    import re
    # Match patterns like linkedin.com/in/username
    pattern = r'https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_-]+)'
    matches = re.findall(pattern, search_text)
    return [f"https://linkedin.com/in/{username}" for username in set(matches)]


def _extract_username_from_url(url: str) -> str | None:
    """Extract username from LinkedIn profile URL."""
    import re
    match = re.search(r'/in/([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else None


def _parse_profile_for_filters(
    profile_result: dict[str, Any],
    past_company_list: list[str],
    current_title: str | None,
) -> dict[str, Any]:
    """Parse profile result and check if it matches filters."""
    sections = profile_result.get("sections", {})
    experience_text = sections.get("experience", "")
    main_text = sections.get("main", "")

    # Combine all text for analysis
    full_text = f"{main_text}\n{experience_text}".lower()

    # Check past companies
    matched_companies = []
    for company in past_company_list:
        if company.lower() in full_text:
            matched_companies.append(company)

    has_past_company = len(matched_companies) > 0

    # Check current title
    has_current_title = False
    if current_title:
        # Look for current title in the beginning of experience or headline
        title_variations = [
            current_title.lower(),
            current_title.lower().replace(" ", ""),
        ]
        # Check if title appears near the beginning (likely current position)
        first_section = full_text[:2000]  # First 2000 chars usually contains current position
        has_current_title = any(tv in first_section for tv in title_variations)

    # Determine match level
    matches_all = has_past_company and (not current_title or has_current_title)
    matches_partial = has_past_company or has_current_title

    return {
        "username": profile_result.get("username"),
        "url": profile_result.get("url"),
        "name": _extract_name_from_profile(main_text),
        "headline": _extract_headline_from_profile(main_text),
        "matched_companies": matched_companies,
        "has_past_company": has_past_company,
        "has_current_title": has_current_title,
        "matches_all": matches_all,
        "matches_partial": matches_partial,
        "experience_preview": experience_text[:500] if experience_text else "",
    }


def _extract_name_from_profile(text: str) -> str:
    """Extract name from profile text (usually first line)."""
    lines = text.strip().split('\n')
    return lines[0].strip() if lines else "Unknown"


def _extract_headline_from_profile(text: str) -> str:
    """Extract headline from profile text (usually second line)."""
    lines = text.strip().split('\n')
    return lines[1].strip() if len(lines) > 1 else ""

