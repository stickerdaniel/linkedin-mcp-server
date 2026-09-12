"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import json
import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BeforeValidator, Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import parse_person_sections
from linkedin_mcp_server.scraping.extractor import FilterValidationError

logger = logging.getLogger(__name__)


def _coerce_str_list(value: Any) -> Any:
    """Accept a string where a list of strings is declared.

    ``network`` is published as ``anyOf: [array, null]``, which is a correct
    JSON Schema. Some MCP clients collapse an ``anyOf``-with-null union to an
    untyped ``{}`` and then transmit the value as a string, so the array the
    caller wrote never arrives as one and pydantic rejects it (#739).

    Coercing at the tool boundary keeps the transport quirk here and leaves
    ``LinkedInExtractor.search_people`` strictly ``list[str]``. Only the
    container shape is repaired; token values are still validated downstream,
    so an invalid token fails with the same message it always did.
    """
    if not isinstance(value, str):
        return value

    text = value.strip()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except ValueError:
            pass
        else:
            if isinstance(decoded, list):
                return decoded

    return [part.strip() for part in text.split(",") if part.strip()]


StrList = Annotated[list[str], BeforeValidator(_coerce_str_list)]


def register_person_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all person-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Person Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects", "honors,languages", "posts"
                Default (None) scrapes only the main profile page.
            max_scrolls: Maximum pagination attempts per section to load more content.
                On detail sections (experience, certifications, skills, etc.) this
                is the max number of "Show more" button clicks. On activity/posts
                it is the max scroll-to-bottom iterations. Applies to all sections
                in this call. Default (None) uses 5 for detail sections and 10 for
                posts. Increase when a profile has many items in a section
                (e.g., 30+ certifications, max_scrolls=20). To avoid slowing down
                other sections, request heavy sections in a separate call.

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            Sections may be absent if extraction yielded no content for that page.
            Includes unknown_sections list when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_person_profile"
            )
            requested, unknown = parse_person_sections(sections)

            logger.info(
                "Scraping profile: %s (sections=%s)",
                linkedin_username,
                sections,
            )

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.scrape_person(
                linkedin_username,
                requested,
                callbacks=cb,
                max_scrolls=max_scrolls,
            )

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_person_profile")
        except Exception as e:
            raise_tool_error(e, "get_person_profile")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Search People",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search"},
        exclude_args=["extractor"],
    )
    async def search_people(
        ctx: Context,
        keywords: str | None = None,
        location: str | None = None,
        network: StrList | None = None,
        current_company: StrList | None = None,
        max_pages: Annotated[int, Field(ge=1, le=10)] = 1,
        title: str | None = None,
        past_company: StrList | None = None,
        industry: StrList | None = None,
        school: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        profile_language: StrList | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn, with Clay-style facets.

        At least one of keywords or a facet is required. Every result page is
        one navigation against the daily budget, and each company name that
        has to be resolved costs one or two more; pass numeric ids (or
        companies already in the cache) to keep it at one per page.

        Recommended funnel for account-based prospecting: search_companies
        (industry/size/hq_location facets) -> enrich_companies(about=True)
        -> query_company_cache to pick the accounts -> search_people(
        current_company=[...ids or names...], keywords='"<title>"') for
        the people.

        Args:
            ctx: FastMCP context for progress reporting
            keywords: Free-text query (e.g., "software engineer", "recruiter
                at Google"). Boolean search works on free LinkedIn: AND, OR,
                NOT, quoted phrases and parentheses, e.g.
                '("head of sales" OR "VP sales") AND NOT recruiter'.
            location: Optional location filter: a country or city name
                (e.g., "Egypt", "United Arab Emirates", "Amsterdam"). It is
                resolved to LinkedIn's numeric geo id through the site's own
                location dropdown, so free-text names actually filter. A name
                LinkedIn's dropdown does not recognize raises an error rather
                than silently returning worldwide results.
            network: Optional connection-degree filter. Each element is one of
                "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
                Example: ["F"] to only return 1st-degree connections. A single
                token ("F") or a comma-separated string ("F,S") is also
                accepted, for clients that cannot transmit an array.
            current_company: Optional current-employer filter, one or a list.
                Each is a company name (e.g. "SAP"), a /company/<slug> URL, or
                the numeric company URN id (e.g. "1115" for SAP). LinkedIn's
                currentCompany facet filters on the id only, so a name or URL
                is resolved to it first (company search, then the company's
                About page; cached on disk so a company already looked up
                costs no navigation). A name that does not resolve raises an
                error rather than silently returning the unfiltered result
                set. Pass the id directly, as exposed by get_company_profile
                under references["about"], to skip the resolution. For
                company-wide employee demographics (location/education/
                function breakdown) plus a slug-based lookup, use
                get_company_employees instead.
            max_pages: Number of result pages to load, 1-10 (default 1).
                LinkedIn returns 10 people per page, so max_pages=10 yields up
                to 100. Pagination stops early once a page adds no new people.
                Raise this when you need more than a top-10 sample -- e.g.
                enumerating 1st-degree connections in a region with
                network=["F"].
            title: Optional current job title, free text (e.g. "Head of
                Sales"). Measured live (2026-09-12) as silently ignored by
                LinkedIn's current results page: the results did not match
                the title. Prefer putting the title in keywords as a quoted
                phrase, e.g. '"VP Engineering"', which does filter; this
                parameter is kept for a results-page variant that may still
                read it and is never merged into keywords for you. On its
                own it is refused with an error rather than returning an
                unfiltered worldwide list: combine it with another facet
                (location, current_company, ...) or use keywords.
            past_company: Optional past-employer filter; same shapes and
                resolution as current_company. Each unresolved name may cost
                up to two navigations. The facet's URL parameter name is
                unverified against live LinkedIn; a wrong name is ignored,
                so cross-check results.
            industry: Optional industry filter, one or a list. Each is
                LinkedIn's numeric industry id (e.g. "4") or a name this
                server knows (e.g. "Software Development", "Financial
                Services"; same table as search_companies). An unknown name
                raises an error listing the known names. The facet's URL
                parameter name and values are unverified against live
                LinkedIn; a wrong name is ignored, so cross-check results.
            school: Optional school filter, the numeric school id only (a
                name raises an error: LinkedIn's schools search exposes no
                id to resolve it from). To find the id: LinkedIn people
                search -> All filters -> School -> pick one; the URL then
                shows schoolFilter=["<id>"]. The facet's URL parameter name
                is unverified against live LinkedIn; a wrong name is
                ignored, so cross-check results.
            first_name: Optional first-name filter (verified live).
            last_name: Optional last-name filter. The facet's URL parameter
                name is unverified against live LinkedIn; a wrong name is
                ignored, so cross-check results.
            profile_language: Optional profile-language filter, one or a list
                of two-letter ISO 639-1 codes (e.g. "en", "de", "fr"). The
                facet's URL parameter name and values are unverified against
                live LinkedIn; a wrong name is ignored, so cross-check
                results.

        Returns:
            Dict with url, sections (name -> raw text), people, result_count,
            and optional references. Pages are joined by a "---" line in the
            raw text; references are deduplicated by URL across pages.
            people is a list of rows {name, degree, headline, location,
            snippet, url[, followers]} parsed from the raw text, deduplicated
            by url across pages; url is null when a card could not be paired
            with a profile link. result_count is the "About N results" header
            of the first page, or null; the measured people-search page
            renders no such header, so expect null here (company search
            has one). Fall back to the raw text for anything the rows do
            not carry.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_people"
            )
            logger.info(
                "Searching people: keywords='%s', location='%s', network=%s, "
                "current_company=%s, past_company=%s, title='%s', industry=%s, "
                "school='%s', max_pages=%d",
                keywords,
                location,
                network,
                current_company,
                past_company,
                title,
                industry,
                school,
                max_pages,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            try:
                result = await extractor.search_people(
                    keywords,
                    location,
                    network=network,
                    current_company=current_company,
                    max_pages=max_pages,
                    title=title,
                    past_company=past_company,
                    industry=industry,
                    school=school,
                    first_name=first_name,
                    last_name=last_name,
                    profile_language=profile_language,
                )
            except FilterValidationError as e:
                # Validation messages carry actionable detail; surface
                # them as ToolError so mask_error_details doesn't reduce
                # them to "Error calling tool 'search_people'".
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            # Already a properly formatted client-facing error; do not
            # log it as "Unexpected error" via raise_tool_error.
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search_people")
        except Exception as e:
            raise_tool_error(e, "search_people")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With Person",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
        exclude_args=["extractor"],
    )
    async def connect_with_person(
        linkedin_username: str,
        ctx: Context,
        note: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send a LinkedIn connection request or accept an incoming one.

        The tool is annotated with destructiveHint so MCP clients will
        prompt for user confirmation before execution.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            note: Optional note to include with the invitation

        Returns:
            Dict with url, status, message, and note_sent.
            Statuses: pending, already_connected, follow_only,
            connect_unavailable, unavailable, send_failed,
            note_not_supported, custom_note_limit_reached,
            connected, or accepted.

            When status is ``custom_note_limit_reached`` LinkedIn rejected
            personalized invite notes because the free note quota for the
            account is exhausted. The ``message`` is the raw Premium dialog
            text read from LinkedIn.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="connect_with_person"
            )
            logger.info(
                "Connecting with person: %s (note=%s)",
                linkedin_username,
                note is not None,
            )

            await ctx.report_progress(
                progress=0,
                total=100,
                message="Starting LinkedIn connection flow",
            )

            result = await extractor.connect_with_person(
                linkedin_username,
                note=note,
            )

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "connect_with_person")
        except Exception as e:
            raise_tool_error(e, "connect_with_person")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sidebar Profiles",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_sidebar_profiles(
        linkedin_username: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get profile links from sidebar recommendation sections on a LinkedIn profile page.

        Extracts profiles from "More profiles for you", "Explore premium profiles",
        and "People you may know" sidebar sections. Follows "Show all" links to
        return the full list from each section. Sections that redirect to
        linkedin.com/premium are skipped.

        Args:
            linkedin_username: LinkedIn username of the profile page to scrape; a full profile URL is accepted too
                (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sidebar_profiles mapping section key to a list of
            /in/username/ paths. Only sections present on the page are included.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sidebar_profiles"
            )
            logger.info("Getting sidebar profiles for: %s", linkedin_username)

            await ctx.report_progress(
                progress=0, total=100, message="Extracting sidebar profiles"
            )

            result = await extractor.get_sidebar_profiles(linkedin_username)

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sidebar_profiles")
        except Exception as e:
            raise_tool_error(e, "get_sidebar_profiles")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_my_profile(
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the authenticated user's own LinkedIn profile.

        Navigates to /in/me/ and resolves the redirect to obtain the real
        username before scraping, so the url field in the result is the actual
        profile URL (e.g. linkedin.com/in/johndoe/) rather than /in/me/.

        Args:
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to scrape.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects"
                Default (None) scrapes only the main profile page.
            max_scrolls: Maximum pagination attempts per section (same as get_person_profile).

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            The url field reflects the resolved profile URL, revealing the real username.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_my_profile"
            )
            requested, unknown = parse_person_sections(sections)

            logger.info("Scraping own profile (sections=%s)", sections)

            cb = MCPContextProgressCallback(ctx)
            result = await extractor.get_my_profile(
                sections=requested,
                callbacks=cb,
                max_scrolls=max_scrolls,
            )

            if unknown:
                result["unknown_sections"] = unknown

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_my_profile")
        except Exception as e:
            raise_tool_error(e, "get_my_profile")  # NoReturn
