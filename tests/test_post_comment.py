"""Tests for post_comment tool and PostSearch comment workflow."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.linkedin.posts import PostSearch


class TestPostCommentNormalization(unittest.TestCase):
    def setUp(self):
        self.post_search = PostSearch(capture=MagicMock())

    def test_normalize_activity_url(self):
        url = "https://www.linkedin.com/posts/kevinlehtiniitty_everyone-budgets-for-the-integration-when-activity-7498363673741209600-sC_A"
        normalized = self.post_search.normalize_post_url(url)
        # Full post URLs are preserved directly to avoid breaking share or custom slug routes
        self.assertEqual(normalized, url)

    def test_normalize_activity_slug_fragment(self):
        slug = "activity-7498363673741209600"
        normalized = self.post_search.normalize_post_url(slug)
        self.assertEqual(
            normalized,
            "https://www.linkedin.com/feed/update/urn:li:activity:7498363673741209600/",
        )

    def test_normalize_bare_activity_urn(self):
        urn = "urn:li:activity:7498363673741209600"
        normalized = self.post_search.normalize_post_url(urn)
        self.assertEqual(
            normalized,
            "https://www.linkedin.com/feed/update/urn:li:activity:7498363673741209600/",
        )

    def test_normalize_bare_slug(self):
        slug = "some-post-slug-123"
        normalized = self.post_search.normalize_post_url(slug)
        self.assertEqual(normalized, "https://www.linkedin.com/posts/some-post-slug-123")

    def test_normalize_absolute_path(self):
        path = "/posts/my-slug"
        normalized = self.post_search.normalize_post_url(path)
        self.assertEqual(normalized, "https://www.linkedin.com/posts/my-slug")


class TestPostCommentExecution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.capture = MagicMock()
        self.session = MagicMock()
        self.page = AsyncMock()
        self.session.page = self.page
        self.post_search = PostSearch(capture=self.capture, session=self.session)

    async def test_empty_comment_raises(self):
        with self.assertRaises(ValueError):
            await self.post_search.post_comment("activity-123", "")

    async def test_confirm_post_false_returns_confirmation_required(self):
        result = await self.post_search.post_comment(
            "activity-123", "Hello world", confirm_post=False
        )
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["comment_text"], "Hello world")

    async def test_no_active_page_raises_runtime_error(self):
        unconnected_search = PostSearch(capture=self.capture, session=None)
        with self.assertRaises(RuntimeError):
            await unconnected_search.post_comment("activity-123", "Hello", confirm_post=True)


if __name__ == "__main__":
    unittest.main()
