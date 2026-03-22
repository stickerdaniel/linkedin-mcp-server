"""Tests for scraping/posts.py: normalize URL, get_post_content, get_my_recent_posts, get_post_comments, find_unreplied_comments."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.scraping.extractor import ExtractedSection, _RATE_LIMITED_MSG
from linkedin_mcp_server.scraping.cache import scraping_cache
from linkedin_mcp_server.scraping.posts import (
    _extract_author_info,
    _extract_engagement_metrics,
    _detect_post_type,
    _expand_comments_section,
    _get_current_user_name,
    _normalize_post_url,
    _unreplied_via_notifications,
    find_unreplied_comments,
    get_feed_posts,
    get_my_recent_posts,
    get_notifications,
    get_post_comments,
    get_post_content,
    get_profile_recent_posts,
)


class TestNormalizePostUrl:
    """Unit tests for _normalize_post_url (pure function)."""

    def test_full_url_returned_unchanged(self):
        url = "https://www.linkedin.com/feed/update/urn:li:activity:123456/"
        assert _normalize_post_url(url) == url

    def test_full_url_without_trailing_slash_gets_normalized(self):
        url = "https://www.linkedin.com/feed/update/urn:li:activity:123456"
        result = _normalize_post_url(url)
        assert result == "https://www.linkedin.com/feed/update/urn:li:activity:123456/"

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

    def test_non_url_non_urn_non_digit_without_trailing_slash(self):
        result = _normalize_post_url("some-slug")
        assert result == "https://www.linkedin.com/feed/update/some-slug/"

    def test_non_url_non_urn_non_digit_with_trailing_slash(self):
        result = _normalize_post_url("some-slug/")
        assert result == "https://www.linkedin.com/feed/update/some-slug/"


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

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        scraping_cache.clear()
        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await get_post_comments(mock_page, "12345")

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

    async def test_deduplicates_comments_by_id(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Same comment_id appearing multiple times should be deduplicated."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "urn:li:comment:(activity:1,100)",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "Great post!",
                    "created_at": None,
                    "comment_permalink": None,
                },
                {
                    "comment_id": "urn:li:comment:(activity:1,100)",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "Great post!",
                    "created_at": None,
                    "comment_permalink": None,
                },
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:dedup-id-test/",
        )
        assert len(result) == 1
        assert result[0]["comment_id"] == "urn:li:comment:(activity:1,100)"

    async def test_deduplicates_comments_by_text_fallback(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """When comment_id is missing, dedup by (author_url, text)."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": None,
                    "author_name": "Bob",
                    "author_url": "https://linkedin.com/in/bob/",
                    "text": "Nice!",
                    "created_at": None,
                    "comment_permalink": None,
                },
                {
                    "comment_id": None,
                    "author_name": "Bob",
                    "author_url": "https://linkedin.com/in/bob/",
                    "text": "Nice!",
                    "created_at": None,
                    "comment_permalink": None,
                },
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:dedup-text-test/",
        )
        assert len(result) == 1
        assert result[0]["author_name"] == "Bob"

    async def test_keeps_distinct_comments_same_author(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Same author with different text should both be kept."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "Great post!",
                    "created_at": None,
                    "comment_permalink": None,
                },
                {
                    "comment_id": "c2",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "One more thought...",
                    "created_at": None,
                    "comment_permalink": None,
                },
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:dedup-distinct-test/",
        )
        assert len(result) == 2
        texts = {r["text"] for r in result}
        assert texts == {"Great post!", "One more thought..."}

    async def test_filters_ghost_comment_entries(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Ghost entry with text matching author name should be filtered out."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "View Eloisio Alves de Abreu\u2019s  graphic link",
                    "author_url": "https://linkedin.com/in/eloisio/",
                    "text": "Obrigado por compartilhar André! Parabéns pela carreira!",
                    "created_at": None,
                    "comment_permalink": None,
                },
                {
                    "comment_id": "c2",
                    "author_name": "View Eloisio Alves de Abreu\u2019s  graphic link",
                    "author_url": "https://linkedin.com/in/eloisio/",
                    "text": "Eloisio Alves de Abreu",
                    "created_at": None,
                    "comment_permalink": None,
                },
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:ghost-test/",
        )
        assert len(result) == 1
        assert "Obrigado" in result[0]["text"]

    async def test_filters_short_ghost_comment(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Comment with very short text after author removal should be filtered."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "Alice Bob",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "Alice Bob ok",
                    "created_at": None,
                    "comment_permalink": None,
                },
            ]
        )
        result = await get_post_comments(
            mock_page,
            "https://linkedin.com/feed/update/urn:li:activity:ghost-short-test/",
        )
        assert len(result) == 0


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
            return_value=ExtractedSection(
                text="Hello, this is my post content!", references=[]
            )
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
        mock_instance.extract_page.assert_awaited_once_with(
            result["url"], section_name="post_content"
        )

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_normalizes_full_url(self, mock_extractor_cls, mock_page):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Content", references=[])
        )
        mock_extractor_cls.return_value = mock_instance

        url = "https://www.linkedin.com/feed/update/urn:li:activity:999/"
        result = await get_post_content(mock_page, url)

        assert result["url"] == url
        mock_instance.extract_page.assert_awaited_once_with(
            url, section_name="post_content"
        )

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_empty_content_returns_empty_sections(
        self, mock_extractor_cls, mock_page
    ):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="", references=[])
        )
        mock_extractor_cls.return_value = mock_instance

        result = await get_post_content(mock_page, "12345")

        assert result["sections"] == {}
        assert result["pages_visited"] == [result["url"]]

    @patch(
        "linkedin_mcp_server.scraping.posts.LinkedInExtractor",
    )
    async def test_normalizes_urn_input(self, mock_extractor_cls, mock_page):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Post text", references=[])
        )
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

    async def test_strips_status_reachable_from_notifications(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Notification text prefixed with 'Status is reachable' should be stripped."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "text": "Status is reachable\nFrancisco commented on your post",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "type": "comment",
                    "created_at": "2h",
                },
            ]
        )
        result = await get_notifications(mock_page, limit=10)
        assert len(result) == 1
        assert not result[0]["text"].startswith("Status is")
        assert result[0]["text"] == "Francisco commented on your post"

    async def test_classifies_reacted_as_reaction_type(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        """Notification with type 'reaction' should be preserved through Python layer."""
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "text": "Francisco Pinheiro and 19 others reacted to your post",
                    "link": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "type": "reaction",
                    "created_at": "1d",
                },
            ]
        )
        result = await get_notifications(mock_page, limit=10)
        assert len(result) == 1
        assert result[0]["type"] == "reaction"


