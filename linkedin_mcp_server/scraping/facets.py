"""Resolution of free-text search facets to the ids LinkedIn filters on."""

from __future__ import annotations

import re

from linkedin_mcp_server.core.humanize import human_type
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


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
        box = None
        for sel in (
            "input[id*='jobs-search-box-location']",
            "input[aria-label='City, state, or zip code']",
            "input[aria-label*='location' i]",
        ):
            box = await page.query_selector(sel)
            if box:
                break

        geo_id: str | None = None
        if box is not None:
            await box.click()
            await box.fill("")
            # Type it like a person; the dropdown resolves as we type.
            await human_type(page, location)
            await self._session.pace(1.5)
            suggestion = await page.query_selector(
                ".basic-typeahead__selectable, [role=option]"
            )
            if suggestion is not None:
                await suggestion.click()
                await self._session.pace(1.0)
                match = re.search(r"[?&]geoId=(\d+)", page.url)
                if match:
                    geo_id = match.group(1)

        # Cache the outcome (including a miss) to avoid re-driving the dropdown.
        self._geo_cache[key] = geo_id or ""
        return geo_id
