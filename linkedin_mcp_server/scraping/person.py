"""Person profile, own-profile, sidebar and people-search workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import logging
import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.fields import PERSON_SECTIONS
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.search_urls import build_people_search_url
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import SIDEBAR_CHROME_EN

if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)

# Pacing between page navigations. Owned here rather than copied, because the
# company and job walks that still sit on the facade pace themselves the same
# way: two constants would let one relocation give two workflows different
# policies without anything failing.
NAV_DELAY = 2.0


def _js_literal(value: str, quote: str) -> str:
    """Quote one locale-table label for the program below.

    Both quoting styles already occur in that program, and reproducing each
    exactly is what keeps it the byte-identical program the inlined original
    was. Escaping is deliberately absent: the table holds visible LinkedIn
    labels, and a label carrying a quote or a backslash would be a reason to
    stop matching on text here, not a reason to escape it.

    So the label that cannot be quoted is refused instead, and at import,
    because the alternative is a `SyntaxError` raised out of `page.evaluate`
    under an unguarded call — reachable only against live LinkedIn, and only
    once the table grows the locale this exists to accept.
    """
    if quote in value or "\\" in value:
        raise ValueError(
            f"sidebar chrome label {value!r} cannot be quoted with {quote!r}: "
            "a label carrying a quote or a backslash is a reason to stop "
            "matching on text, not a reason to escape it"
        )
    return f"{quote}{value}{quote}"


# The template indents the first heading and the join has to carry the rest,
# or headings two and three land at column zero. Only whitespace, and the
# `program_digest` the policy traces fingerprint strips it either way, which
# is exactly why nothing would have said so.
_HEADING_INDENT = " " * 20


# The sidebar headings and the "Show all" control are the one place this
# workflow reads visible text to classify anything, and the strings come from
# the explicit `en-US` table in `text.py` rather than from a copy here. Two
# substitutions instead of a literal, so changing the table changes the
# program and nothing has to remember to change both.
_SIDEBAR_PROFILES_JS = """() => {
                const SIDEBAR_SECTIONS = [
                    __SECTION_HEADINGS__
                ];
                const normalize = text => (text || '').replace(/\\s+/g, ' ').trim();
                const slugify = text => text.toLowerCase().replace(/\\s+/g, '_');
                const extractProfilePath = href => {
                    if (!href) return null;
                    const idx = href.indexOf('/in/');
                    if (idx === -1) return null;
                    const rest = href.slice(idx + 4);
                    const end = rest.search(/[/?#]/);
                    const username = end === -1 ? rest : rest.slice(0, end);
                    return username ? '/in/' + username + '/' : null;
                };

                const sections = {};
                const showAllUrls = {};

                const headings = Array.from(document.querySelectorAll('h1, h2, h3'));
                for (const heading of headings) {
                    const headingText = normalize(
                        heading.innerText || heading.textContent
                    );
                    if (!SIDEBAR_SECTIONS.includes(headingText)) continue;

                    const sectionKey = slugify(headingText);

                    // Walk up to find a section/aside container (max 5 levels)
                    let container = heading.parentElement;
                    let foundSection = false;
                    for (let depth = 0; container && depth < 5; depth++) {
                        const tag = container.tagName.toLowerCase();
                        if (tag === 'section' || tag === 'aside') { foundSection = true; break; }
                        container = container.parentElement;
                    }
                    if (!container || !foundSection) continue;

                    // Collect /in/ profile links, deduplicated
                    const seen = new Set();
                    const profileLinks = [];
                    for (const a of container.querySelectorAll('a[href*="/in/"]')) {
                        const path = extractProfilePath(a.getAttribute('href'));
                        if (path && !seen.has(path)) {
                            seen.add(path);
                            profileLinks.push(path);
                        }
                    }

                    // Find "Show all" / "See all" anchor within container
                    let showAll = null;
                    for (const a of container.querySelectorAll('a')) {
                        const text = normalize(
                            a.innerText || a.textContent
                        ).toLowerCase();
                        if (__SHOW_ALL_TEST__) {
                            showAll = a.href || a.getAttribute('href');
                            break;
                        }
                    }

                    sections[sectionKey] = profileLinks;
                    if (showAll) showAllUrls[sectionKey] = showAll;
                }

                return { sections, showAllUrls };
            }""".replace(
    "__SECTION_HEADINGS__",
    f",\n{_HEADING_INDENT}".join(
        _js_literal(heading, '"') for heading in SIDEBAR_CHROME_EN.section_headings
    ),
).replace(
    "__SHOW_ALL_TEST__",
    " || ".join(
        f"text.startsWith({_js_literal(prefix, chr(39))})"
        for prefix in SIDEBAR_CHROME_EN.show_all_prefixes
    ),
)

_SIDEBAR_EXPANDED_PROFILES_JS = """() => {
                    const extractProfilePath = href => {
                        if (!href) return null;
                        const idx = href.indexOf('/in/');
                        if (idx === -1) return null;
                        const rest = href.slice(idx + 4);
                        const end = rest.search(/[/?#]/);
                        const username = end === -1 ? rest : rest.slice(0, end);
                        return username ? '/in/' + username + '/' : null;
                    };
                    const seen = new Set();
                    const links = [];
                    for (const a of document.querySelectorAll(
                        'main a[href*="/in/"]'
                    )) {
                        const path = extractProfilePath(a.getAttribute('href'));
                        if (path && !seen.has(path)) {
                            seen.add(path);
                            links.push(path);
                        }
                    }
                    return links;
                }"""


class PersonScraper:
    """Own every workflow whose subject is one LinkedIn member."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        capture: SectionCapture,
        profile_page: ProfilePageReader,
    ):
        self._session = session
        self._navigator = navigator
        self._capture = capture
        self._profile_page = profile_page

    async def scrape_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Scrape a person profile with configurable sections.

        When ``main_profile_already_loaded`` is True and the bound page is on
        the exact profile root for ``username``, the ``main_profile`` section
        is extracted from the current page without re-navigating. Falls back
        to ``extract_page`` if the URL drifts or the reuse path returns the
        soft-rate-limit sentinel (preserving the retry semantics of
        ``extract_page``).

        Returns:
            {url, sections: {name: text}, profile_urn?: str}
        """
        requested = requested | {"main_profile"}
        username = normalize_person_identifier(
            username, allow_self_alias=allow_self_alias
        )
        base_url = person_profile_url(username)
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        profile_urn: str | None = None
        rate_limited = False

        requested_ordered = [
            (name, suffix, is_overlay)
            for name, (suffix, is_overlay) in PERSON_SECTIONS.items()
            if name in requested
        ]
        total = len(requested_ordered)

        if callbacks:
            await callbacks.on_start("person profile", base_url)

        try:
            for i, (section_name, suffix, is_overlay) in enumerate(requested_ordered):
                if i > 0:
                    await self._session.delay(NAV_DELAY)

                url = base_url + suffix
                try:
                    can_reuse_main = (
                        section_name == "main_profile"
                        and main_profile_already_loaded
                        and urlparse(self._session.page.url).path.rstrip("/")
                        == urlparse(base_url).path.rstrip("/")
                    )
                    if can_reuse_main:
                        extracted = await self._capture._extract_loaded_section(
                            url,
                            section_name=section_name,
                            max_scrolls=max_scrolls,
                        )
                        if extracted.text == RATE_LIMITED_SECTION_TEXT:
                            logger.info(
                                "Reuse path soft-rate-limited; falling back "
                                "to extract_page for retry parity"
                            )
                            extracted = await self._capture.extract_page(
                                url,
                                section_name=section_name,
                                max_scrolls=max_scrolls,
                            )
                    elif is_overlay:
                        extracted = await self._capture._extract_overlay(
                            url, section_name=section_name
                        )
                    else:
                        extracted = await self._capture.extract_page(
                            url,
                            section_name=section_name,
                            max_scrolls=max_scrolls,
                        )

                    if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
                        sections[section_name] = extracted.text
                        if extracted.references:
                            references[section_name] = extracted.references
                    elif extracted.text == RATE_LIMITED_SECTION_TEXT:
                        section_errors[section_name] = rate_limited_section_error()
                        # Stop rather than walk the remaining sections. Each one
                        # is another navigation, and LinkedIn has just said it
                        # wants fewer of them. Whatever was gathered before this
                        # point is kept and returned.
                        rate_limited = True
                    elif extracted.error:
                        section_errors[section_name] = extracted.error

                    # Skipped once the section came back empty: there is no
                    # content to read a URN from, and a failure here lands in
                    # the handler below, which would overwrite the entry just
                    # recorded with a generic diagnostic — losing the one
                    # finding this section had.
                    if (
                        section_name == "main_profile"
                        and profile_urn is None
                        and not rate_limited
                    ):
                        profile_urn = await self._profile_page._extract_profile_urn()
                except LinkedInScraperException:
                    raise
                except Exception as e:
                    logger.warning("Error scraping section %s: %s", section_name, e)
                    section_errors[section_name] = build_issue_diagnostics(
                        e,
                        context="scrape_person",
                        target_url=url,
                        section_name=section_name,
                    )

                # "Scraped" = processed/attempted, not necessarily successful.
                # Per-section failures are captured in section_errors.
                if callbacks:
                    percent = round((i + 1) / total * 95)
                    await callbacks.on_progress(
                        f"Scraped {section_name} ({i + 1}/{total})", percent
                    )

                if rate_limited:
                    break
        except LinkedInScraperException as e:
            if callbacks:
                await callbacks.on_error(e)
            raise

        result: dict[str, Any] = {
            "url": f"{base_url}/",
            "sections": sections,
        }
        if profile_urn:
            result["profile_urn"] = profile_urn
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors

        if callbacks:
            await callbacks.on_complete("person profile", result)

        return result

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Scrape the authenticated user's own LinkedIn profile.

        Navigates to /in/me/ and resolves the redirect to obtain the real
        username before scraping, so result["url"] reflects the actual profile
        URL rather than /in/me/.

        Returns:
            {url, sections: {name: text}}
        """
        await self._navigator._navigate_to_page("https://www.linkedin.com/in/me/")
        real_url = self._session.page.url  # post-redirect, e.g. /in/johndoe/
        match = re.search(r"/in/([^/?#]+)", real_url)
        username = match.group(1) if match else "me"
        logger.debug("get_my_profile resolved username=%r from %s", username, real_url)

        return await self.scrape_person(
            username,
            sections if sections is not None else {"main_profile"},
            callbacks=callbacks,
            max_scrolls=max_scrolls,
            main_profile_already_loaded=True,
            # The redirect is what resolves the alias. When it has not, this is
            # still the tool the user asked for, so "me" stays usable here and
            # nowhere else.
            allow_self_alias=True,
        )

    async def get_sidebar_profiles(self, username: str) -> dict[str, Any]:
        """Extract profile links from sidebar sections on a LinkedIn profile page.

        Scrapes "More profiles for you", "Explore premium profiles", and
        "People you may know" sidebar sections. Follows each "Show all" link to
        collect the full list; skips any section whose "Show all" URL contains or
        redirects to /premium.

        Returns:
            Dict with url and sidebar_profiles mapping section key to list of
            /in/username/ paths. Sections absent from the page are omitted.
        """
        username = normalize_person_identifier(username)
        url = person_profile_url(username, "/")
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()

        try:
            await self._session.page.wait_for_selector("main", timeout=5000)
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        await self._session.dismiss_modal()

        sidebar_data: dict[str, Any] = await self._session.page.evaluate(
            _SIDEBAR_PROFILES_JS
        )

        sidebar_profiles: dict[str, list[str]] = dict(sidebar_data.get("sections", {}))
        show_all_urls: dict[str, str] = dict(sidebar_data.get("showAllUrls", {}))

        first_show_all = True
        for section_key, show_all_url in show_all_urls.items():
            if "/premium" in show_all_url:
                continue

            if not first_show_all:
                await self._session.delay(NAV_DELAY)
            first_show_all = False

            try:
                await self._navigator._navigate_to_page(show_all_url)
            except LinkedInScraperException:
                raise
            except Exception:
                logger.debug(
                    "Failed to navigate to Show all for section %s: %s",
                    section_key,
                    show_all_url,
                )
                continue

            if "/premium" in self._session.page.url:
                logger.debug(
                    "Show all for section %s redirected to premium, skipping",
                    section_key,
                )
                continue

            await self._session.check_rate_limit()

            try:
                await self._session.page.wait_for_selector("main")
            except PlaywrightTimeoutError:
                logger.debug("No <main> on Show all page for section %s", section_key)

            await self._session.dismiss_modal()

            expanded_links: list[str] = await self._session.page.evaluate(
                _SIDEBAR_EXPANDED_PROFILES_JS
            )

            # Merge: sidebar links first, then show_all expansion, deduped
            existing = sidebar_profiles.get(section_key, [])
            seen_paths: set[str] = set(existing)
            merged = list(existing)
            for link in expanded_links:
                if link not in seen_paths:
                    seen_paths.add(link)
                    merged.append(link)
            sidebar_profiles[section_key] = merged

        return {
            "url": url,
            "sidebar_profiles": sidebar_profiles,
        }

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page.

        Args:
            keywords: Free-text query ("software engineer", "recruiter at Google").
            location: Optional location filter ("New York", "Remote").
            network: Optional connection-degree filter. Each element is one of
                ``"F"`` (1st-degree), ``"S"`` (2nd-degree), ``"O"`` (3rd-degree
                and beyond). Example: ``["F"]`` to only return 1st-degree
                connections. Invalid tokens raise ``ValueError``. The container
                shape is repaired at the MCP tool boundary, so the list arrives
                here already normalized.
            current_company: Optional current-employer filter. LinkedIn's
                ``currentCompany`` facet only filters on the numeric company
                URN id (e.g. ``"1115"`` for SAP); plain company names are
                accepted by the URL but ignored by LinkedIn and return the
                unfiltered result set. Look up a company's URN via
                ``get_company_profile`` -- it is exposed under
                ``references["about"]``.

        Returns:
            {url, sections: {name: text}}
        """
        # Builds before it navigates, and the builder refuses a filter
        # LinkedIn would swallow, so an invalid token costs no page load.
        url = build_people_search_url(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
        )
        extracted = await self._capture.extract_page(url, section_name="search_results")

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