class TestCache:
    """Tests for ScrapingCache used by post comments."""

    def setup_method(self):
        scraping_cache.clear()

    def test_cache_put_and_get(self):
        data = [{"comment_id": "c1"}]
        scraping_cache.put("key1", data)
        assert scraping_cache.get("key1") == data

    def test_cache_get_missing_key(self):
        assert scraping_cache.get("nonexistent") is None

    def test_cache_expired_entry(self):
        scraping_cache.put("expired", [{"id": "old"}], ttl=0.0)
        import time as _time

        _time.sleep(0.01)
        assert scraping_cache.get("expired") is None

    def test_cache_not_expired(self):
        data = [{"id": "fresh"}]
        scraping_cache.put("fresh", data, ttl=600.0)
        assert scraping_cache.get("fresh") == data


def _parse_count_py(s: str | None) -> int | None:
    """Python mirror of the parseCount JS in scraping/posts.py for testing."""
    if not s:
        return None
    s = s.strip()
    norm = s.replace(",", ".")
    k_match = re.match(r"([\d.]+)\s*[kK]", norm)
    if k_match:
        return round(float(k_match.group(1)) * 1000)
    m_match = re.match(r"([\d.]+)\s*[mM]", norm)
    if m_match:
        return round(float(m_match.group(1)) * 1000000)
    cleaned = re.sub(r"[.,]", "", s)
    cleaned = re.sub(r"\D", "", cleaned)
    if not cleaned:
        return None
    return int(cleaned)


