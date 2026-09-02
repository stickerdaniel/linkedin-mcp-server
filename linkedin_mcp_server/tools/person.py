"""
LinkedIn person profile scraping tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.

Modified in this fork (Apache-2.0 section 4(b)): search pagination and
facets, batch invitations, network-listing tools, and the
fallback_to_no_note option. See CHANGELOG-FORK.md.
"""

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping import parse_person_sections
from linkedin_mcp_server.scraping.extractor import FilterValidationError

logger = logging.getLogger(__name__)


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
        keywords: str,
        ctx: Context,
        network: list[str] | None = None,
        current_company: str | None = None,
        past_company: str | None = None,
        school: str | None = None,
        industry: str | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Free-text query (e.g., "software engineer"). To filter by location, include it in the keywords (e.g., "embedded engineer Bonn").
            ctx: FastMCP context for progress reporting
            network: Optional connection-degree filter. Each element is one of
                "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
                Example: ["F"] to only return 1st-degree connections. Second-degree
                people accept far more often than third, so this is usually the
                highest-leverage filter for a limited invite quota.
            current_company: Optional current-employer filter. LinkedIn's
                currentCompany facet only filters on the numeric company URN id
                (e.g. "1115" for SAP); plain company names are accepted by the
                URL but ignored by LinkedIn and return the unfiltered result
                set. Look up a company's URN via get_company_profile -- it is
                exposed under references["about"]. For company-wide employee
                demographics (location/education/function breakdown) plus a
                slug-based lookup, use get_company_employees instead.
            past_company: Optional former-employer filter, numeric URN id, same
                rule as current_company.
            school: Optional school filter, numeric school URN id. Alumni of a
                shared institution are the highest-accepting cold audience.
            industry: Optional industry filter, numeric industry id.
            page: 1-based results page. LinkedIn returns roughly ten people per
                page; without this most of a result set is unreachable.

        Returns:
            Dict with url, sections (name -> raw text), page, and optional references.
            The LLM should parse the raw text to extract individual people and their profiles.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="search_people"
            )
            logger.info(
                "Searching people: keywords='%s', network=%s, current_company='%s', "
                "past_company='%s', school='%s', industry='%s', page=%s",
                keywords,
                network,
                current_company,
                past_company,
                school,
                industry,
                page,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting people search"
            )

            try:
                result = await extractor.search_people(
                    keywords,
                    network=network,
                    current_company=current_company,
                    past_company=past_company,
                    school=school,
                    industry=industry,
                    page=page,
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
        fallback_to_no_note: bool = False,
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
            fallback_to_no_note: Send the invitation without a note when
                LinkedIn's personalized-invite quota is exhausted, instead of
                returning custom_note_limit_reached and sending nothing. Only
                applies where an invite can actually be sent; a profile that
                exposes no Connect action is still reported, never sent to.

        Returns:
            Dict with url, status, message, note_sent and resolved_username.
            Statuses: pending, already_connected, follow_only,
            connect_unavailable, unavailable, send_failed,
            note_not_supported, custom_note_limit_reached,
            connected, or accepted.

            ``resolved_username`` is the profile the call actually acted on.
            A state-bearing status is only returned once the browser has been
            confirmed to be on that profile, so comparing it against the
            requested username is a cheap check that the answer describes the
            right person. It differs from the request when LinkedIn resolves
            an outdated vanity name to a current one.

            ``unavailable`` includes the case where the profile could not be
            confirmed; nothing was sent and the call is safe to retry.

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
                fallback_to_no_note=fallback_to_no_note,
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

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With People (Batch)",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
        exclude_args=["extractor"],
    )
    async def connect_with_people(
        linkedin_usernames: list[str],
        ctx: Context,
        note: str | None = None,
        fallback_to_no_note: bool = False,
        min_delay_seconds: Annotated[float, Field(ge=0)] = 8.0,
        max_delay_seconds: Annotated[float, Field(ge=0)] = 25.0,
        max_invites: Annotated[int, Field(ge=1, le=20)] = 20,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Send connection requests to several people, paced by the server.

        Prefer this over calling connect_with_person in a loop: the pacing and
        the stop-on-resistance rule live here, and this server is the only
        component that sees all traffic to the account.

        Each invitation runs the full single-invite flow, so each one confirms
        the browser is on the right profile before reading any state and is
        gated on LinkedIn exposing a Connect action before anything is sent.
        The run stops at the first rate limit or authentication barrier rather
        than pushing through it, and returns what completed.

        The same note is sent to everyone, so keep it generic or omit it.

        Args:
            linkedin_usernames: Public identifiers or profile URLs, in order.
                Duplicates are collapsed before anything is sent.
            ctx: FastMCP context for progress reporting
            note: Optional note included with every invitation.
            fallback_to_no_note: Send without a note when the personalized-invite
                quota is exhausted, rather than skipping the person.
            min_delay_seconds: Lower bound of the randomised gap between invites.
            max_delay_seconds: Upper bound of that gap. Must be >= the lower bound.
            max_invites: Ceiling on invitations attempted in this call.

        Returns:
            Dict with results (one connection result per person, each carrying
            resolved_username), attempted, sent, stopped_early and, when the run
            ended early, stop_reason.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="connect_with_people"
            )
            logger.info(
                "Batch connect: %d requested (note=%s, max_invites=%d)",
                len(linkedin_usernames),
                note is not None,
                max_invites,
            )

            await ctx.report_progress(
                progress=0, total=100, message="Starting paced batch invitations"
            )

            try:
                result = await extractor.connect_with_people(
                    linkedin_usernames,
                    note=note,
                    fallback_to_no_note=fallback_to_no_note,
                    delay_range=(min_delay_seconds, max_delay_seconds),
                    max_invites=max_invites,
                )
            except FilterValidationError as e:
                # Carries the correction to make; surfaced as ToolError so
                # mask_error_details cannot reduce it to a generic failure.
                raise ToolError(str(e)) from e

            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "connect_with_people")
        except Exception as e:
            raise_tool_error(e, "connect_with_people")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sent Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "network"},
        exclude_args=["extractor"],
    )
    async def get_sent_invitations(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List connection invitations you have sent that are still pending.

        Use this to deduplicate before sending: without it, the only way to
        discover an invitation already exists is to attempt it again. It also
        shows the pending backlog, which is worth clearing periodically
        because LinkedIn weighs a high ratio of long-pending invitations
        against the account.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (sent_invitations -> raw text), and
            references containing the /in/ paths of each recipient.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_sent_invitations"
            )
            logger.info("Reading sent invitations")

            await ctx.report_progress(
                progress=0, total=100, message="Loading sent invitations"
            )
            result = await extractor.get_sent_invitations()
            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_sent_invitations")
        except Exception as e:
            raise_tool_error(e, "get_sent_invitations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Received Invitations",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "network"},
        exclude_args=["extractor"],
    )
    async def get_received_invitations(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        List incoming connection invitations awaiting your response.

        Accept one with connect_with_person, which detects the incoming
        state and clicks Accept.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (received_invitations -> raw text), and
            references containing the /in/ paths of each sender.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_received_invitations"
            )
            logger.info("Reading received invitations")

            await ctx.report_progress(
                progress=0, total=100, message="Loading received invitations"
            )
            result = await extractor.get_received_invitations()
            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_received_invitations")
        except Exception as e:
            raise_tool_error(e, "get_received_invitations")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Notifications",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "network"},
        exclude_args=["extractor"],
    )
    async def get_notifications(
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Read your LinkedIn notifications, including connection acceptances.

        The natural outreach loop is invite, wait for acceptance, then send a
        message. This is what makes the middle step visible: the moment
        someone has just accepted is when a message lands best.

        Args:
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, sections (notifications -> raw text), and optional
            references.
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_notifications"
            )
            logger.info("Reading notifications")

            await ctx.report_progress(
                progress=0, total=100, message="Loading notifications"
            )
            result = await extractor.get_notifications()
            await ctx.report_progress(progress=100, total=100, message="Complete")

            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_notifications")
        except Exception as e:
            raise_tool_error(e, "get_notifications")  # NoReturn
