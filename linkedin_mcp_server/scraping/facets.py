"""Resolution of free-text search facets to the ids LinkedIn filters on."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote_plus

import logging
import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.company_cache import CompanyCache, normalize_company_name
from linkedin_mcp_server.core.humanize import human_type
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.company_parse import parse_search_results
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    FilterValidationError,
)
from linkedin_mcp_server.scraping.identifiers import (
    company_page_url,
    normalize_company_identifier,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession, nav_delay

logger = logging.getLogger(__name__)

# The jobs-search typeahead renders its suggestions from a network round trip
# and selecting one navigates. Both are bounded so a stalled page reads as a
# miss rather than hanging the tool call.
TYPEAHEAD_TIMEOUT_MS = 5000
GEO_ID_PATTERN = re.compile(r"[?&]geoId=(\d+)")
LOCATION_BOX_SELECTOR = "input[id*='jobs-search-box-location']"


def _company_urn_of_first_card(references: list[Reference]) -> str | None:
    """The ``company_urn`` reference belonging to the first company card, if any.

    References come in DOM order, so an id anchor sitting between the first
    ``/company/<slug>/`` link and the next card's link (a different slug) is
    the first card's own. One before any company link, or after the second
    card starts, is not attributed to anything.
    """
    first_slug: str | None = None
    for ref in references:
        if ref["kind"] == "company":
            match = re.search(r"/company/([^/?#]+)", ref["url"])
            slug = match.group(1) if match else None
            if first_slug is None:
                first_slug = slug
            elif slug != first_slug:
                return None
        elif first_slug is not None and ref["kind"] == "company_urn":
            value = ref.get("value")
            if value:
                return str(value)
    return None


class FacetResolver:
    """Resolve locations and companies to ids, once per name per tool call.

    Shared by people and company search. One resolver is built per facade,
    which is one per tool call, so the caches span a batch of names and no
    more.
    """

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        capture: SectionCapture,
    ):
        self._session = session
        self._navigator = navigator
        self._capture = capture
        # location name (casefolded) -> numeric geo id ("" means "did not
        # resolve"), so a repeated region in a batch resolves once.
        self._geo_cache: dict[str, str] = {}
        # company name/slug (casefolded) -> numeric company URN id ("" means
        # "did not resolve"), same contract as ``_geo_cache``.
        self._company_urn_cache: dict[str, str] = {}
        # Whether a resolution has navigated on this resolver: the next
        # navigation, here or the caller's first results page, is paced like
        # every later hop.
        self.navigated = False
        # The on-disk company cache, opened on first use so a resolver that
        # never resolves a company name never touches the filesystem.
        self._company_cache: CompanyCache | None = None

    async def resolve_geo_urn(self, location: str) -> str | None:
        """Resolve a free-text location to LinkedIn's numeric geo id.

        People search's location facet is ``geoUrn=["<id>"]`` (a numeric geo
        id), not the free-text ``location=`` param, which LinkedIn accepts in
        the URL but silently ignores -- so a plain ``location=Egypt`` returns
        the unfiltered result set. There is no stable public endpoint to map a
        name to a geo id (the REST typeahead is gone and the search box is now
        an opaque server-driven-UI action), so we resolve it the way a person
        does: drive the jobs-search location typeahead (a stable on-page
        dropdown), pick the top suggestion, and read the ``geoId`` LinkedIn
        itself puts in the URL. That numeric id doubles as the people-search
        geoUrn. Works for any country/city LinkedIn's own dropdown knows.

        Returns the id, or ``None`` if the dropdown offered no match. Results
        are cached per resolver so a repeated region costs one resolution; a
        dropdown that never opened is not, since a timeout says nothing about
        the name.
        """
        key = location.casefold()
        if key in self._geo_cache:
            return self._geo_cache[key] or None

        page = self._session.page
        await self._navigator._goto_with_auth_checks(
            "https://www.linkedin.com/jobs/search/?keywords="
        )
        self.navigated = True
        # The id is structural; an ``aria-label`` fallback would carry the
        # locale's own words for "location", which is exactly the kind of text
        # match that reads as a miss on a non-English profile.
        box = await page.query_selector(LOCATION_BOX_SELECTOR)

        if box is None:
            return None
        await box.click()
        await box.fill("")
        # Type it like a person; the dropdown resolves as we type.
        await human_type(page, location)
        suggestion = await self._first_location_suggestion(box)
        if suggestion is None:
            # A dropdown that never opened is a stalled page as often as an
            # unknown name; remembering it would pin a valid location as a
            # miss for the rest of the batch.
            return None
        await suggestion.click()
        try:
            await page.wait_for_url(GEO_ID_PATTERN, timeout=TYPEAHEAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        match = GEO_ID_PATTERN.search(page.url)
        geo_id = match.group(1) if match else None
        # LinkedIn answered, with or without an id: cache either so a repeated
        # name does not re-drive the dropdown.
        self._geo_cache[key] = geo_id or ""
        return geo_id

    async def _first_location_suggestion(self, box):
        """Wait for the location box's own dropdown and return its top option.

        The jobs page carries other ``role=option`` elements (the keyword
        typeahead, filter menus), so a document-wide query can land on one of
        those and either drop the geoId or navigate to the wrong region. The
        combobox pattern names its listbox in ``aria-controls`` (or the older
        ``aria-owns``); that scope is structural and locale-independent. With
        neither attribute, any listbox is the one that just opened under the
        typed text. The first match is the top suggestion, which is the one a
        person picks.
        """
        listbox_id = await box.get_attribute(
            "aria-controls"
        ) or await box.get_attribute("aria-owns")
        scope = f'[id="{listbox_id}"]' if listbox_id else "[role=listbox]"
        try:
            return await self._session.page.wait_for_selector(
                f"{scope} [role=option]", timeout=TYPEAHEAD_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return None

    async def resolve_company_urn(self, name_or_urn: str) -> str:
        """Resolve a company name, slug or URL to LinkedIn's numeric company id.

        People search's ``currentCompany`` facet filters on the numeric URN
        only (``"1115"`` for SAP); a name in the URL is accepted and ignored.
        The id is public but only on the company's own About page, in the
        "See all employees" anchor that ``link_metadata`` already reads as a
        ``company_urn`` reference (the same one ``get_company_profile``
        returns). So a name costs a company search to find the slug, then the
        About page to read the id; a ``/company/<slug>`` URL skips the search.

        Cache-first: an all-digit input is returned as is, then the
        per-resolver cache, then the on-disk company cache (populated by the
        enrichment tools and by this method), so a repeated company in a batch
        resolves at most once and a company already researched never
        navigates at all. A disk record that knows only the page URL skips
        the search and goes straight to About.

        A search hit counts only when its name normalises to the query: the
        top card is often a promoted page for another company. A page of
        candidates none of which match raises, naming their slugs.

        Raises ``FilterValidationError`` when nothing resolves. A clean miss
        is remembered for the batch; a throttled or failed lookup is not.
        """
        if re.fullmatch(r"[0-9]+", name_or_urn):
            return name_or_urn

        key = name_or_urn.strip().casefold()
        if key in self._company_urn_cache:
            urn = self._company_urn_cache[key]
            if urn:
                return urn
            raise FilterValidationError(self._company_unresolved_message(name_or_urn))

        # A URL names the page outright; anything else is a name to search for.
        slug = (
            normalize_company_identifier(name_or_urn)
            if "/company/" in name_or_urn
            else None
        )
        lookup = slug or name_or_urn.strip()

        if self._company_cache is None:
            self._company_cache = CompanyCache()
        record = self._company_cache.get(lookup)
        if record is not None:
            if record.company_urn:
                self._company_urn_cache[key] = record.company_urn
                return record.company_urn
            # A search-sourced record (enrich_companies) knows the page but
            # not the id: the slug is in the URL, so skip the search.
            if slug is None and "/company/" in record.linkedin_url:
                slug = normalize_company_identifier(record.linkedin_url)

        urn: str | None = None
        throttled = False
        failed = False
        searched = False
        cache_name = lookup
        if slug is None:
            search_url = (
                "https://www.linkedin.com/search/results/companies/"
                f"?keywords={quote_plus(lookup)}"
            )
            # A batch resolves names back to back, so the previous name's
            # About page and this search are consecutive navigations.
            if self.navigated:
                await self._session.pace(nav_delay())
            extracted = await self._capture.capture(
                search_url,
                "search_results",
                CapturePlan(CaptureMode.SEARCH_RESULTS),
            )
            searched = True
            self.navigated = True
            throttled = extracted.text == RATE_LIMITED_SECTION_TEXT
            failed = extracted.error is not None
            hits = parse_search_results([dict(ref) for ref in extracted.references])
            # Never take the top card on position alone: it is often a
            # promoted "Page by <Company>" for a different company (see
            # ``parse_search_results``). Only a card whose name normalises to
            # the query is the query.
            wanted = normalize_company_name(lookup)
            hit = next(
                (h for h in hits if normalize_company_name(h["name"]) == wanted),
                None,
            )
            if hit is None and hits and not throttled and not failed:
                self._company_urn_cache[key] = ""
                raise FilterValidationError(
                    f"Could not resolve company {name_or_urn!r}: no company "
                    f"search card is named that. Candidates: "
                    f"{[h['slug'] for h in hits]!r}. Pass the intended one as "
                    f"https://www.linkedin.com/company/<slug>/ instead."
                )
            if hit is not None:
                slug = hit["slug"]
                cache_name = hit["name"]
                # ponytail: a live check may collapse this to one navigation.
                # If the top card carries its own "See all employees" anchor,
                # the id is already here; the first company_urn reference that
                # follows the top card's link and precedes the next card's is
                # that card's. Unverified live, so the About page below stays
                # the fallback rather than the other way round.
                if hit is hits[0]:
                    urn = _company_urn_of_first_card(extracted.references)

        if slug is not None and urn is None:
            if searched or self.navigated:
                await self._session.pace(nav_delay())
            about = await self._capture.extract_page(
                company_page_url(slug, "/about/"), "about"
            )
            self.navigated = True
            throttled = throttled or about.text == RATE_LIMITED_SECTION_TEXT
            failed = failed or about.error is not None
            for ref in about.references:
                if ref["kind"] == "company_urn" and ref.get("value"):
                    urn = str(ref["value"])
                    break

        if not urn:
            # Only a clean miss is remembered; a throttled or failed lookup
            # may succeed on retry and must not poison the batch.
            if not throttled and not failed:
                self._company_urn_cache[key] = ""
            raise FilterValidationError(
                self._company_unresolved_message(name_or_urn, throttled=throttled)
            )
        self._company_urn_cache[key] = urn
        # The write-back is an optimisation, not the result: ``_path`` refuses
        # a name that normalises to nothing (punctuation only) with ValueError.
        try:
            self._company_cache.record_firmographics(
                cache_name,
                datetime.now().astimezone(),
                source="search",
                linkedin_url=company_page_url(slug) if slug else "",
                company_urn=urn,
            )
        except (OSError, ValueError) as e:
            logger.warning("Could not cache company urn for %r: %s", cache_name, e)
        return urn

    @staticmethod
    def _company_unresolved_message(name: str, *, throttled: bool = False) -> str:
        why = (
            "LinkedIn throttled the lookup; retry later"
            if throttled
            else "no company search hit or About page yielded an id"
        )
        return (
            f"Could not resolve company {name!r} to a LinkedIn company URN "
            f"({why}). Pass the numeric id instead: get_company_profile "
            f'exposes it under references["about"] as kind "company_urn".'
        )