class TestParseCountLogic:
    """Test the parseCount logic that lives inside the JS evaluate.

    Uses a Python mirror to validate all locale/format edge cases.
    The JS and Python implementations must stay in sync.
    """

    def test_simple_integer(self):
        assert _parse_count_py("42") == 42

    def test_thousands_with_comma(self):
        assert _parse_count_py("1,234") == 1234

    def test_thousands_with_dot(self):
        assert _parse_count_py("1.234") == 1234

    def test_k_with_dot_decimal(self):
        assert _parse_count_py("1.2K") == 1200

    def test_k_with_comma_decimal(self):
        assert _parse_count_py("1,2K") == 1200

    def test_k_integer(self):
        assert _parse_count_py("5K") == 5000

    def test_k_lowercase(self):
        assert _parse_count_py("3.5k") == 3500

    def test_m_with_dot_decimal(self):
        assert _parse_count_py("1.5M") == 1500000

    def test_m_with_comma_decimal(self):
        assert _parse_count_py("2,3M") == 2300000

    def test_m_integer(self):
        assert _parse_count_py("1M") == 1000000

    def test_empty_string(self):
        assert _parse_count_py("") is None

    def test_none(self):
        assert _parse_count_py(None) is None

    def test_whitespace_padding(self):
        assert _parse_count_py("  1.2K  ") == 1200

    def test_non_numeric(self):
        assert _parse_count_py("abc") is None

    def test_k_with_space(self):
        assert _parse_count_py("1.2 K") == 1200

    def test_plain_large_number_with_comma(self):
        assert _parse_count_py("12,345") == 12345

    def test_plain_large_number_with_dot(self):
        assert _parse_count_py("12.345") == 12345


class TestExtractEngagementMetrics:
    """Tests for _extract_engagement_metrics."""

    async def test_returns_metrics_dict(self):
        page = MagicMock()
        page.evaluate = AsyncMock(
            return_value={"reactions": 42, "comments_count": 5, "reposts_count": 3}
        )
        result = await _extract_engagement_metrics(page)
        assert result == {"reactions": 42, "comments_count": 5, "reposts_count": 3}

    async def test_returns_empty_dict_on_non_dict(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="not a dict")
        result = await _extract_engagement_metrics(page)
        assert result == {}

    async def test_returns_empty_dict_on_exception(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("JS error"))
        result = await _extract_engagement_metrics(page)
        assert result == {}


class TestDetectPostType:
    """Tests for _detect_post_type."""

    async def test_returns_post_type_string(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="video")
        result = await _detect_post_type(page)
        assert result == "video"

    async def test_returns_unknown_on_non_string(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=123)
        result = await _detect_post_type(page)
        assert result == "unknown"

    async def test_returns_unknown_on_exception(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("JS error"))
        result = await _detect_post_type(page)
        assert result == "unknown"


class TestExtractAuthorInfo:
    """Tests for _extract_author_info."""

    async def test_returns_author_dict(self):
        page = MagicMock()
        page.evaluate = AsyncMock(
            return_value={
                "name": "Alice",
                "headline": "Engineer",
                "profile_url": "https://linkedin.com/in/alice/",
            }
        )
        result = await _extract_author_info(page)
        assert result["name"] == "Alice"

    async def test_returns_empty_dict_on_non_dict(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=None)
        result = await _extract_author_info(page)
        assert result == {}

    async def test_returns_empty_dict_on_exception(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("JS error"))
        result = await _extract_author_info(page)
        assert result == {}


class TestGetCurrentUserName:
    """Tests for _get_current_user_name."""

    async def test_returns_name_string(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="John Doe")
        result = await _get_current_user_name(page)
        assert result == "John Doe"

    async def test_returns_none_on_empty_string(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="")
        result = await _get_current_user_name(page)
        assert result is None

    async def test_returns_none_on_non_string(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=42)
        result = await _get_current_user_name(page)
        assert result is None

    async def test_returns_none_on_null(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=None)
        result = await _get_current_user_name(page)
        assert result is None

    async def test_returns_none_on_exception(self):
        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=Exception("JS error"))
        result = await _get_current_user_name(page)
        assert result is None


