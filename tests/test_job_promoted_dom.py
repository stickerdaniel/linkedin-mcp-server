"""Browser-DOM tests for reading promoted results and what sits above a notice.

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

from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

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
            # On a LinkedIn address, because the reads refuse any other
            # page, and `set_content` keeps the address it replaces.
            await page.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(content_type="text/html", body=""),
            )
            await page.goto("https://www.linkedin.com/jobs/search/")
            yield page
        finally:
            await browser.close()


def _reader(page) -> JobPageReader:
    session = PageSession(page)
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


_NOTICE = (
    "We found more results related to your search that may not be exact "
    "matches, but could still be a great fit."
)


def _redesigned_card(job_id: str, title: str, *, promoted: bool = False) -> str:
    footer = "<p>Promoted</p>" if promoted else "<p>1 week ago</p>"
    return (
        f'<div componentkey="job-card-component-ref-{job_id}">'
        f"<p>{title}</p><p>Acme</p>{footer}</div>"
    )


class TestOrganicBeforeNotice:
    async def _count(self, page, cards: str) -> int | None:
        await page.set_content(f'<main><div style="{_SCROLLABLE}">{cards}</div></main>')
        return await _reader(page)._count_organic_before_notice(_NOTICE, "Promoted")

    async def test_only_promoted_cards_above_the_notice_count_zero(self, dom_page):
        """The nonsense-query shape: ads, the notice, then unrelated jobs."""
        cards = (
            _redesigned_card("501", "Success Engineer", promoted=True)
            + _redesigned_card("502", "Data Engineer", promoted=True)
            + f"<div><p>{_NOTICE}</p></div>"
            + _redesigned_card("503", "Solutions Architect")
        )

        assert await self._count(dom_page, cards) == 0

    async def test_unpromoted_cards_above_the_notice_are_counted(self, dom_page):
        cards = (
            _redesigned_card("601", "Forward Deployed Engineer")
            + _redesigned_card("602", "AI Engineer", promoted=True)
            + _redesigned_card("603", "Forward Deployed Engineer - L5")
            + f"<div><p>{_NOTICE}</p></div>"
            + _redesigned_card("604", "Solutions Architect")
        )

        assert await self._count(dom_page, cards) == 2

    async def test_a_notice_split_across_inline_elements_is_found(self, dom_page):
        """The visible line is whole while its text sits in several nodes."""
        head, tail = _NOTICE.split(", but ")
        cards = (
            _redesigned_card("801", "Success Engineer", promoted=True)
            + f"<div><p><span>{head}</span>, <b>but</b> {tail}</p></div>"
            + _redesigned_card("802", "Solutions Architect")
        )

        assert await self._count(dom_page, cards) == 0

    async def test_a_page_without_the_notice_reads_as_none(self, dom_page):
        """Not zero, which would discard a page of matches as a no-match."""
        cards = _redesigned_card("701", "Python Developer")

        assert await self._count(dom_page, cards) is None
