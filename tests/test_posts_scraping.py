"""Tests for scraping/posts.py: normalize URL, get_post_content, get_my_recent_posts, get_post_comments, find_unreplied_comments."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping.extractor import ExtractedSection
from linkedin_mcp_server.scraping.posts import (
    _normalize_post_url,
    find_unreplied_comments,
    get_my_recent_posts,
    get_notifications,
    get_post_comments,
    get_post_content,
)


class TestNormalizePostUrl:
    """Unit tests for _normalize_post_url (pure function)."""

    def test_full_url_returned_unchanged(self):
        url = "https://www.linkedin.com/feed/update/urn:li:activity:123456/"
        assert _normalize_post_url(url) == url

    def test_full_url_without_trailing_slash(self):
        url = "https://www.linkedin.com/feed/update/urn:li:activity:123456"
        result = _normalize_post_url(url)
        assert "urn:li:activity:123456" in result
        assert result.startswith("https://")

    def test_numeric_id_normalized_to_full_url(self):
        result = _normalize_post_url("987654321")
        assert (
            result == "https://www.linkedin.com/feed/update/urn:li:activity:987654321/"
        )

    def test_urn_activity_normalized_to_full_url(self):
        result = _normalize_post_url("urn:li:activity:111222333")
        assert "urn:li:activity:111222333" in result
        assert result.startswith("https://www.linkedin.com/feed/update/")
        assert result.endswith("/")

    def test_urn_with_extra_text_extracts_id(self):
        result = _normalize_post_url("something urn:li:activity:555 end")
        assert "urn:li:activity:555" in result
        assert result.endswith("/")

    def test_stripped_whitespace(self):
        result = _normalize_post_url("  12345  ")
        assert result == "https://www.linkedin.com/feed/update/urn:li:activity:12345/"

    def test_relative_path_with_urn_extracts_and_builds_url(self):
        result = _normalize_post_url("/feed/update/urn:li:activity:42/")
        assert result.startswith("https://www.linkedin.com/feed/update/")
        assert "urn:li:activity:42" in result
        assert result.endswith("/")


@pytest.fixture
def mock_page():
    """Mock Patchright Page for scraping tests."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])
    return page


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetMyRecentPosts:
    """Tests for get_my_recent_posts."""

    async def test_returns_posts_from_evaluate(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "First post",
                    "created_at": None,
                },
                {
                    "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:2/",
                    "post_id": "urn:li:activity:2",
                    "text_preview": "Second post",
                    "created_at": None,
                },
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10)
        assert len(result) == 2
        assert (
            result[0]["post_url"]
            == "https://www.linkedin.com/feed/update/urn:li:activity:1/"
        )
        assert result[0]["text_preview"] == "First post"
        assert result[1]["post_id"] == "urn:li:activity:2"
        mock_page.goto.assert_awaited_once()
        mock_page.evaluate.assert_awaited_once()
        # evaluate was called with (limit,) as first arg (the JS receives limit)
        assert mock_page.evaluate.await_args[0][1] == 10

    async def test_passes_limit_to_evaluate(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        # Limit is applied in JS; Python forwards whatever evaluate returns.
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": f"https://linkedin.com/feed/update/urn:li:activity:{i}/",
                    "post_id": f"urn:li:activity:{i}",
                    "text_preview": "",
                    "created_at": None,
                }
                for i in range(3)
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=3)
        assert len(result) == 3
        assert mock_page.evaluate.await_args[0][1] == 3

    async def test_returns_empty_list_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        result = await get_my_recent_posts(mock_page, limit=5)
        assert result == []

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await get_my_recent_posts(mock_page, limit=5)

    async def test_filters_invalid_entries(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "Ok",
                    "created_at": None,
                },
                {},  # no post_url
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:2/",
                    "post_id": "urn:li:activity:2",
                    "text_preview": "",
                    "created_at": None,
                },
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10)
        assert len(result) == 2
        assert all("post_url" in p for p in result)


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetPostComments:
    """Tests for get_post_comments."""

    async def test_normalizes_post_id_and_returns_comments(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": None,
                    "author_name": "Alice",
                    "author_url": "https://www.linkedin.com/in/alice/",
                    "text": "Great post!",
                    "created_at": None,
                    "comment_permalink": None,
                    "has_reply_from_author": False,
                }
            ]
        )
        result = await get_post_comments(mock_page, "12345")
        assert len(result) == 1
        assert result[0]["author_name"] == "Alice"
        assert result[0]["text"] == "Great post!"
        mock_page.goto.assert_awaited_once()
        goto_url = mock_page.goto.await_args[0][0]
        assert "urn:li:activity:12345" in goto_url or "12345" in goto_url

    async def test_passes_current_user_name_for_reply_detection(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value=[])
        await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:1/",
            current_user_name="Me",
        )
        mock_page.evaluate.assert_awaited_once()
        assert mock_page.evaluate.await_args[0][1] == "Me"

    async def test_returns_empty_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Load failed"))
        result = await get_post_comments(mock_page, "urn:li:activity:999")
        assert result == []

    async def test_includes_has_reply_from_author_when_present(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "Bob",
                    "author_url": "https://linkedin.com/in/bob/",
                    "text": "Thanks!",
                    "created_at": None,
                    "comment_permalink": None,
                    "has_reply_from_author": True,
                }
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:1/",
            current_user_name="Me",
        )
        assert len(result) == 1
        assert result[0].get("has_reply_from_author") is True