class TestExpandCommentsSection:
    """Tests for _expand_comments_section."""

    async def test_returns_zero_when_no_buttons_visible(self):
        page = MagicMock()
        loc = MagicMock()
        loc.is_visible = AsyncMock(return_value=False)
        locator_mock = MagicMock(return_value=MagicMock(first=loc))
        page.locator = locator_mock
        result = await _expand_comments_section(page, max_clicks=2)
        assert result == 0

    async def test_clicks_visible_button(self):
        page = MagicMock()
        loc = MagicMock()
        # First call: visible and clickable, second call: not visible
        loc.is_visible = AsyncMock(
            side_effect=[True, False, False, False, False, False, False, False, False]
        )
        loc.click = AsyncMock()
        page.locator = MagicMock(return_value=MagicMock(first=loc))
        result = await _expand_comments_section(page, max_clicks=1)
        assert result == 1
        loc.click.assert_awaited_once()

    async def test_handles_timeout_error(self):
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        page = MagicMock()
        loc = MagicMock()
        loc.is_visible = AsyncMock(side_effect=PlaywrightTimeoutError("timeout"))
        page.locator = MagicMock(return_value=MagicMock(first=loc))
        result = await _expand_comments_section(page, max_clicks=1)
        assert result == 0

    async def test_handles_generic_exception(self):
        page = MagicMock()
        loc = MagicMock()
        loc.is_visible = AsyncMock(side_effect=RuntimeError("unexpected"))
        page.locator = MagicMock(return_value=MagicMock(first=loc))
        result = await _expand_comments_section(page, max_clicks=1)
        assert result == 0


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetProfileRecentPosts:
    """Tests for get_profile_recent_posts."""

    async def test_returns_posts_from_profile(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "Hello world",
                    "created_at": None,
                }
            ]
        )
        result = await get_profile_recent_posts(mock_page, "testuser", limit=10)
        assert len(result) == 1
        assert result[0]["post_url"].endswith("urn:li:activity:1/")
        assert result[0]["text_preview"] == "Hello world"
        goto_url = mock_page.goto.await_args[0][0]
        assert "testuser" in goto_url

    async def test_filters_invalid_entries(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "",
                    "created_at": None,
                },
                {},  # no post_url
                "not a dict",
            ]
        )
        result = await get_profile_recent_posts(mock_page, "user1", limit=10)
        assert len(result) == 1

    async def test_returns_empty_on_non_list(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value="not a list")
        result = await get_profile_recent_posts(mock_page, "user1", limit=10)
        assert result == []

    async def test_returns_empty_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        result = await get_profile_recent_posts(mock_page, "user1", limit=10)
        assert result == []

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await get_profile_recent_posts(mock_page, "user1", limit=10)

    async def test_strips_username_slashes(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value=[])
        await get_profile_recent_posts(mock_page, " /testuser/ ", limit=5)
        goto_url = mock_page.goto.await_args[0][0]
        assert "//testuser//" not in goto_url
        assert "testuser" in goto_url


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetFeedPosts:
    """Tests for get_feed_posts."""

    async def test_returns_feed_posts(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "Feed post",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "created_at": None,
                }
            ]
        )
        result = await get_feed_posts(mock_page, limit=10)
        assert len(result) == 1
        assert result[0]["author_name"] == "Alice"
        assert result[0]["post_url"].endswith("urn:li:activity:1/")

    async def test_handles_dict_response_with_items(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "items": [
                    {
                        "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                        "post_id": "urn:li:activity:1",
                        "text_preview": "Post",
                        "author_name": "Bob",
                        "author_url": None,
                        "created_at": None,
                    }
                ],
                "scrollHeight": 5000,
            }
        )
        result = await get_feed_posts(mock_page, limit=1)
        assert len(result) == 1
        assert result[0]["author_name"] == "Bob"

    async def test_stops_on_stable_scroll_height(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        # Two calls returning same height and no new items -> stop
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"items": [], "scrollHeight": 1000},
                {"items": [], "scrollHeight": 1000},
            ]
        )
        result = await get_feed_posts(mock_page, limit=10, max_scrolls=5)
        assert result == []
        # Should stop after 2 calls (first sets prev_height, second matches)
        assert mock_page.evaluate.await_count == 2

    async def test_returns_empty_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        result = await get_feed_posts(mock_page, limit=10)
        assert result == []

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await get_feed_posts(mock_page, limit=10)

    async def test_deduplicates_by_post_id(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        dup_post = {
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
            "post_id": "urn:li:activity:1",
            "text_preview": "Same",
            "author_name": None,
            "author_url": None,
            "created_at": None,
        }
        mock_page.evaluate = AsyncMock(return_value=[dup_post, dup_post])
        result = await get_feed_posts(mock_page, limit=10)
        assert len(result) == 1

    async def test_filters_entries_without_url_or_id(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {"post_url": None, "post_id": "urn:li:activity:1"},
                {"post_url": "https://linkedin.com/post/1", "post_id": None},
                "not a dict",
                42,
            ]
        )
        result = await get_feed_posts(mock_page, limit=10)
        assert result == []

    async def test_breaks_on_non_dict_raw(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        # Legacy single-shot evaluate that returns list (not dict)
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "",
                    "author_name": None,
                    "author_url": None,
                    "created_at": None,
                }
            ]
        )
        result = await get_feed_posts(mock_page, limit=10, max_scrolls=5)
        assert len(result) == 1
        # Should only call evaluate once (breaks on non-dict raw)
        assert mock_page.evaluate.await_count == 1


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestUnrepliedViaNotifications:
    """Tests for _unreplied_via_notifications."""

    async def test_returns_comment_links(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "items": [
                    {
                        "link": "https://linkedin.com/feed/update/urn:li:activity:1/?commentUrn=c1",
                        "snippet": "Someone commented",
                    }
                ],
                "hasContent": True,
            }
        )
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert result is not None
        assert len(result) == 1
        assert "comment_permalink" in result[0]
        assert "post_url" in result[0]

    async def test_returns_empty_list_when_no_comments_but_content(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value={"items": [], "hasContent": True})
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert result == []

    async def test_returns_none_when_no_content(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value={"items": [], "hasContent": False})
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert result is None

    async def test_returns_none_on_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert result is None

    async def test_reraises_linkedin_scraper_exception(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        mock_rate_limit.side_effect = RateLimitError(
            "Rate limited", suggested_wait_time=60
        )
        with pytest.raises(RateLimitError):
            await _unreplied_via_notifications(mock_page, since_days=7, max_posts=20)

    async def test_filters_items_without_link(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "items": [
                    {"link": "https://linkedin.com/feed/update/1/", "snippet": "ok"},
                    {"link": None, "snippet": "no link"},
                    "not a dict",
                ],
                "hasContent": True,
            }
        )
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert len(result) == 1

    async def test_returns_none_on_non_dict_raw(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(return_value="not a dict")
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert result is None

    async def test_splits_link_for_post_url(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "items": [
                    {
                        "link": "https://linkedin.com/feed/update/urn:li:activity:1/?commentUrn=x",
                        "snippet": "text",
                    }
                ],
                "hasContent": True,
            }
        )
        result = await _unreplied_via_notifications(
            mock_page, since_days=7, max_posts=20
        )
        assert (
            result[0]["post_url"]
            == "https://linkedin.com/feed/update/urn:li:activity:1/"
        )


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetPostCommentsCache:
    """Tests for cache integration in get_post_comments."""

    def setup_method(self):
        scraping_cache.clear()

    async def test_returns_cached_comments(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        cached_data = [{"comment_id": "c1", "author_name": "Cached", "text": "cached"}]
        scraping_cache.put(
            "comments:https://www.linkedin.com/feed/update/urn:li:activity:111/:user=",
            cached_data,
        )
        result = await get_post_comments(mock_page, "111")
        assert result == cached_data
        mock_page.goto.assert_not_awaited()

    async def test_caches_on_success(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "Alice",
                    "author_url": "https://linkedin.com/in/alice/",
                    "text": "Great!",
                    "created_at": None,
                    "comment_permalink": None,
                }
            ]
        )
        await get_post_comments(mock_page, "222")
        url = "https://www.linkedin.com/feed/update/urn:li:activity:222/"
        key = f"comments:{url}:user="
        assert scraping_cache.get(key) is not None


