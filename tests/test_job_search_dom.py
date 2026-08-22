"""Browser-DOM tests for LinkedIn job search cards."""

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.utils import scroll_job_sidebar
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

pytestmark = pytest.mark.browser_dom


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


class TestJobSearchCards:
    async def test_extracts_redesigned_cards(self, dom_page):
        await dom_page.set_content(
            """
            <main>
              <div componentkey="job-card-component-ref-111" role="button">One</div>
              <div componentkey="job-card-component-ref-222" role="button">Two</div>
            </main>
            """
        )

        assert await LinkedInExtractor(dom_page)._extract_job_ids() == ["111", "222"]

    async def test_deduplicates_legacy_and_redesigned_cards(self, dom_page):
        await dom_page.set_content(
            """
            <main>
              <a href="https://www.linkedin.com/jobs/view/111/">One</a>
              <div componentkey="job-card-component-ref-111" role="button">One</div>
              <div componentkey="job-card-component-ref-222" role="button">Two</div>
            </main>
            """
        )

        assert await LinkedInExtractor(dom_page)._extract_job_ids() == ["111", "222"]

    async def test_scroll_finds_redesigned_cards(self, dom_page):
        await dom_page.set_content(
            """
            <main style="height: 30px; overflow-y: scroll">
              <div componentkey="job-card-component-ref-111" role="button">One</div>
              <div style="height: 100px"></div>
            </main>
            """
        )

        await scroll_job_sidebar(dom_page, pause_time=0, max_scrolls=1)

        scroll_top = await dom_page.evaluate("document.querySelector('main').scrollTop")
        assert scroll_top > 0
