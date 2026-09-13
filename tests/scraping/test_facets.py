"""Tests for the facet resolver behind people search."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession


def _resolver(page) -> FacetResolver:
    """Wire the resolver the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    capture = SectionCapture(session, navigator, PageContentReader(session))
    return FacetResolver(session, navigator, capture)


def _sleep():
    return patch(
        "linkedin_mcp_server.scraping.session.asyncio.sleep", new_callable=AsyncMock
    )


class TestResolveGeoUrn:
    async def test_resolve_geo_urn_reads_geoid_and_caches(self, mock_page):
        """Drives the dropdown once: types the name, clicks the top suggestion,
        reads geoId from the URL, and caches it (second call does not re-drive)."""
        resolver = _resolver(mock_page)
        box = AsyncMock()
        suggestion = AsyncMock()
        # The location box answers the first selector; the dropdown the next.
        mock_page.query_selector = AsyncMock(side_effect=[box, suggestion])
        mock_page.url = "https://www.linkedin.com/jobs/search/?geoId=106155005&foo=1"
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ) as goto,
            patch(
                "linkedin_mcp_server.scraping.facets.human_type",
                new_callable=AsyncMock,
            ) as typed,
            _sleep(),
        ):
            first = await resolver.resolve_geo_urn("Egypt")
            assert first == "106155005"
            assert resolver._geo_cache["egypt"] == "106155005"

            # Second call (case-insensitive) is served from cache: the dropdown
            # is not driven again, so no further navigation happens.
            assert await resolver.resolve_geo_urn("EGYPT") == "106155005"
            assert goto.await_count == 1

        goto.assert_awaited_once_with("https://www.linkedin.com/jobs/search/?keywords=")
        box.fill.assert_awaited_once_with("")
        typed.assert_awaited_once_with(mock_page, "Egypt")
        suggestion.click.assert_awaited_once()

    async def test_no_suggestion_is_a_cached_miss(self, mock_page):
        resolver = _resolver(mock_page)
        box = AsyncMock()
        # The location box is found; the dropdown then offers nothing.
        mock_page.query_selector = AsyncMock(side_effect=[box, None])
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords="
        with (
            patch.object(
                resolver._navigator, "_goto_with_auth_checks", new_callable=AsyncMock
            ) as goto,
            patch(
                "linkedin_mcp_server.scraping.facets.human_type",
                new_callable=AsyncMock,
            ),
            _sleep(),
        ):
            assert await resolver.resolve_geo_urn("Nowhereland") is None
            assert await resolver.resolve_geo_urn("nowhereland") is None

        assert resolver._geo_cache["nowhereland"] == ""
        assert goto.await_count == 1

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