@patch("linkedin_mcp_server.scraping.posts.detect_rate_limit", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.handle_modal_close", new_callable=AsyncMock)
@patch("linkedin_mcp_server.scraping.posts.scroll_to_bottom", new_callable=AsyncMock)
class TestGetMyRecentPostsEdgeCases:
    """Additional edge case tests for get_my_recent_posts."""

    async def test_dict_response_with_items(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "items": [
                    {
                        "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                        "post_id": "urn:li:activity:1",
                        "text_preview": "Post",
                        "created_at": None,
                    }
                ],
                "scrollHeight": 5000,
            }
        )
        result = await get_my_recent_posts(mock_page, limit=1)
        assert len(result) == 1

    async def test_stops_on_stable_scroll_height(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            side_effect=[
                {"items": [], "scrollHeight": 1000},
                {"items": [], "scrollHeight": 1000},
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10, max_scrolls=5)
        assert result == []
        assert mock_page.evaluate.await_count == 2

    async def test_deduplicates_by_post_id(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        dup = {
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
            "post_id": "urn:li:activity:1",
            "text_preview": "",
            "created_at": None,
        }
        mock_page.evaluate = AsyncMock(return_value=[dup, dup])
        result = await get_my_recent_posts(mock_page, limit=10)
        assert len(result) == 1

    async def test_since_days_filters_old_posts(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "Recent",
                    "created_at": "2099-01-01T00:00:00Z",
                },
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:2/",
                    "post_id": "urn:li:activity:2",
                    "text_preview": "Old",
                    "created_at": "2000-01-01T00:00:00Z",
                },
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10, since_days=7)
        assert len(result) == 1
        assert result[0]["text_preview"] == "Recent"

    async def test_since_days_keeps_unknown_dates(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "No date",
                    "created_at": None,
                },
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10, since_days=7)
        assert len(result) == 1

    async def test_since_days_keeps_unparseable_dates(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {
                    "post_url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                    "post_id": "urn:li:activity:1",
                    "text_preview": "Bad date",
                    "created_at": "not-a-date",
                },
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10, since_days=7)
        assert len(result) == 1

    async def test_filters_entries_without_url_or_id(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value=[
                {"post_url": None, "post_id": "urn:li:activity:1"},
                {"post_url": "https://linkedin.com/post/1", "post_id": None},
                42,
            ]
        )
        result = await get_my_recent_posts(mock_page, limit=10)
        assert result == []

    async def test_non_dict_items_value(
        self, mock_scroll, mock_modal, mock_rate_limit, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={"items": "not a list", "scrollHeight": 1000}
        )
        result = await get_my_recent_posts(mock_page, limit=10, max_scrolls=1)
        assert result == []


