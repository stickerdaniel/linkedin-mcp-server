"""Resolution of free-text search facets to the ids LinkedIn filters on."""

from __future__ import annotations

import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.humanize import human_type
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

# The jobs-search typeahead renders its suggestions from a network round trip
# and selecting one navigates. Both are bounded so a stalled page reads as a
# miss rather than hanging the tool call.
TYPEAHEAD_TIMEOUT_MS = 5000
GEO_ID_PATTERN = re.compile(r"[?&]geoId=(\d+)")
LOCATION_BOX_SELECTOR = "input[id*='jobs-search-box-location']"


class FacetResolver:
    """Resolve locations to ids, once per name per tool call.

    One resolver is built per facade, which is one per tool call, so the
    cache spans a batch of names and no more.
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
        are cached per resolver so a repeated region costs one resolution.
        """
        key = location.casefold()
        if key in self._geo_cache:
            return self._geo_cache[key] or None

        page = self._session.page
        await self._navigator._goto_with_auth_checks(
            "https://www.linkedin.com/jobs/search/?keywords="
        )
        # The id is structural; an ``aria-label`` fallback would carry the
        # locale's own words for "location", which is exactly the kind of text
        # match that reads as a miss on a non-English profile.
        box = await page.query_selector(LOCATION_BOX_SELECTOR)

        geo_id: str | None = None
        if box is not None:
            await box.click()
            await box.fill("")
            # Type it like a person; the dropdown resolves as we type.
            await human_type(page, location)
            suggestion = await self._first_location_suggestion(box)
            if suggestion is not None:
                await suggestion.click()
                try:
                    await page.wait_for_url(
                        GEO_ID_PATTERN, timeout=TYPEAHEAD_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    pass
                match = GEO_ID_PATTERN.search(page.url)
                if match:
                    geo_id = match.group(1)

        # Cache the outcome (including a miss) to avoid re-driving the dropdown.
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
