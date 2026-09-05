# tests/test_root_content_dom.py
"""Browser-DOM tests for the raw content read in ``scraping/content.py``.

The unit suite mocks ``page.evaluate``, so the reader's JavaScript never runs
there: root selection, the anchor filter, href resolution and the heading
walk are all asserted as source text and nothing else. These cases execute
that program in headless chromium.

Every fixture here is a synthetic container, so each case is a claim about
the algorithm and never about LinkedIn's markup. Skipped automatically when
chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.session import ScrapingSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

BASE_URL = "https://www.linkedin.com/in/ada-lovelace/"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip -- the ``yield`` is outside it
    so an assertion failure or JS error in a test body is never swallowed
    into a skip.

    ``channel="chromium"`` names the browser this project installs. Without
    it Playwright picks the *binary* from the ``headless`` flag alone and
    asks for ``chromium-headless-shell``, which nothing here installs since
    the setup moved to ``--no-shell``.
    """
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


async def read(page: Any, html: str, selectors: list[str]) -> dict[str, Any]:
    """Run the production read against `html` served from a LinkedIn origin.

    The document needs a real origin because the reader reports ``anchor.href``,
    which the browser resolves against the document address. Served from
    ``about:blank`` a relative path resolves to ``about:blank`` and the case
    would prove nothing about what a caller receives.
    """
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    await page.goto(BASE_URL)
    reader = PageContentReader(ScrapingSession(page))
    return await reader._extract_root_content(selectors)


def document(main_body: str, *, outside: str = "Outside the root") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Content</title></head>
  <body>
    <p>{outside}</p>
    <main>{main_body}</main>
  </body>
</html>
"""


class TestRootSelectionAgainstRealDom:
    async def test_the_first_matching_selector_bounds_the_read(self, dom_page):
        result = await read(
            dom_page,
            document("<h1>Root heading</h1><p>Inside the root</p>"),
            ["#absent", "main"],
        )

        assert result["source"] == "root"
        assert "Inside the root" in result["text"]
        assert "Outside the root" not in result["text"]

    async def test_no_matching_selector_falls_back_to_the_body(self, dom_page):
        result = await read(
            dom_page,
            document("<p>Inside the root</p>"),
            ["#absent"],
        )

        assert result["source"] == "body"
        assert "Outside the root" in result["text"]


class TestAnchorFilteringAgainstRealDom:
    async def test_placeholder_anchors_are_dropped_and_the_rest_resolved(
        self, dom_page
    ):
        result = await read(
            dom_page,
            document(
                """
                <h1>Root heading</h1>
                <a href="">Empty</a>
                <a href="   ">Blank</a>
                <a href="#">Placeholder</a>
                <a href="#experience">Fragment</a>
                <a href="/in/grace-hopper/">Relative</a>
                <a>No href at all</a>
                """
            ),
            ["main"],
        )

        assert [reference["href"] for reference in result["references"]] == [
            "#experience",
            "https://www.linkedin.com/in/grace-hopper/",
        ]
        assert [reference["text"] for reference in result["references"]] == [
            "Fragment",
            "Relative",
        ]

    async def test_label_attributes_and_placement_flags_travel_with_the_anchor(
        self, dom_page
    ):
        result = await read(
            dom_page,
            document(
                """
                <h1>Root heading</h1>
                <nav><a href="/feed/" aria-label="Home feed">Feed</a></nav>
                <article><a href="/posts/one/" title="First post">Post</a></article>
                <footer><a href="/legal/">Legal</a></footer>
                """
            ),
            ["main"],
        )
        by_text = {reference["text"]: reference for reference in result["references"]}

        assert by_text["Feed"]["aria_label"] == "Home feed"
        assert by_text["Feed"]["in_nav"] is True
        assert by_text["Feed"]["in_article"] is False
        assert by_text["Post"]["title"] == "First post"
        assert by_text["Post"]["in_article"] is True
        assert by_text["Legal"]["in_footer"] is True
        assert by_text["Legal"]["in_nav"] is False

    async def test_the_anchor_cap_bounds_a_page_that_links_to_everything(
        self, dom_page
    ):
        anchors = "".join(
            f'<a href="/in/person-{index}/">Person {index}</a>' for index in range(505)
        )
        result = await read(dom_page, document(anchors), ["main"])

        assert len(result["references"]) == 500
        assert result["references"][-1]["text"] == "Person 499"


def sibling_document(fillers: int) -> str:
    """A heading and an anchor separated by `fillers` sibling elements.

    Kept flat under the root on purpose: nested inside a section the heading
    would be reachable through the ancestor walk as well, and the sibling
    bound this measures would hold whatever its limit was.
    """
    filler = "".join(f"<div>Filler {index}</div>" for index in range(fillers))
    return document(
        "<h1>Root heading</h1><h2>Skills</h2>"
        f'{filler}<div><a href="/company/acme/">Acme</a></div>'
    )


class TestHeadingAttributionAgainstRealDom:
    async def test_an_ancestor_container_labels_an_anchor_nested_below_it(
        self, dom_page
    ):
        result = await read(
            dom_page,
            document(
                """
                <h1>Root heading</h1>
                <section>
                  <h2>Experience</h2>
                  <ul><li><a href="/company/acme/">Acme</a></li></ul>
                </section>
                """
            ),
            ["main"],
        )

        assert [reference["heading"] for reference in result["references"]] == [
            "Experience"
        ]

    async def test_a_heading_three_siblings_back_still_labels_the_anchor(
        self, dom_page
    ):
        result = await read(dom_page, sibling_document(2), ["main"])

        assert [reference["heading"] for reference in result["references"]] == [
            "Skills"
        ]

    async def test_a_heading_four_siblings_back_is_out_of_reach(self, dom_page):
        result = await read(dom_page, sibling_document(3), ["main"])

        assert [reference["heading"] for reference in result["references"]] == [""]
