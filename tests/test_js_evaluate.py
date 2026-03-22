"""Integration tests for JS evaluate strings using patchright + HTML fixtures.

These tests load minimal HTML fixtures into a real browser page and run the
actual JS extraction code from scraping/posts.py, catching selector bugs
that unit tests with mocked page.evaluate cannot detect.
"""

from pathlib import Path

import pytest

from linkedin_mcp_server.scraping.posts import (
    _JS_EXTRACT_COMMENTS,
    _JS_EXTRACT_FEED_POSTS,
    _JS_EXTRACT_MY_POSTS,
    _JS_EXTRACT_NOTIFICATIONS,
)

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.integration


@pytest.fixture
async def page():
    from patchright.async_api import async_playwright

    pw = await async_playwright().start()
    b = await pw.chromium.launch(headless=True)
    p = await b.new_page()
    yield p
    await p.close()
    await b.close()
    await pw.stop()


async def _load_fixture(page, name: str):
    html = (FIXTURES / name).read_text()
    await page.set_content(html)


# ---------------------------------------------------------------------------
# get_my_recent_posts JS
# ---------------------------------------------------------------------------


class TestMyRecentPostsJS:
    async def test_extracts_post_content_not_author_name(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_MY_POSTS, 10)
        items = result["items"]
        assert len(items) == 2
        assert "This is a LinkedIn post" in items[0]["text_preview"]
        assert "John Doe" not in items[0]["text_preview"]

    async def test_extracts_urn_from_data_attribute(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_MY_POSTS, 10)
        items = result["items"]
        assert items[0]["post_id"] == "urn:li:activity:1234567890"
        assert "1234567890" in items[0]["post_url"]

    async def test_extracts_created_at_from_time_element(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_MY_POSTS, 10)
        items = result["items"]
        assert items[0]["created_at"] == "2026-03-22T10:00:00Z"

    async def test_respects_limit(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_MY_POSTS, 1)
        assert len(result["items"]) == 1

    async def test_empty_main_returns_empty(self, page):
        await page.set_content("<html><body><main></main></body></html>")
        result = await page.evaluate(_JS_EXTRACT_MY_POSTS, 10)
        assert result["items"] == []


# ---------------------------------------------------------------------------
# get_feed_posts JS
# ---------------------------------------------------------------------------


class TestFeedPostsJS:
    async def test_extracts_post_content_not_author_name(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_FEED_POSTS, 10)
        items = result["items"]
        assert len(items) == 2
        assert "This is a LinkedIn post" in items[0]["text_preview"]

    async def test_extracts_author_info(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_FEED_POSTS, 10)
        items = result["items"]
        assert items[0]["author_name"] == "John Doe"
        assert "/in/johndoe/" in items[0]["author_url"]

    async def test_author_url_normalized(self, page):
        await _load_fixture(page, "feed_post.html")
        result = await page.evaluate(_JS_EXTRACT_FEED_POSTS, 10)
        items = result["items"]
        assert items[0]["author_url"].startswith("https://www.linkedin.com")

    async def test_deduplicates_by_urn(self, page):
        await page.set_content("""<html><body><main>
            <article data-urn="urn:li:activity:111">
                <div dir="ltr">Post content that is long enough to be a real preview text.</div>
            </article>
            <article data-urn="urn:li:activity:111">
                <div dir="ltr">Duplicate should be skipped by the seen set.</div>
            </article>
        </main></body></html>""")
        result = await page.evaluate(_JS_EXTRACT_FEED_POSTS, 10)
        assert len(result["items"]) == 1


# ---------------------------------------------------------------------------
# get_post_comments JS
# ---------------------------------------------------------------------------


class TestCommentsJS:
    async def test_extracts_top_level_comments(self, page):
        await _load_fixture(page, "comments.html")
        result = await page.evaluate(_JS_EXTRACT_COMMENTS, "")
        # Should find Alice (top-level), Bob, Charlie — not the nested duplicate
        author_names = [c["author_name"] for c in result]
        assert any("Alice" in n for n in author_names if n)
        assert any("Bob" in n for n in author_names if n)

    async def test_truly_nested_comment_skipped(self, page):
        """Inner div whose parent also has data-urn*=urn:li:comment is skipped."""
        await _load_fixture(page, "comments.html")
        result = await page.evaluate(_JS_EXTRACT_COMMENTS, "")
        # The innermost div (line 11) is nested inside another comment-urn div
        # (line 10), so closest() filter skips it. But the outer wrapper (line 10)
        # is a sibling of line 4 — both are top-level. JS returns both;
        # Python dedup by comment_id removes the duplicate.
        urns = [c["comment_id"] for c in result]
        count_7001 = sum(1 for u in urns if u and "7001" in u)
        # JS returns 2 (outer wrapper + original), Python dedup reduces to 1
        assert count_7001 == 2

    async def test_comment_id_extracted(self, page):
        await _load_fixture(page, "comments.html")
        result = await page.evaluate(_JS_EXTRACT_COMMENTS, "")
        ids = [c["comment_id"] for c in result]
        assert any("7001" in str(i) for i in ids if i)
        assert any("7002" in str(i) for i in ids if i)

    async def test_has_reply_from_author_detected(self, page):
        await _load_fixture(page, "comments.html")
        result = await page.evaluate(_JS_EXTRACT_COMMENTS, "the author")
        charlie_comment = [
            c for c in result if c.get("author_name") and "Charlie" in c["author_name"]
        ]
        assert len(charlie_comment) == 1
        assert charlie_comment[0]["has_reply_from_author"] is True

    async def test_empty_main_returns_empty(self, page):
        await page.set_content("<html><body><main></main></body></html>")
        result = await page.evaluate(_JS_EXTRACT_COMMENTS, "")
        assert result == []


# ---------------------------------------------------------------------------
# get_notifications JS
# ---------------------------------------------------------------------------


class TestNotificationsJS:
    async def test_reaction_type_detected(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        reaction_items = [i for i in result if i["type"] == "reaction"]
        assert len(reaction_items) >= 1
        assert any("reacted" in i["text"] for i in reaction_items)

    async def test_comment_type_detected(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        comment_items = [i for i in result if i["type"] == "comment"]
        assert len(comment_items) >= 1
        assert any("commented" in i["text"] for i in comment_items)

    async def test_view_type_detected(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        view_items = [i for i in result if i["type"] == "view"]
        assert len(view_items) >= 1

    async def test_status_reachable_stripped(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        for item in result:
            assert not item["text"].startswith("Status is")

    async def test_timestamp_from_time_element(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        first = result[0]
        assert first["created_at"] is not None

    async def test_link_normalized(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        for item in result:
            if item["link"]:
                assert item["link"].startswith("https://")

    async def test_respects_max_items(self, page):
        await _load_fixture(page, "notifications.html")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 1)
        assert len(result) == 1

    async def test_empty_main_returns_empty(self, page):
        await page.set_content("<html><body><main></main></body></html>")
        result = await page.evaluate(_JS_EXTRACT_NOTIFICATIONS, 20)
        assert result == []
