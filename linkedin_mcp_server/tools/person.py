"""
LinkedIn person profile reading tools.

Uses innerText extraction for resilient profile data capture
with configurable section selection.
"""

import asyncio
import json
from datetime import date
import logging
import random
import time
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator, Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ENRICH_MY_NETWORK_MAX_SECONDS,
    SYNC_MY_CONNECTIONS_MAX_SECONDS,
    tool_timeout_for,
)
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.linkedin import parse_person_sections
from linkedin_mcp_server.linkedin.contracts import FilterValidationError
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier
from linkedin_mcp_server.core.throttle import throttled_count
from linkedin_mcp_server.linkedin.my_connections import CONNECTIONS_URL
from linkedin_mcp_server.network.ranking import rank_paths
from linkedin_mcp_server.linkedin.search_urls import build_people_search_url
from linkedin_mcp_server.tools.network import open_store

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

#: Seconds between two people in an enrichment pass, plus uniform jitter.
#: Each person is two page loads (profile and experience), which `read_person`
#: already spaces; this keeps the pass well under the one-profile-a-second
#: limit live checks hold to, however long it runs.
ENRICH_PAUSE = 5.0
ENRICH_JITTER = 3.0


def register_person_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register all person-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Person Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person"},
    )
    @tool_errors
    async def get_person_profile(
        linkedin_username: str,
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
    ) -> dict[str, Any]:
        """
        Get a specific person's LinkedIn profile.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel", "williamhgates"). A full profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to read.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, volunteering, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects", "honors,languages", "posts"
                Default (None) reads only the main profile page.
            max_scrolls: Maximum pagination attempts per section to load more content.
                On detail sections (experience, certifications, skills, etc.) this
                is the max number of "Show more" button clicks. On activity/posts
                it is the max scroll-to-bottom iterations. Applies to all sections
                in this call. Default (None) uses 5 for detail sections and 10 for
                posts. Increase when a profile has many items in a section
                (e.g., 30+ certifications, max_scrolls=20). To avoid slowing down
                other sections, request heavy sections in a separate call.

        Returns:
            Dict with url, sections (name -> raw text), and optional references and section_errors.
            Sections may be absent if extraction yielded no content for that page.
            contact_info is read only from an accepted contact-overlay root. If no such root
            is found, the section is omitted and section_errors explains the failure;
            underlying profile text and links are never substituted. Existing suspected
            rate-limit retry and stop behavior is retained.
            Includes unknown_sections when unrecognised names are passed.
            The LLM should parse the raw text in each section.
        """
        linkedin_username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="get_person_profile")
        requested, unknown = parse_person_sections(sections)

        logger.info(
            "Reading profile: %s (sections=%s)",
            linkedin_username,
            sections,
        )

        cb = MCPContextProgressCallback(ctx)
        result = await extractor.read_person(
            linkedin_username,
            requested,
            callbacks=cb,
            max_scrolls=max_scrolls,
        )

        if unknown:
            result["unknown_sections"] = unknown

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Search People",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search"},
    )
    @tool_errors
    async def search_people(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        network: StrList | None = None,
        current_company: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for people on LinkedIn.

        Args:
            keywords: Search keywords (e.g., "software engineer", "recruiter at Google")
            ctx: FastMCP context for progress reporting
            location: Optional location filter, as LinkedIn's numeric geo URN
                id (e.g., "103644278" for the United States). Plain-text place
                names are silently ignored by LinkedIn and return unfiltered
                results, so they are refused here. The id is the geoUrn value
                in the URL of a LinkedIn people search filtered to that place;
                without one, put the place in keywords instead.
            network: Optional connection-degree filter. Each element is one of
                "F" (1st-degree), "S" (2nd-degree), "O" (3rd-degree and beyond).
                Example: ["F"] to only return 1st-degree connections. A single
                token ("F") or a comma-separated string ("F,S") is also
                accepted, for clients that cannot transmit an array.
            current_company: Optional current-employer filter. LinkedIn's
                currentCompany facet only filters on the numeric company URN id
                (e.g. "1115" for SAP); plain company names are accepted by the
                URL but ignored by LinkedIn and return the unfiltered result
                set. Look up a company's URN via get_company_profile -- it is
                exposed under references["about"]. For company-wide employee
                demographics (location/education/function breakdown) plus a
                slug-based lookup, use get_company_employees instead.

        Returns:
            Dict with url, sections (name -> raw text), and optional references.
            The LLM should parse the raw text to extract individual people and their profiles.
        """
        try:
            # The builder refuses a filter LinkedIn would ignore. Doing it here
            # keeps that refusal off the browser: get_ready_extractor can install
            # Chromium or rotate a login before this call would have failed.
            build_people_search_url(
                keywords,
                location=location,
                network=network,
                current_company=current_company,
            )
        except FilterValidationError as e:
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        extractor = await get_ready_extractor(ctx, tool_name="search_people")
        logger.info(
            "Searching people: keywords='%s', location='%s', network=%s, current_company='%s'",
            keywords,
            location,
            network,
            current_company,
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
            )
        except FilterValidationError as e:
            # Validation messages carry actionable detail; surface
            # them as ToolError so mask_error_details doesn't reduce
            # them to "Error calling tool 'search_people'".
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout_for("sync_my_connections", tool_timeout),
        title="Sync My Connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "network"},
    )
    @tool_errors
    async def sync_my_connections(
        ctx: Context,
        full: bool = False,
        time_budget_seconds: Annotated[
            int, Field(ge=30, le=SYNC_MY_CONNECTIONS_MAX_SECONDS)
        ] = 600,
        stop_after_known: Annotated[int, Field(ge=5, le=500)] = 25,
    ) -> dict[str, Any]:
        """
        Read your own 1st-degree connections list into the local network store.

        Walks linkedin.com/mynetwork/invite-connect/connections/ (newest first),
        scrolling slowly, and saves each connection's name, headline, profile
        URL and connection date to a local SQLite file. Afterwards
        search_my_network and get_network_overview answer from that file
        without touching LinkedIn. Every batch is saved as it is read, so a run
        cut short keeps what it read; call again to continue.

        This is the heaviest read the server makes. Run it rarely: a full walk
        once, then incremental ones. Prefer import_connections_csv for the bulk
        of the data when the user has LinkedIn's data export.

        Args:
            ctx: FastMCP context for progress reporting
            full: False (default) stops once `stop_after_known` consecutive
                connections are already in the store -- only new connections
                are read -- but only after some earlier walk reached the end of
                the list; until then every call walks the whole list, so a run
                cut short is finished by the next one. True always walks the
                whole list, e.g. to refresh headlines.
            time_budget_seconds: How long the walk may run (30-900, default 600).
            stop_after_known: Incremental stop threshold (default 25).

        Returns:
            Dict with url, seen, added, total_in_store, listed_on_linkedin (the
            count the page states, when it states one), incremental, stop_reason
            (end_of_list | reached_known_connections | time_budget | max_rounds
            | throttled), complete, and store path. When complete is false,
            calling again continues; when stop_reason is throttled, wait hours
            before retrying.
        """
        extractor = await get_ready_extractor(ctx, tool_name="sync_my_connections")
        store = open_store()
        try:
            known = store.known_slugs()
            # Until one walk has reached the end of the list, the store may
            # hold a newest-first prefix with a gap below it, so an early stop
            # at known connections would leave that gap unread.
            incremental = not full and store.has_complete_walk()
            run_id = store.start_run("page_incremental" if incremental else "page_full")
            added = 0

            def on_batch(batch: list[Any]) -> None:
                nonlocal added
                added += store.upsert_page_connections(batch).added

            async def on_progress(message: str, read: int) -> None:
                await ctx.report_progress(progress=read, total=None, message=message)

            logger.info(
                "Syncing connections (full=%s, budget=%ss, known=%d)",
                full,
                time_budget_seconds,
                len(known),
            )
            result = await extractor.walk_my_connections(
                on_batch,
                known=known,
                stop_after_known=stop_after_known if incremental else None,
                time_budget=float(time_budget_seconds),
                on_progress=on_progress,
            )
            store.finish_run(
                run_id,
                seen=result.seen,
                added=added,
                complete=result.complete,
                note=result.stop_reason,
            )
            return {
                "url": CONNECTIONS_URL,
                "seen": result.seen,
                "added": added,
                "total_in_store": store.count(),
                "listed_on_linkedin": result.listed_total,
                "rounds": result.rounds,
                "stop_reason": result.stop_reason,
                "complete": result.complete,
                "incremental": incremental,
                "throttled": result.throttled,
                "store": str(store.path),
            }
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout_for("enrich_my_network", tool_timeout),
        title="Enrich My Network",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "network"},
    )
    @tool_errors
    async def enrich_my_network(
        ctx: Context,
        usernames: StrList | None = None,
        query: str | None = None,
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
        refresh: bool = False,
        time_budget_seconds: Annotated[
            int, Field(ge=30, le=ENRICH_MY_NETWORK_MAX_SECONDS)
        ] = 300,
    ) -> dict[str, Any]:
        """
        Read About and job history for some of your connections into the local store.

        For each person it reads the main profile (headline, About, current
        role) and the full experience page, and stores both. After that,
        search_my_network matches words in those texts too (past employers,
        roles, topics in About), returns the matching passage, and
        get_network_person returns the stored texts without touching LinkedIn.

        Each person costs two page loads and the pass is paced, so enrich a
        shortlist, not the whole network: search first, then enrich the
        candidates whose fit depends on their history.

        Args:
            ctx: FastMCP context for progress reporting
            usernames: Connections to enrich (LinkedIn usernames or profile
                URLs, as search_my_network returns them in `slug`).
            query: Instead of usernames, enrich the top matches of this
                search_my_network query.
            limit: Most people to read in this call (1-30, default 10).
            refresh: Re-read people already enriched (default False skips them).
            time_budget_seconds: How long the pass may run (30-900, default 300).

        Returns:
            Dict with enriched (slug, name, sections read, and any errors per
            person), skipped (already enriched or not in your network),
            remaining (candidates left for another call), stop_reason
            (done | limit | time_budget | throttled), and store path.
        """
        if not usernames and not query:
            raise CodedToolError(
                "Pass usernames or a query to choose who to enrich.",
                ErrorCode.INVALID_ARGUMENT,
            )
        store = open_store()
        try:
            skipped: list[dict[str, str]] = []
            if usernames:
                wanted = [normalize_person_identifier(u) for u in usernames]
            else:
                wanted = [
                    r["slug"]
                    for r in store.search(query, limit=200)["results"]
                    if not r["slug"].startswith("csv:")
                ]
            candidates = []
            for slug in dict.fromkeys(wanted):
                if store.get(slug) is None:
                    skipped.append({"slug": slug, "reason": "not_in_network"})
                else:
                    candidates.append(slug)
            if not refresh:
                fresh = store.without_details(candidates)
                skipped += [
                    {"slug": s, "reason": "already_enriched"}
                    for s in candidates
                    if s not in fresh
                ]
                candidates = fresh

            extractor = await get_ready_extractor(ctx, tool_name="enrich_my_network")
            deadline = time.monotonic() + time_budget_seconds
            throttle_before = throttled_count()
            enriched: list[dict[str, Any]] = []
            stop_reason = "done"
            for i, slug in enumerate(candidates):
                if i >= limit:
                    stop_reason = "limit"
                    break
                if time.monotonic() >= deadline:
                    stop_reason = "time_budget"
                    break
                if i:
                    await asyncio.sleep(ENRICH_PAUSE + random.uniform(0, ENRICH_JITTER))
                await ctx.report_progress(
                    progress=i,
                    total=min(limit, len(candidates)),
                    message=f"Reading {slug}",
                )
                result = await extractor.read_person(slug, {"experience"})
                sections = result.get("sections", {})
                errors = result.get("section_errors", {})
                if sections:
                    store.save_details(
                        slug,
                        profile_text=sections.get("main_profile"),
                        experience_text=sections.get("experience"),
                    )
                person = store.get(slug) or {}
                entry: dict[str, Any] = {
                    "slug": slug,
                    "name": person.get("name"),
                    "read": sorted(sections),
                }
                if errors:
                    entry["errors"] = errors
                enriched.append(entry)
                rate_limited = any(
                    e.get("error_type") == "rate_limit" for e in errors.values()
                )
                if rate_limited or throttled_count() > throttle_before:
                    stop_reason = "throttled"
                    break
            done = {e["slug"] for e in enriched}
            return {
                "enriched": enriched,
                "skipped": skipped,
                "remaining": [s for s in candidates if s not in done],
                "stop_reason": stop_reason,
                "store": str(store.path),
            }
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Find Introductions",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "search", "network"},
    )
    @tool_errors
    async def find_introductions(
        keywords: str,
        ctx: Context,
        location: str | None = None,
        current_company: str | None = None,
        pages: Annotated[int, Field(ge=1, le=3)] = 1,
    ) -> dict[str, Any]:
        """
        Find 2nd-degree people for a purpose, and which of your connections could introduce you.

        Runs a LinkedIn people search limited to 2nd-degree connections and,
        for every result, reads the mutual connections LinkedIn links on the
        card. Each mutual connection is looked up in your local network store,
        so each introducer comes with the headline, company, email and
        connection date you have for them. Fill the store first with
        sync_my_connections or import_connections_csv.

        Search your own network with search_my_network first; use this when
        nobody there fits. A broad role or topic works best ("AI governance",
        "program officer horizon europe"). Each page is one search load.

        Args:
            keywords: What the people you want to reach do or work on.
            ctx: FastMCP context for progress reporting
            location: Optional LinkedIn numeric geo URN id (as search_people).
            current_company: Optional LinkedIn numeric company URN id.
            pages: Result pages to read, 1-3 (10 results each, default 1).

        Results are ranked by their warmest path, not by LinkedIn's order
        alone: how well you know the introducer (your mark_connections marks
        first, then message history from import_messages_csv, then how long
        you have been connected), whether introducer and target share an
        organisation, how many of your connections link to the target, and
        the search rank. Mark the people you are close to, and import your
        messages, for a much better ranking.

        Returns:
            Dict with url, results (each: slug, name, score, reasons, best_via,
            card -- the result's visible lines such as headline, location and
            current role -- and introducers, your stored connections linked on
            that card, each with tie and reasons), and by_introducer (your
            connections ranked by how many results they connect you to).
            LinkedIn names only the first two or three mutual connections on a
            card, so a card may show "& 2 other mutual connections" in its
            lines beyond the introducers listed.
        """
        try:
            base_url = build_people_search_url(
                keywords,
                location=location,
                network=["S"],
                current_company=current_company,
            )
        except FilterValidationError as e:
            raise CodedToolError(str(e), ErrorCode.INVALID_ARGUMENT) from e

        extractor = await get_ready_extractor(ctx, tool_name="find_introductions")
        store = open_store()
        try:
            first_degree = store.all_slugs()
            cards = []
            for page in range(1, pages + 1):
                if page > 1:
                    await asyncio.sleep(ENRICH_PAUSE + random.uniform(0, ENRICH_JITTER))
                url = base_url if page == 1 else f"{base_url}&page={page}"
                await ctx.report_progress(
                    progress=page - 1,
                    total=pages,
                    message=f"Reading results page {page}",
                )
                page_cards = await extractor.read_second_degree(url, first_degree)
                if not page_cards:
                    break
                cards.extend(page_cards)

            seen: set[str] = set()
            results: list[dict[str, Any]] = []
            by_introducer: dict[str, list[str]] = {}
            for card in cards:
                if card.slug in seen:
                    continue
                seen.add(card.slug)
                introducers = [p for p in (store.get(m) for m in card.mutual) if p]
                for p in introducers:
                    by_introducer.setdefault(p["slug"], []).append(card.slug)
                results.append(
                    {
                        "slug": card.slug,
                        "name": card.name,
                        "card": card.lines[:10],
                        "introducers": [
                            {
                                k: p[k]
                                for k in (
                                    "slug",
                                    "name",
                                    "headline",
                                    "company",
                                    "position",
                                    "email",
                                    "connected_on",
                                    "has_details",
                                )
                                if k in p
                            }
                            for p in introducers
                        ],
                    }
                )
            intro_slugs = list(by_introducer)
            results = rank_paths(
                results,
                store.ties(intro_slugs),
                {
                    slug: (store.details(slug) or {}).get("experience_text")
                    for slug in intro_slugs
                },
                date.today(),
            )
            ranked = sorted(by_introducer.items(), key=lambda kv: -len(kv[1]))
            answer: dict[str, Any] = {
                "url": base_url,
                "results": results,
                "by_introducer": [
                    {
                        "slug": slug,
                        "name": (store.get(slug) or {}).get("name"),
                        "headline": (store.get(slug) or {}).get("headline"),
                        "connects_you_to": targets,
                    }
                    for slug, targets in ranked
                ],
            }
            if not first_degree:
                answer["note"] = (
                    "Your network store is empty, so no introducers could be "
                    "recognised. Run sync_my_connections or import_connections_csv "
                    "first."
                )
            return answer
        finally:
            store.close()

    @mcp.tool(
        timeout=tool_timeout,
        title="Connect With Person",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "actions"},
    )
    @tool_errors
    async def connect_with_person(
        linkedin_username: str,
        ctx: Context,
        note: str | None = None,
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

            ``connected`` means this call submitted the invitation and the
            re-read profile no longer exposes Connect; it does not mean a
            1st-degree connection. The ``message`` names the state read after
            the send, normally pending. ``pending`` means an invitation was
            already outstanding before the call, and ``accepted`` means an
            incoming invitation was accepted.

            When status is ``custom_note_limit_reached`` LinkedIn rejected
            personalized invite notes because the free note quota for the
            account is exhausted. The ``message`` is the raw Premium dialog
            text read from LinkedIn.

            A status of ``outcome_unknown`` comes from the transport rather
            than the page: the call's answer never arrived from the browser
            process, so whether the invitation was sent is unknown. It carries
            ``retry_safe: False`` and no ``note_sent``; check the profile
            before calling again, because a repeat may invite twice.
        """
        linkedin_username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="connect_with_person")
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

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Sidebar Profiles",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person"},
    )
    @tool_errors
    async def get_sidebar_profiles(
        linkedin_username: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Get profile links from sidebar recommendation sections on a LinkedIn profile page.

        Extracts profiles from "More profiles for you", "Explore premium profiles",
        and "People you may know" sidebar sections. Follows "Show all" links to
        return the full list from each section. Sections that redirect to
        linkedin.com/premium are skipped.

        Args:
            linkedin_username: LinkedIn username of the profile page to read; a full profile URL is accepted too
                (e.g., "stickerdaniel", "williamhgates")
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and sidebar_profiles mapping section key to a list of
            /in/username/ paths. Only sections present on the page are included.
        """
        linkedin_username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="get_sidebar_profiles")
        logger.info("Getting sidebar profiles for: %s", linkedin_username)

        await ctx.report_progress(
            progress=0, total=100, message="Extracting sidebar profiles"
        )

        result = await extractor.get_sidebar_profiles(linkedin_username)

        await ctx.report_progress(progress=100, total=100, message="Complete")

        return result

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person"},
    )
    @tool_errors
    async def get_my_profile(
        ctx: Context,
        sections: str | None = None,
        max_scrolls: Annotated[int, Field(ge=1, le=50)] | None = None,
    ) -> dict[str, Any]:
        """
        Get the authenticated user's own LinkedIn profile.

        Navigates to /in/me/ and resolves the redirect to obtain the real
        username before reading the profile, so the url field in the result is the actual
        profile URL (e.g. linkedin.com/in/johndoe/) rather than /in/me/.

        Args:
            ctx: FastMCP context for progress reporting
            sections: Comma-separated list of extra sections to read.
                The main profile page is always included.
                Available sections: experience, education, interests, honors, volunteering, languages, certifications, skills, projects, contact_info, posts
                Examples: "experience,education", "contact_info", "skills,projects"
                Default (None) reads only the main profile page.
            max_scrolls: Maximum pagination attempts per section (same as get_person_profile).

        Returns:
            Dict with url, sections (name -> raw text), and optional references and section_errors.
            The url field reflects the resolved profile URL, revealing the real username.
            Sections may be absent if extraction yielded no content for that page.
            contact_info is read only from an accepted contact-overlay root. If no such root
            is found, the section is omitted and section_errors explains the failure;
            underlying profile text and links are never substituted. Existing suspected
            rate-limit retry and stop behavior is retained.
            Includes unknown_sections when unrecognised names are passed.
        """
        extractor = await get_ready_extractor(ctx, tool_name="get_my_profile")
        requested, unknown = parse_person_sections(sections)

        logger.info("Reading own profile (sections=%s)", sections)

        cb = MCPContextProgressCallback(ctx)
        result = await extractor.get_my_profile(
            sections=requested,
            callbacks=cb,
            max_scrolls=max_scrolls,
        )

        if unknown:
            result["unknown_sections"] = unknown

        return result
