"""Browser-DOM tests for the job posting readiness predicate in ``scraping/text.py``.

The unit suite mocks ``page.wait_for_function``, so the predicate never runs
there. These cases execute it in headless chromium against synthetic
documents, so each is a claim about the predicate and never about LinkedIn's
markup. Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import asyncio

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.text import JOB_POSTING_EN_US

#: Keep every test that launches Chromium on one worker; see
#: ``test_root_content_dom.py``.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

READY = JOB_POSTING_EN_US.readiness_expression()


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


async def test_holds_once_the_heading_is_its_own_line(dom_page):
    await dom_page.set_content(
        "<main><h1>Engineer</h1><h2> About the job </h2><p>Build things</p></main>"
    )
    assert await dom_page.evaluate(READY) is True


async def test_rejects_the_heading_words_inside_other_text(dom_page):
    await dom_page.set_content(
        "<main><h1>Engineer</h1>"
        "<p>Tell us what you like About the job in your cover letter</p></main>"
    )
    assert await dom_page.evaluate(READY) is False


async def test_rejects_a_heading_outside_main(dom_page):
    await dom_page.set_content("<header><h2>About the job</h2></header><main></main>")
    assert await dom_page.evaluate(READY) is False


async def test_rejects_a_document_without_main(dom_page):
    await dom_page.set_content("<div><h2>About the job</h2></div>")
    assert await dom_page.evaluate(READY) is False


async def test_a_wait_resolves_when_the_description_hydrates_late(dom_page):
    await dom_page.set_content("<main><h1>Engineer</h1><button>Apply</button></main>")
    waiting = asyncio.ensure_future(dom_page.wait_for_function(READY, timeout=5000))
    await asyncio.sleep(0.3)
    assert not waiting.done()

    await dom_page.evaluate(
        """() => {
            const panel = document.createElement('section');
            panel.innerHTML = '<h2>About the job</h2><p>Build things</p>';
            document.querySelector('main').appendChild(panel);
        }"""
    )
    await waiting