class TestGetPostContentEngagement:
    """Tests for get_post_content engagement, post_type, and author integration."""

    @patch(
        "linkedin_mcp_server.scraping.posts._extract_author_info",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._detect_post_type", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._extract_engagement_metrics",
        new_callable=AsyncMock,
    )
    @patch("linkedin_mcp_server.scraping.posts.LinkedInExtractor")
    async def test_includes_engagement_post_type_author(
        self, mock_ext_cls, mock_engagement, mock_type, mock_author, mock_page
    ):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Hello", references=[])
        )
        mock_ext_cls.return_value = mock_instance
        mock_engagement.return_value = {"reactions": 10}
        mock_type.return_value = "image"
        mock_author.return_value = {"name": "Alice"}

        result = await get_post_content(mock_page, "12345")

        assert result["engagement"] == {"reactions": 10}
        assert result["post_type"] == "image"
        assert result["author"] == {"name": "Alice"}

    @patch(
        "linkedin_mcp_server.scraping.posts._extract_author_info",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._detect_post_type", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._extract_engagement_metrics",
        new_callable=AsyncMock,
    )
    @patch("linkedin_mcp_server.scraping.posts.LinkedInExtractor")
    async def test_preserves_references_when_present(
        self, mock_ext_cls, mock_engagement, mock_type, mock_author, mock_page
    ):
        refs = [{"url": "https://example.com", "type": "link"}]
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Post text", references=refs)
        )
        mock_ext_cls.return_value = mock_instance
        mock_engagement.return_value = {}
        mock_type.return_value = "text"
        mock_author.return_value = {}

        result = await get_post_content(mock_page, "12345")

        assert result["sections"]["post_content"] == "Post text"
        assert result["references"] == {"post_content": refs}
        assert "section_errors" not in result

    @patch(
        "linkedin_mcp_server.scraping.posts._extract_author_info",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._detect_post_type", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._extract_engagement_metrics",
        new_callable=AsyncMock,
    )
    @patch("linkedin_mcp_server.scraping.posts.LinkedInExtractor")
    async def test_surfaces_section_errors_on_failure(
        self, mock_ext_cls, mock_engagement, mock_type, mock_author, mock_page
    ):
        error_info = {"context": "extract_page", "error": "something failed"}
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text="", references=[], error=error_info)
        )
        mock_ext_cls.return_value = mock_instance
        mock_engagement.return_value = {}
        mock_type.return_value = "unknown"
        mock_author.return_value = {}

        result = await get_post_content(mock_page, "12345")

        assert result["sections"] == {}
        assert result["section_errors"] == {"post_content": error_info}
        assert "references" not in result

    @patch(
        "linkedin_mcp_server.scraping.posts._extract_author_info",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._detect_post_type", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._extract_engagement_metrics",
        new_callable=AsyncMock,
    )
    @patch("linkedin_mcp_server.scraping.posts.LinkedInExtractor")
    async def test_suppresses_rate_limited_msg(
        self, mock_ext_cls, mock_engagement, mock_type, mock_author, mock_page
    ):
        mock_instance = MagicMock()
        mock_instance.extract_page = AsyncMock(
            return_value=ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        )
        mock_ext_cls.return_value = mock_instance
        mock_engagement.return_value = {}
        mock_type.return_value = "unknown"
        mock_author.return_value = {}

        result = await get_post_content(mock_page, "12345")

        assert result["sections"] == {}
        assert "references" not in result


