"""Browser-DOM tests for the post-boundary read in ``linkedin/feed.py``.

The unit suite mocks ``page.evaluate``, so ``FEED_POST_BOUNDARIES_JS`` never
runs there: boundary selection, nesting and URN ownership are asserted as source
text and nothing else. These cases
execute that program in headless chromium.

Every fixture drives a synthetic container, so each case is a claim about the
algorithm and never about LinkedIn's markup. Skipped automatically when
chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.feed import FEED_POST_BOUNDARIES_JS

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

BASE_URL = "https://www.linkedin.com/feed/"


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


async def snapshot(page: Any, main_body: str) -> dict[str, Any]:
    """Run the boundary read against `main_body` served from a feed address.

    A real LinkedIn document address is required: the program resolves
    permalinks against ``location.href``, and an ``about:blank`` origin would
    prove nothing about what a caller receives.
    """
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(
            content_type="text/html",
            body=f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Feed</title></head>
<body><main>{main_body}</main></body></html>""",
        ),
    )
    await page.goto(BASE_URL)
    return await page.evaluate(FEED_POST_BOUNDARIES_JS)


class TestBoundaryIdentityAgainstRealDom:
    async def test_each_post_boundary_pairs_its_urn_and_text(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article data-urn="urn:li:activity:7214916299838103554">
              <p>Ada writes about engines</p>
            </article>
            <article data-urn="urn:li:activity:7424761688761069570">
              <p>Grace on compilers</p>
            </article>
            """,
        )

        assert [p["urn"] for p in result["posts"]] == [
            "urn:li:activity:7214916299838103554",
            "urn:li:activity:7424761688761069570",
        ]
        assert [p["text"] for p in result["posts"]] == [
            "Ada writes about engines",
            "Grace on compilers",
        ]

    async def test_a_nested_post_does_not_dilute_the_outer_posts_urn(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article data-urn="urn:li:activity:7214916299838103554">
              <p>Outer post</p>
              <article data-urn="urn:li:activity:1111111111111111111">
                <p>Nested card</p>
              </article>
            </article>
            """,
        )

        # One post is emitted for the outer boundary, named by its own URN:
        # the nested card's URN belongs to the embedded post.
        assert len(result["posts"]) == 1
        assert result["posts"][0]["urn"] == "urn:li:activity:7214916299838103554"

    async def test_an_outer_post_never_borrows_a_nested_posts_urn(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>Outer commentary about another post</p>
              <article data-urn="urn:li:activity:7000000000000000001">
                <p>Quoted inner content</p>
              </article>
            </article>
            """,
        )

        assert len(result["posts"]) == 1
        assert result["posts"][0]["urn"] is None

    async def test_a_urn_on_a_wrapper_inside_the_article_names_it(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article>
              <div data-id="urn:li:activity:7214916299838103554">
                <p>A post keyed one level down</p>
              </div>
            </article>
            """,
        )

        assert result["posts"][0]["urn"] == "urn:li:activity:7214916299838103554"

    async def test_two_post_urns_of_its_own_name_no_post(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <div data-id="urn:li:activity:7214916299838103554">
              <span data-urn="urn:li:activity:9999999999999999999">x</span>
            </div>
            """,
        )

        assert result["posts"][0]["urn"] is None


class TestBoundaryAnchorsAgainstRealDom:
    async def test_a_lone_post_link_does_not_name_the_post(self, dom_page):
        # The only post link in a card can cite someone else's post, so a
        # link alone never names the boundary.
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>Outer post criticizing a different post</p>
              <a href="https://www.linkedin.com/posts/quoted_topic-ugcPost-7000000000000000001-xyz">
                A link to someone else's post
              </a>
            </article>
            """,
        )

        assert result["posts"][0]["urn"] is None

    async def test_non_post_urns_do_not_dilute_the_identity(self, dom_page):
        # Comment and profile URNs ride inside a card without being the
        # post's identity; only post kinds count.
        result = await snapshot(
            dom_page,
            """
            <article data-urn="urn:li:activity:7214916299838103554">
              <p>Post with reactions</p>
              <span data-urn="urn:li:fsd_profile:ACoAAABCD1234">Ada</span>
              <span data-urn="urn:li:comment:(ugcPost:1,2)">reply</span>
            </article>
            """,
        )

        assert result["posts"][0]["urn"] == ("urn:li:activity:7214916299838103554")

    async def test_data_id_names_a_post_the_same_as_data_urn(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <div data-id="urn:li:activity:7214916299838103554">
              <p>A card keyed by data-id</p>
            </div>
            """,
        )

        assert result["posts"][0]["urn"] == ("urn:li:activity:7214916299838103554")

    async def test_a_urn_with_a_link_to_another_post_keeps_its_own_urn(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article data-urn="urn:li:activity:111">
              <p>A post linking another one</p>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:222/">
                the other post
              </a>
            </article>
            """,
        )

        assert result["posts"][0]["urn"] == "urn:li:activity:111"
