"""Browser-DOM tests for the post-boundary read in ``linkedin/feed.py``.

The unit suite mocks ``page.evaluate``, so ``FEED_POST_BOUNDARIES_JS`` never
runs there: boundary selection, nesting, URN and permalink reading and the
ambiguity verdict are asserted as source text and nothing else. These cases
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

    async def test_a_nested_boundary_is_read_once_at_the_outer_post(self, dom_page):
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

        # One post is emitted for the outer boundary — but two post URNs
        # inside it cannot name one post, so the identity is ambiguous
        # rather than attributed to the outer container.
        assert len(result["posts"]) == 1
        assert result["posts"][0]["urn"] is None
        assert result["posts"][0]["ambiguous"] is True


class TestBoundaryPermalinksAgainstRealDom:
    async def test_the_single_post_anchor_inside_a_boundary_is_its_url(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>A post with a timestamp link</p>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:7214916299838103554/">3d</a>
              <a href="https://www.linkedin.com/in/ada-lovelace/">Ada</a>
            </article>
            """,
        )

        assert result["posts"][0]["url"] == (
            "/feed/update/urn:li:activity:7214916299838103554/"
        )
        assert result["posts"][0]["ambiguous"] is False

    async def test_a_posts_slug_anchor_is_accepted_the_same_way(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>A post whose card links the /posts/ form</p>
              <a href="https://www.linkedin.com/posts/ada_engines-ugcPost-7214916299838103554-abc/">Full post</a>
            </article>
            """,
        )

        assert result["posts"][0]["url"] == (
            "/posts/ada_engines-ugcPost-7214916299838103554-abc/"
        )

    async def test_two_different_post_anchors_mark_the_boundary_ambiguous(
        self, dom_page
    ):
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>Original and embedded link different posts</p>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:7214916299838103554/">3d</a>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:9999999999999999999/">
                embedded
              </a>
            </article>
            """,
        )

        assert result["posts"][0]["url"] is None
        assert result["posts"][0]["ambiguous"] is True

    async def test_the_permalinks_urn_names_a_post_without_attributes(self, dom_page):
        # Cards without data-urn still carry their identity in the
        # /feed/update/<urn>/ permalink they link.
        result = await snapshot(
            dom_page,
            """
            <article>
              <p>Identity lives in the timestamp link</p>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:7214916299838103554/">3d</a>
            </article>
            """,
        )

        assert result["posts"][0]["urn"] == ("urn:li:activity:7214916299838103554")

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
        assert result["posts"][0]["ambiguous"] is False

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

    async def test_urn_and_link_disagreeing_marks_the_post_ambiguous(self, dom_page):
        result = await snapshot(
            dom_page,
            """
            <article data-urn="urn:li:activity:111">
              <p>Conflicting post</p>
              <a href="https://www.linkedin.com/feed/update/urn:li:activity:222/">
                1w
              </a>
            </article>
            """,
        )

        assert result["posts"][0]["ambiguous"] is True
        assert result["posts"][0]["url"] is None
