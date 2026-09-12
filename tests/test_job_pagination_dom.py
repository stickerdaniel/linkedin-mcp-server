"""Browser-DOM tests for the two job pagination reads.

The unit suite mocks ``page.evaluate``, so the programs inside
``JobPageReader._get_total_search_pages`` and ``_get_total_list_pages`` never
execute there: a mocked return value asserts the Python around them and
nothing about the JS. These run the real ones against synthetic markup in
headless chromium.

Both are deliberate DOM exceptions, documented as such on the methods. That is
exactly why they need this file: a class LinkedIn renames must degrade to
``None`` and let pagination fall back to ``max_pages``, and only a real
document can show the difference between "no such element" and a crash.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.job_pages import JobPageReader
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def _reader(page) -> JobPageReader:
    """The page reader wired the way the facade does, over a real browser."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    return JobPageReader(session, navigator, PageContentReader(session))


class TestTotalSearchPages:
    async def test_reads_a_count_the_page_text_does_not_carry(self, dom_page):
        """The element is screen-reader only, which is why the class is read.

        ``display: none`` in the fixture as it is live, and the premise is
        asserted rather than assumed: the same document is asked for the text
        an ordinary extraction takes from ``<main>``, and the count is not in
        it. Note that the program reads ``textContent`` for a reason that is
        *not* visible here — Chromium's ``innerText`` falls back to
        ``textContent`` for an unrendered element, so swapping the two passes
        against this fixture. What it would break is a page that renders the
        state and hyphenates it, which no fixture here claims to model.
        """
        await dom_page.set_content(
            "<main>"
            "<p>Python Developer</p>"
            '<div class="jobs-search-pagination__page-state" style="display:none">'
            "  Page 2 of 7 "
            "</div>"
            "</main>"
        )
        visible = await dom_page.evaluate(
            "() => document.querySelector('main').innerText"
        )

        assert "of 7" not in visible
        assert await _reader(dom_page)._get_total_search_pages() == 7

    async def test_a_renamed_class_degrades_to_no_count(self, dom_page):
        """Pagination then falls back to ``max_pages`` instead of failing."""
        await dom_page.set_content(
            '<main><div class="jobs-search-pagination__state">Page 2 of 7</div></main>'
        )

        assert await _reader(dom_page)._get_total_search_pages() is None

    async def test_text_without_a_count_degrades_to_no_count(self, dom_page):
        await dom_page.set_content(
            '<main><div class="jobs-search-pagination__page-state">Page 2</div></main>'
        )

        assert await _reader(dom_page)._get_total_search_pages() is None


class TestTotalListPages:
    async def test_reads_the_last_numbered_pager_button(self, dom_page):
        """The largest numeral wins, not the last node in the list.

        LinkedIn's pager renders an ellipsis between the near pages and the
        final one, so the buttons are neither contiguous nor sorted by
        position in every state.
        """
        await dom_page.set_content(
            "<main><ul class='artdeco-pagination__pages'>"
            "<li><button>1</button></li>"
            "<li><button>2</button></li>"
            "<li><button>…</button></li>"
            "<li><button>9</button></li>"
            "<li><button>3</button></li>"
            "</ul></main>"
        )

        assert await _reader(dom_page)._get_total_list_pages() == 9

    async def test_no_pager_degrades_to_no_count(self, dom_page):
        await dom_page.set_content("<main><p>Nothing saved yet</p></main>")

        assert await _reader(dom_page)._get_total_list_pages() is None

    async def test_non_ascii_numerals_degrade_to_no_count(self, dom_page):
        """``parseInt`` cannot read them, and a wrong count is worse than none.

        Named on the method as the locale case it accepts losing. It has to
        stay a ``None`` rather than a ``NaN`` reaching Python as a float.
        """
        await dom_page.set_content(
            "<main><ul class='artdeco-pagination__pages'>"
            "<li><button>١</button></li>"
            "<li><button>٢</button></li>"
            "</ul></main>"
        )

        assert await _reader(dom_page)._get_total_list_pages() is None