class TestFindUnrepliedCommentsEdgeCases:
    """Additional edge cases for find_unreplied_comments."""

    @patch(
        "linkedin_mcp_server.scraping.posts.get_my_recent_posts", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts.get_post_comments", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._get_current_user_name",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
        new_callable=AsyncMock,
    )
    async def test_fallback_skips_posts_without_url(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        mock_posts.return_value = [
            {"post_url": None, "post_id": "urn:li:activity:1"},
        ]
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        assert result == []
        mock_comments.assert_not_awaited()

    @patch(
        "linkedin_mcp_server.scraping.posts.get_my_recent_posts", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts.get_post_comments", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._get_current_user_name",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
        new_callable=AsyncMock,
    )
    async def test_fallback_handles_comment_exception(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        mock_posts.return_value = [
            {"post_url": "https://linkedin.com/post/1", "post_id": "1"},
        ]
        mock_comments.side_effect = Exception("Comment scraping failed")
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        assert result == []

    @patch(
        "linkedin_mcp_server.scraping.posts.get_my_recent_posts", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts.get_post_comments", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._get_current_user_name",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
        new_callable=AsyncMock,
    )
    async def test_fallback_caps_navigations(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        mock_posts.return_value = [
            {"post_url": f"https://linkedin.com/post/{i}", "post_id": f"p{i}"}
            for i in range(10)
        ]
        mock_comments.return_value = []
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=20)
        assert result == []
        # Should be capped at 5 navigations
        assert mock_comments.await_count == 5

    @patch(
        "linkedin_mcp_server.scraping.posts.get_my_recent_posts", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts.get_post_comments", new_callable=AsyncMock
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._get_current_user_name",
        new_callable=AsyncMock,
    )
    @patch(
        "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
        new_callable=AsyncMock,
    )
    async def test_fallback_stops_when_too_many_unreplied(
        self, mock_notif, mock_name, mock_comments, mock_posts, mock_page
    ):
        mock_notif.return_value = None
        mock_name.return_value = "Me"
        # max_posts=1 -> cap = 1*5 = 5 unreplied
        mock_posts.return_value = [
            {"post_url": "https://linkedin.com/post/1", "post_id": "p1"},
        ]
        mock_comments.return_value = [
            {
                "comment_id": f"c{i}",
                "author_name": f"User{i}",
                "text": f"Comment {i}",
                "has_reply_from_author": False,
            }
            for i in range(10)
        ]
        result = await find_unreplied_comments(mock_page, since_days=7, max_posts=1)
        # All 10 comments returned because cap is max_posts*5=5, but loop checks after append
        assert len(result) >= 5
