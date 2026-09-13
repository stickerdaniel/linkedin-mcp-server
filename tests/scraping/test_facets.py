"""Tests for the facet resolver behind people search."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.facets import (
    GEO_ID_PATTERN,
    LOCATION_BOX_SELECTOR,
    TYPEAHEAD_TIMEOUT_MS,
    FacetResolver,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


def _resolver(page) -> FacetResolver:
    """Wire the resolver the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    capture = SectionCapture(session, navigator, PageContentReader(session))
    return FacetResolver(session, navigator, capture)


def _location_box(**attrs: str | None) -> AsyncMock:
    """A location input whose ARIA attributes answer from ``attrs``."""
    box = AsyncMock()
    box.get_attribute = AsyncMock(side_effect=lambda name: attrs.get(name))
    return box


def _typeahead(page, box, suggestion=None):
    """Drive the page like the jobs-search typeahead: the location box answers
    the selector scan, the dropdown answers the scoped wait, and selecting a
    suggestion is what moves the URL."""
    page.query_selector = AsyncMock(return_value=box)
    page.wait_for_selector = AsyncMock(return_value=suggestion)
    page.wait_for_url = AsyncMock()
    return patch(
        "linkedin_mcp_server.scraping.facets.human_type", new_callable=AsyncMock
    )


class TestResolveGeoUrn:
    async def test_resolve_geo_urn_reads_geoid_and_caches(self, mock_page):
        """Drives the dropdown once: types the name, clicks the top suggestion,
        reads geoId from the URL, and caches it (second call does not re-drive)."""
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": "location-listbox"})
        suggestion = AsyncMock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=106155005&foo=1"
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ) as goto,
            _typeahead(mock_page, box, suggestion) as typed,
        ):
            first = await resolver.resolve_geo_urn("Egypt")
            assert first == "106155005"
            assert resolver._geo_cache["egypt"] == "106155005"

            # Second call (case-insensitive) is served from cache: the dropdown
            # is not driven again, so no further navigation happens.
            assert await resolver.resolve_geo_urn("EGYPT") == "106155005"
            assert goto.await_count == 1

        goto.assert_awaited_once_with("https://www.linkedin.com/jobs/search/?keywords=")
        # One structural query, no text fallback: an ``aria-label`` match
        # would be the locale's word for "location" and miss elsewhere.
        mock_page.query_selector.assert_awaited_once_with(LOCATION_BOX_SELECTOR)
        assert "aria-label" not in LOCATION_BOX_SELECTOR
        box.fill.assert_awaited_once_with("")
        typed.assert_awaited_once_with(mock_page, "Egypt")
        suggestion.click.assert_awaited_once()

    async def test_suggestion_is_scoped_to_the_location_listbox(self, mock_page):
        """The jobs page carries other ``role=option`` elements; only the listbox
        the location input names in ``aria-controls`` may answer."""
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": "location-listbox"})
        mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=1"
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            _typeahead(mock_page, box, AsyncMock()),
        ):
            assert await resolver.resolve_geo_urn("Egypt") == "1"

        mock_page.wait_for_selector.assert_awaited_once_with(
            '[id="location-listbox"] [role=option]', timeout=TYPEAHEAD_TIMEOUT_MS
        )

    async def test_aria_owns_names_the_listbox_when_aria_controls_is_absent(
        self, mock_page
    ):
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": None, "aria-owns": "owned-listbox"})
        mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=1"
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            _typeahead(mock_page, box, AsyncMock()),
        ):
            assert await resolver.resolve_geo_urn("Egypt") == "1"

        mock_page.wait_for_selector.assert_awaited_once_with(
            '[id="owned-listbox"] [role=option]', timeout=TYPEAHEAD_TIMEOUT_MS
        )

    async def test_without_aria_scope_any_listbox_option_is_taken(self, mock_page):
        """No ``aria-controls``/``aria-owns``: the option still has to sit inside a
        listbox, never be a bare ``role=option`` anywhere on the page."""
        resolver = _resolver(mock_page)
        box = _location_box()
        mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=1"
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            _typeahead(mock_page, box, AsyncMock()),
        ):
            assert await resolver.resolve_geo_urn("Egypt") == "1"

        mock_page.wait_for_selector.assert_awaited_once_with(
            "[role=listbox] [role=option]", timeout=TYPEAHEAD_TIMEOUT_MS
        )

    async def test_geoid_is_read_only_after_the_url_carries_it(self, mock_page):
        """Selecting a suggestion navigates; the id is read after that navigation
        lands, not from whatever the URL says the instant after the click."""
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": "location-listbox"})
        suggestion = AsyncMock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords="

        def _navigated(*args, **kwargs):
            mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=104305776"

        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            _typeahead(mock_page, box, suggestion),
        ):
            mock_page.wait_for_url.side_effect = _navigated
            assert await resolver.resolve_geo_urn("United Arab Emirates") == "104305776"

        mock_page.wait_for_url.assert_awaited_once_with(
            GEO_ID_PATTERN, timeout=TYPEAHEAD_TIMEOUT_MS
        )

    async def test_url_never_carrying_geoid_is_a_cached_miss(self, mock_page):
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": "location-listbox"})
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords="
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            _typeahead(mock_page, box, AsyncMock()),
        ):
            mock_page.wait_for_url.side_effect = PlaywrightTimeoutError("no geoId")
            assert await resolver.resolve_geo_urn("Atlantis") is None

        assert resolver._geo_cache["atlantis"] == ""

    async def test_no_suggestion_is_a_cached_miss(self, mock_page):
        resolver = _resolver(mock_page)
        box = _location_box(**{"aria-controls": "location-listbox"})
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords="
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ) as goto,
            _typeahead(mock_page, box),
        ):
            # The location box is found; the dropdown then never opens.
            mock_page.wait_for_selector.side_effect = PlaywrightTimeoutError("empty")
            assert await resolver.resolve_geo_urn("Nowhereland") is None
            assert await resolver.resolve_geo_urn("nowhereland") is None

        assert resolver._geo_cache["nowhereland"] == ""
        assert goto.await_count == 1
        mock_page.wait_for_url.assert_not_awaited()

    async def test_no_location_box_is_a_miss_without_typing(self, mock_page):
        resolver = _resolver(mock_page)
        mock_page.query_selector = AsyncMock(return_value=None)
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ),
            patch(
                "linkedin_mcp_server.scraping.facets.human_type",
                new_callable=AsyncMock,
            ) as typed,
        ):
            assert await resolver.resolve_geo_urn("Egypt") is None

        typed.assert_not_awaited()
        # A missing box is a miss, not a cue to widen the scan to text.
        mock_page.query_selector.assert_awaited_once_with(LOCATION_BOX_SELECTOR)