@patch("linkedin_mcp_server.scraping.posts.get_my_recent_posts", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.get_post_comments", new_callable=AsyncMock)
@patch(
    "linkedin_mcp_server.scraping.posts._get_current_user_name", new_callable=AsyncMock
)
@patch(
    "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
    new_callable=AsyncMock,
)
class TestFindUnrepliedComments:
    """Tests for find_unreplied_comments (notifications path and fallback)."""

    async def test_uses_notifications_when_available(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = [
            {
                "comment_permalink": "https://linkedin.com/feed/update/urn:li:activity:1/?commentUrn=urn:li:comment:1",
                "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                "snippet": "Someone commented",
            }
        ]
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        assert len(result) == 1
        assert result[0]["comment_permalink"] is not None
        assert "comment" in (result[0].get("snippet") or "").lower()
        mock_notif.assert_awaited_once()
        mock_posts.assert_not_awaited()
        mock_comments.assert_not_awaited()

    async def test_empty_notifications_returns_empty_without_fallback(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        """When notifications loads successfully but finds nothing, return empty
        without falling back to the expensive post-scanning path."""
        mock_notif.return_value = []
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        mock_notif.assert_awaited_once()
        mock_posts.assert_not_awaited()
        mock_comments.assert_not_awaited()
        assert result == []

    async def test_fallback_to_posts_when_notifications_fail(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        """When notifications path fails (returns None), fall back to scanning posts."""
        mock_notif.return_value = None
        mock_name.return_value = "Current User"
        mock_posts.return_value = [
            {
                "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                "post_id": "urn:li:activity:1",
                "text_preview": "",
                "created_at": None,
            }
        ]
        mock_comments.return_value = [
            {
                "comment_id": None,
                "author_name": "Commenter",
                "author_url": "https://linkedin.com/in/commenter/",
                "text": "Unreplied comment",
                "created_at": None,
                "comment_permalink": None,
                "has_reply_from_author": False,
            }
        ]
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        mock_notif.assert_awaited_once()
        mock_posts.assert_awaited_once_with(
            mock_page, limit=20, since_days=7, max_scrolls=20
        )
        assert len(result) == 1
        assert result[0]["author_name"] == "Commenter"
        assert result[0]["text"] == "Unreplied comment"

    async def test_fallback_excludes_comments_with_reply(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        mock_posts.return_value = [
            {
                "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                "post_id": "urn:li:activity:1",
                "text_preview": "",
                "created_at": None,
            }
        ]
        mock_comments.return_value = [
            {
                "comment_id": None,
                "author_name": "A",
                "author_url": "https://linkedin.com/in/a/",
                "text": "Replied",
                "created_at": None,
                "comment_permalink": None,
                "has_reply_from_author": True,
            },
            {
                "comment_id": None,
                "author_name": "B",
                "author_url": "https://linkedin.com/in/b/",
                "text": "Unreplied",
                "created_at": None,
                "comment_permalink": None,
                "has_reply_from_author": False,
            },
        ]
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        assert len(result) == 1
        assert result[0]["text"] == "Unreplied"

    async def test_fallback_when_notifications_returns_none(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        mock_posts.return_value = []
        mock_comments.return_value = []
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=5)
        assert result == []
        mock_posts.assert_awaited_once()


class TestGetPostContent:
    """Tests for get_post_content."""

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_returns_post_content_from_extractor(
        self, mock_extractor_cls, mock_page
    ):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Hello, this is my post content!", references=[])
        )
        mock_extractor_cls.return_value = mock_instance

        result = await get_post_content(mock_page, "12345")

        assert (
            result["url"]
            == "https://www.linkedin.com/feed/update/urn:li:activity:12345/"
        )
        assert result["sections"]["post_content"] == "Hello, this is my post content!"
        assert result["pages_visited"] == [result["url"]]
        assert result["sections_requested"] == ["post_content"]
        mock_instance.extract_page.assert_awaited_once_with(result["url"], section_name="post_content")

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_normalizes_full_url(self, mock_extractor_cls, mock_page):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(return_value=ExtractedSection(text="Content", references=[]))
        mock_extractor_cls.return_value = mock_instance

        url = "https://www.linkedin.com/feed/update/urn:li:activity:999/"
        result = await get_post_content(mock_page, url)

        assert result["url"] == url
        mock_instance.extract_page.assert_awaited_once_with(url, section_name="post_content")

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_empty_content_returns_empty_sections(
        self, mock_extractor_cls, mock_page
    ):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(return_value=ExtractedSection(text="", references=[]))
        mock_extractor_cls.return_value = mock_instance

        result = await get_post_content(mock_page, "12345")

        assert result["sections"] == {}
        assert result["pages_visited"] == [result["url"]]

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_normalizes_urn_input(self, mock_extractor_cls, mock_page):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(return_value=ExtractedSection(text="Post text", references=[]))
        mock_extractor_cls.return_value = mock_instance

        result = await get_post_content(mock_page, "urn:li:activity:777")

        assert "urn:li:activity:777" in result["url"]
        assert result["sections"]["post_content"] == "Post text"


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetNotifications:
    """Tests for get_notifications."""

    async def test_returns_notifications_from_evaluate(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "text": "Alice commented on your post",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "type": "comment",
                    "created_at": "2h",
                },
                {
                    "text": "Bob liked your post",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:2/",
                    "type": "reaction",
                    "created_at": "3h",
                },
            ]
        )
        result = await get_notifications(mock_page, limit=10)
        assert len(result) == 2
        assert result[0]["text"] == "Alice commented on your post"
        assert result[0]["type"] == "comment"
        assert result[0]["link"] is not None
        assert result[1]["type"] == "reaction"
        mock_page.goto.assert_awaited_once()
        mock_page.evaluate.assert_awaited_once()
        assert mock_page.evaluate.await_args[0][1] == 10

    async def test_passes_limit_to_evaluate(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value=[])
        await get_notifications(mock_page, limit=5)
        assert mock_page.evaluate.await_args[0][1] == 5

    async def test_returns_empty_list_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        result = await get_notifications(mock_page, limit=10)
        assert result == []

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await get_notifications(mock_page, limit=5)

    async def test_filters_invalid_entries(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "text": "Valid notification text here",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "type": "comment",
                    "created_at": None,
                },
                {},  # no text
                {
                    "text": "",
                    "link": None,
                    "type": "other",
                    "created_at": None,
                },  # empty text
            ]
        )
        result = await get_notifications(mock_page, limit=10)
        assert len(result) == 1
        assert result[0]["text"] == "Valid notification text here"

    async def test_default_limit_is_20(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value=[])
        await get_notifications(mock_page)
        assert mock_page.evaluate.await_args[0][1] == 20
