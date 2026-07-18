"""Real-browser coverage for people-search pagination.

The browser route is fulfilled with deterministic LinkedIn-shaped HTML so the
test exercises production navigation, innerText extraction, reference building,
pagination, and early stopping without depending on a live account or locale.
"""

from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

pytestmark = pytest.mark.browser_dom


def _search_page(page_number: int) -> str:
    people = {
        1: [
            ("/in/jane-doe/", "Jane"),
            ("/in/alex-smith/", "Alex Smith"),
        ],
        2: [
            ("/in/jane-doe/", "Jane Doe"),
            ("/in/robin-lee/", "Robin Lee"),
        ],
        # Repeating only known profiles represents LinkedIn's terminal page.
        3: [
            ("/in/jane-doe/", "Jane Doe"),
            ("/in/robin-lee/", "Robin Lee"),
        ],
    }[page_number]
    cards = "".join(
        f'<article><a href="{url}">{name}</a><p>Software engineer</p></article>'
        for url, name in people
    )
    return f"""
        <!doctype html>
        <html>
          <head><title>People search page {page_number}</title></head>
          <body>
            <main>
              <h1>Results page {page_number}</h1>
              <p>People matching the requested role, location, and company filters.</p>
              <p>This deterministic content is long enough to represent hydrated search results.</p>
              {cards}
            </main>
          </body>
        </html>
    """


@pytest.fixture
async def routed_linkedin_page():
    """Real Chromium page with deterministic LinkedIn search responses."""
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")

        requested_pages: list[int] = []

        async def fulfill_search(route) -> None:
            query = parse_qs(urlparse(route.request.url).query)
            page_number = int(query.get("page", ["1"])[0])
            requested_pages.append(page_number)
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=_search_page(page_number),
            )

        await page.route(
            "https://www.linkedin.com/search/results/people/**", fulfill_search
        )
        try:
            yield page, requested_pages
        finally:
            await browser.close()


async def test_people_search_paginates_through_real_browser(routed_linkedin_page):
    """Drive production extraction until a page has no new person URLs."""
    page, requested_pages = routed_linkedin_page
    extractor = LinkedInExtractor(page)

    # Preserve real browser navigation and DOM work while removing only the
    # production courtesy delay between deterministic test pages.
    with patch("linkedin_mcp_server.scraping.extractor._NAV_DELAY", 0):
        result = await extractor.search_people(
            "software engineer",
            location="New York",
            max_pages=5,
        )

    assert requested_pages == [1, 2, 3]
    assert result["url"] == (
        "https://www.linkedin.com/search/results/people/"
        "?keywords=software+engineer&location=New+York"
    )
    assert result["sections"]["search_results"].count("\n---\n") == 2
    assert "Results page 1" in result["sections"]["search_results"]
    assert "Results page 2" in result["sections"]["search_results"]
    assert "Results page 3" in result["sections"]["search_results"]
    assert result["references"] == {
        "search_results": [
            {
                "kind": "person",
                "url": "/in/jane-doe/",
                "text": "Jane Doe",
                "context": "search result",
            },
            {
                "kind": "person",
                "url": "/in/alex-smith/",
                "text": "Alex Smith",
                "context": "search result",
            },
            {
                "kind": "person",
                "url": "/in/robin-lee/",
                "text": "Robin Lee",
                "context": "search result",
            },
        ]
    }
