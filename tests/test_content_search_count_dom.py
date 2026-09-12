"""Browser-DOM tests for the content-search result count.

The unit suite mocks ``page.evaluate``, so ``_CONTENT_SEARCH_COUNT_JS`` never
executes there. These tests run it against a synthetic ``<main>`` in headless
chromium. The fixtures drive a synthetic container, so they are a claim about
the algorithm and not about LinkedIn's markup: the ancestor chain of a live
content-search card is unverified. Skipped automatically when chromium is
not installed; run locally after ``uv run patchright install chromium
--no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import _CONTENT_SEARCH_COUNT_JS

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

#: Nine ``/in/`` anchors standing in for a post body that @-mentions nine
#: people. Under the old distinct-href count these alone reached
#: ``max_posts=10`` before the first wheel.
MENTIONS = "".join(f'<a href="/in/mention-{i}/">@person {i}</a> ' for i in range(9))


def results(card_tag: str | None) -> str:
    """Two posts: the first mentions nine people, the second none.

    ``card_tag`` wraps each post in that element, or in a plain ``<div>``
    when None, which is the shape that has no card boundary to group by.
    """
    open_tag = f"<{card_tag}>" if card_tag else "<div>"
    close_tag = f"</{card_tag}>" if card_tag else "</div>"
    return f"""
    <body>
      <main>
        {open_tag}
          <a href="/in/author-one/?miniProfileUrn=x">Author One</a>
          <p>Thanks to {MENTIONS} for the launch.</p>
        {close_tag}
        {open_tag}
          <a href="/in/author-two/">Author Two</a>
          <p>No mentions here.</p>
        {close_tag}
      </main>
    </body>
    """


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


class TestContentSearchCount:
    async def test_mentions_inside_a_card_count_once(self, dom_page):
        """Ten anchors in one ``<li>`` are one card, not ten."""
        await dom_page.set_content(results("li"))

        assert await dom_page.evaluate(_CONTENT_SEARCH_COUNT_JS) == 2

    async def test_without_a_card_boundary_distinct_hrefs_stand_in(self, dom_page):
        """No ``li``/``article`` ancestor: the count is the old one, eleven
        distinct hrefs, which over-counts rather than returning zero."""
        await dom_page.set_content(results(None))

        assert await dom_page.evaluate(_CONTENT_SEARCH_COUNT_JS) == 11
