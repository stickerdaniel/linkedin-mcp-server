"""Browser-DOM tests for reading which job search results are promoted.

The unit suite mocks ``page.evaluate``, so the program behind
``JobPageReader._extract_promoted_job_ids`` never runs there. These run it
against synthetic markup in headless chromium. The markup is a claim about the
algorithm, not about LinkedIn: a scrollable results rail beside a scrollable
detail pane, the shape the rail rule is written for, with cards built the two
ways a job id is carried.

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
    session = ScrapingSession(page)
    return JobPageReader(session, PageNavigator(session), PageContentReader(session))


def _classic_card(job_id: str, title: str, footer: list[str]) -> str:
    items = "".join(f"<li>{item}</li>" for item in footer)
    return (
        f'<li><div><a href="/jobs/view/{job_id}/">{title}</a>'
        f"<p>Acme</p><ul>{items}</ul></div></li>"
    )


_SCROLLABLE = "overflow-y:auto;height:120px"


class TestPromotedJobIds:
    async def test_only_rail_cards_with_their_own_promoted_line(self, dom_page):
        """The pane repeats a rail job and adds a promoted one of its own.

        Neither is a result. The pane's "Promoted by hirer" line sits next to
        a rail job's permalink, and its similar job carries a bare "Promoted"
        line, so reading the document rather than the rail flags both.
        """
        rail = "".join(
            [
                _classic_card("301", "Data Engineer", ["Viewed", "Promoted"]),
                _classic_card("302", "Promoted Products Manager", ["Easy Apply"]),
                _classic_card("303", "Analyst", ["Promoted", "Easy Apply"]),
            ]
        )
        pane = (
            '<a href="/jobs/view/302/">Promoted Products Manager</a>'
            "<p>Promoted by hirer · Actively reviewing applicants</p>"
            '<div><a href="/jobs/view/999/">Similar job</a><p>Promoted</p></div>'
        )
        await dom_page.set_content(
            f'<main><div style="{_SCROLLABLE}"><ul>{rail}</ul></div>'
            f'<div style="{_SCROLLABLE}">{pane}</div></main>'
        )

        promoted = await _reader(dom_page)._extract_promoted_job_ids("Promoted")

        assert promoted == ["301", "303"]

    async def test_redesigned_cards_carry_the_id_in_componentkey(self, dom_page):
        cards = (
            '<div componentkey="job-card-component-ref-401">'
            "<p>Product Owner</p><p>Delos</p><p>Easy Apply</p><p>Promoted</p></div>"
            '<div componentkey="job-card-component-ref-402">'
            "<p>AI Product Manager</p><p>Modjo</p><p>2 weeks ago</p></div>"
        )
        await dom_page.set_content(
            f'<main><div style="{_SCROLLABLE}">{cards}</div></main>'
        )

        promoted = await _reader(dom_page)._extract_promoted_job_ids("Promoted")

        assert promoted == ["401"]
