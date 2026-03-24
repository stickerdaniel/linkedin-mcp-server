"""Tests for tools/posts.py: all 7 posts-related MCP tools."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.exceptions import SessionExpiredError
from linkedin_mcp_server.tools.posts import register_posts_tools


async def get_tool_fn(mcp, name):
    tool = await mcp.get_tool(name)
    if tool is None:
        raise ValueError(f"Tool '{name}' not found")
    return tool.fn


def _make_extractor_with_page():
    """Create a mock extractor with a mock _page attribute."""
    mock_extractor = MagicMock()
    mock_extractor._page = MagicMock()
    return mock_extractor


class TestGetMyRecentPosts:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_my_recent_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_my_recent_posts",
            new_callable=AsyncMock,
            return_value=[{"post_url": "https://linkedin.com/post/1", "post_id": "1"}],
        ) as mock_scrape:
            result = await tool_fn(mock_context, limit=5, extractor=mock_extractor)

        assert "posts" in result
        assert len(result["posts"]) == 1
        mock_scrape.assert_awaited_once_with(mock_extractor._page, limit=5)

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_my_recent_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_my_recent_posts",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(mock_context, limit=5, extractor=mock_extractor)


class TestGetPostComments:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_comments")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_comments",
            new_callable=AsyncMock,
            return_value=[{"comment_id": "c1", "text": "Nice!"}],
        ) as mock_scrape:
            result = await tool_fn(
                "https://linkedin.com/feed/update/urn:li:activity:1/",
                mock_context,
                extractor=mock_extractor,
            )

        assert "comments" in result
        assert len(result["comments"]) == 1
        mock_scrape.assert_awaited_once()

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_comments")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_comments",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(
                    "https://linkedin.com/feed/update/urn:li:activity:1/",
                    mock_context,
                    extractor=mock_extractor,
                )


class TestGetPostContent:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_content")

        expected_result = {
            "url": "https://linkedin.com/feed/update/urn:li:activity:1/",
            "sections": {"post_content": "Hello world"},
        }
        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_content",
            new_callable=AsyncMock,
            return_value=expected_result,
        ):
            result = await tool_fn(
                "https://linkedin.com/feed/update/urn:li:activity:1/",
                mock_context,
                extractor=mock_extractor,
            )

        assert result["sections"]["post_content"] == "Hello world"

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_content")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_content",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(
                    "https://linkedin.com/feed/update/urn:li:activity:1/",
                    mock_context,
                    extractor=mock_extractor,
                )


class TestGetNotifications:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_notifications")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_notifications",
            new_callable=AsyncMock,
            return_value=[{"text": "Alice commented", "type": "comment"}],
        ):
            result = await tool_fn(mock_context, limit=10, extractor=mock_extractor)

        assert "notifications" in result
        assert len(result["notifications"]) == 1

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_notifications")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_notifications",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(mock_context, limit=10, extractor=mock_extractor)


class TestGetPersonPosts:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_person_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_profile_recent_posts",
            new_callable=AsyncMock,
            return_value=[{"post_url": "https://linkedin.com/post/1"}],
        ) as mock_scrape:
            result = await tool_fn(
                "testuser", mock_context, limit=5, extractor=mock_extractor
            )

        assert "posts" in result
        assert len(result["posts"]) == 1
        mock_scrape.assert_awaited_once_with(mock_extractor._page, "testuser", limit=5)

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_person_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_profile_recent_posts",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(
                    "testuser", mock_context, limit=5, extractor=mock_extractor
                )


class TestGetFeedPosts:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_feed_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_feed_posts",
            new_callable=AsyncMock,
            return_value=[
                {
                    "post_url": "https://linkedin.com/post/1",
                    "author_name": "Alice",
                }
            ],
        ) as mock_scrape:
            result = await tool_fn(mock_context, limit=10, extractor=mock_extractor)

        assert "posts" in result
        assert len(result["posts"]) == 1
        mock_scrape.assert_awaited_once_with(mock_extractor._page, limit=10)

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_feed_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_feed_posts",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(mock_context, limit=10, extractor=mock_extractor)


class TestFindUnrepliedComments:
    async def test_success(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "find_unreplied_comments")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_find_unreplied_comments",
            new_callable=AsyncMock,
            return_value=[{"comment_permalink": "https://linkedin.com/c/1"}],
        ) as mock_scrape:
            result = await tool_fn(
                mock_context,
                since_days=7,
                max_posts=20,
                extractor=mock_extractor,
            )

        assert "unreplied_comments" in result
        assert len(result["unreplied_comments"]) == 1
        mock_scrape.assert_awaited_once_with(
            mock_extractor._page, since_days=7, max_posts=20
        )

    async def test_error_raises_tool_error(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "find_unreplied_comments")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_find_unreplied_comments",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError(),
        ):
            with pytest.raises(ToolError, match="Session expired"):
                await tool_fn(
                    mock_context,
                    since_days=7,
                    max_posts=20,
                    extractor=mock_extractor,
                )


class TestStripNoneIntegration:
    """Verify strip_none is applied at tool boundary with None-heavy payloads."""

    async def test_my_recent_posts_strips_none_fields(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_my_recent_posts")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_my_recent_posts",
            new_callable=AsyncMock,
            return_value=[
                {
                    "post_url": "https://linkedin.com/post/1",
                    "post_id": "1",
                    "text_preview": "Hello",
                    "created_at": None,
                }
            ],
        ):
            result = await tool_fn(mock_context, limit=5, extractor=mock_extractor)

        post = result["posts"][0]
        assert "created_at" not in post
        assert post["post_url"] == "https://linkedin.com/post/1"
        assert post["post_id"] == "1"

    async def test_post_comments_strips_none_fields(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_comments")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_comments",
            new_callable=AsyncMock,
            return_value=[
                {
                    "comment_id": "c1",
                    "author_name": "Alice",
                    "text": "Nice!",
                    "created_at": None,
                    "comment_permalink": None,
                }
            ],
        ):
            result = await tool_fn(
                "https://linkedin.com/feed/update/urn:li:activity:1/",
                mock_context,
                extractor=mock_extractor,
            )

        comment = result["comments"][0]
        assert "created_at" not in comment
        assert "comment_permalink" not in comment
        assert comment["text"] == "Nice!"

    async def test_post_content_preserves_non_none_fields(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_post_content")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_post_content",
            new_callable=AsyncMock,
            return_value={
                "url": "https://linkedin.com/feed/update/urn:li:activity:1/",
                "sections": {"post_content": "Hello world"},
                "engagement": {"reactions": 5, "comments": 0},
                "author": None,
            },
        ):
            result = await tool_fn(
                "https://linkedin.com/feed/update/urn:li:activity:1/",
                mock_context,
                extractor=mock_extractor,
            )

        assert "author" not in result
        assert result["sections"]["post_content"] == "Hello world"
        assert result["engagement"]["comments"] == 0  # falsy but preserved

    async def test_notifications_strips_none_fields(self, mock_context):
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_notifications")

        with patch(
            "linkedin_mcp_server.tools.posts.scrape_get_notifications",
            new_callable=AsyncMock,
            return_value=[
                {
                    "text": "Alice commented",
                    "link": "https://linkedin.com/n/1",
                    "type": "comment",
                    "created_at": None,
                }
            ],
        ):
            result = await tool_fn(mock_context, limit=10, extractor=mock_extractor)

        notif = result["notifications"][0]
        assert "created_at" not in notif
        assert notif["type"] == "comment"


class TestPostsToolRegistration:
    async def test_all_tools_registered(self):
        mcp = FastMCP("test")
        register_posts_tools(mcp)

        expected_names = [
            "get_my_recent_posts",
            "get_post_comments",
            "get_post_content",
            "get_notifications",
            "get_person_posts",
            "get_feed_posts",
            "find_unreplied_comments",
        ]
        for name in expected_names:
            tool = await mcp.get_tool(name)
            assert tool is not None, f"Tool {name} not registered"

    async def test_all_tools_have_timeout(self):
        from linkedin_mcp_server.constants import (
            TOOL_TIMEOUT_LONG_SECONDS,
            TOOL_TIMEOUT_SECONDS,
        )

        mcp = FastMCP("test")
        register_posts_tools(mcp)

        long_timeout_tools = {"find_unreplied_comments"}
        tool_names = [
            "get_my_recent_posts",
            "get_post_comments",
            "get_post_content",
            "get_notifications",
            "get_person_posts",
            "get_feed_posts",
            "find_unreplied_comments",
        ]
        for name in tool_names:
            tool = await mcp.get_tool(name)
            expected = (
                TOOL_TIMEOUT_LONG_SECONDS
                if name in long_timeout_tools
                else TOOL_TIMEOUT_SECONDS
            )
            assert tool.timeout == expected, f"Tool {name} has wrong timeout"


class TestGetMyRecentPostsCacheIntegration:
    async def test_cache_miss_calls_scraper_and_stores(self, mock_context):
        """On cache miss, scraper is called and result is stored."""
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_my_recent_posts")

        with (
            patch("linkedin_mcp_server.tools.posts.sqlite_cache") as mock_cache,
            patch(
                "linkedin_mcp_server.tools.posts.scrape_get_my_recent_posts",
                new_callable=AsyncMock,
                return_value=[{"post_url": "https://linkedin.com/post/1"}],
            ) as mock_scrape,
        ):
            mock_cache.get_tool.return_value = None
            result = await tool_fn(mock_context, limit=5, extractor=mock_extractor)

        mock_cache.get_tool.assert_called_once_with("get_my_recent_posts", {"limit": 5})
        mock_scrape.assert_awaited_once()
        mock_cache.set_tool.assert_called_once()
        assert "posts" in result

    async def test_cache_hit_skips_scraper(self, mock_context):
        """On cache hit, scraper is NOT called."""
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "get_my_recent_posts")
        cached = {"posts": [{"post_url": "https://linkedin.com/cached"}]}

        with (
            patch("linkedin_mcp_server.tools.posts.sqlite_cache") as mock_cache,
            patch(
                "linkedin_mcp_server.tools.posts.scrape_get_my_recent_posts",
                new_callable=AsyncMock,
            ) as mock_scrape,
        ):
            mock_cache.get_tool.return_value = cached
            result = await tool_fn(mock_context, limit=5, extractor=mock_extractor)

        mock_scrape.assert_not_awaited()
        assert result == cached


class TestFindUnrepliedCommentsCacheIntegration:
    async def test_scraper_called_results_returned(self, mock_context):
        """find_unreplied_comments always calls scraper (not tool-result cached)."""
        mock_extractor = _make_extractor_with_page()
        mcp = FastMCP("test")
        register_posts_tools(mcp)
        tool_fn = await get_tool_fn(mcp, "find_unreplied_comments")
        unreplied = [{"comment_permalink": "https://p/1", "text": "Hello"}]

        with (
            patch("linkedin_mcp_server.tools.posts.sqlite_cache"),
            patch(
                "linkedin_mcp_server.tools.posts.scrape_find_unreplied_comments",
                new_callable=AsyncMock,
                return_value=unreplied,
            ) as mock_scrape,
        ):
            result = await tool_fn(
                mock_context, since_days=7, max_posts=10, extractor=mock_extractor
            )

        mock_scrape.assert_awaited_once()
        assert len(result["unreplied_comments"]) == 1
