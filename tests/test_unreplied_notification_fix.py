"""Tests for notification false positive fix in find_unreplied_comments."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping.posts import find_unreplied_comments


class TestNotificationRetroactiveFilter:
    @pytest.mark.asyncio
    async def test_notification_entry_removed_when_author_replied(self):
        """Notification entry is retroactively removed when scan shows user replied."""
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:111"
        comment_permalink = "https://www.linkedin.com/feed/update/urn:li:activity:111?commentUrn=abc"
        alice_url = "https://www.linkedin.com/in/alice"

        notif_entries = [
            {
                "comment_permalink": comment_permalink,
                "post_url": post_url,
                "snippet": "Nice post!",
            }
        ]
        posts_list = [{"post_url": post_url}]

        # Scan returns: alice commented, user replied (starts with "Alice Smith, ...")
        scan_comments = [
            {
                "comment_permalink": comment_permalink,
                "author_url": alice_url,
                "author_name": "Alice Smith",
                "text": "Nice post!",
                "has_reply_from_author": False,
            },
            {
                "comment_permalink": None,
                "author_url": "https://www.linkedin.com/in/andre-martins-tech",
                "author_name": "Andre Martins",
                "text": "Alice Smith, obrigado!",
                "has_reply_from_author": False,
            },
        ]

        with (
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_name",
                new=AsyncMock(return_value="Andre Martins"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_slug",
                new=AsyncMock(return_value="andre-martins-tech"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
                new=AsyncMock(return_value=notif_entries),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_my_recent_posts",
                new=AsyncMock(return_value=posts_list),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_post_comments",
                new=AsyncMock(return_value=scan_comments),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.humanized_delay",
                return_value=0,
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await find_unreplied_comments(AsyncMock(), since_days=7, max_posts=5)

        # Notification entry for alice should be REMOVED (user replied)
        assert not any(
            e.get("comment_permalink") == comment_permalink for e in result
        )

    @pytest.mark.asyncio
    async def test_notification_entry_kept_when_author_not_replied(self):
        """Notification entry is kept when user has NOT replied to that author."""
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:222"
        comment_permalink = (
            "https://www.linkedin.com/feed/update/urn:li:activity:222?commentUrn=xyz"
        )
        bob_url = "https://www.linkedin.com/in/bob"

        notif_entries = [
            {
                "comment_permalink": comment_permalink,
                "post_url": post_url,
                "snippet": "Great!",
            }
        ]
        posts_list = [{"post_url": post_url}]

        scan_comments = [
            {
                "comment_permalink": comment_permalink,
                "author_url": bob_url,
                "author_name": "Bob Jones",
                "text": "Great!",
                "has_reply_from_author": False,
            },
            # No reply from user
        ]

        with (
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_name",
                new=AsyncMock(return_value="Andre Martins"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_slug",
                new=AsyncMock(return_value="andre-martins-tech"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
                new=AsyncMock(return_value=notif_entries),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_my_recent_posts",
                new=AsyncMock(return_value=posts_list),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_post_comments",
                new=AsyncMock(return_value=scan_comments),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.humanized_delay",
                return_value=0,
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await find_unreplied_comments(AsyncMock(), since_days=7, max_posts=5)

        # Bob's notification entry should be kept (not replied)
        assert any(e.get("comment_permalink") == comment_permalink for e in result)

    @pytest.mark.asyncio
    async def test_notification_entry_without_permalink_always_kept(self):
        """Notification entry with no comment_permalink cannot be matched — kept."""
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:333"
        notif_entries = [
            {"comment_permalink": None, "post_url": post_url, "snippet": "No permalink"}
        ]
        posts_list = [{"post_url": post_url}]
        scan_comments = []

        with (
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_name",
                new=AsyncMock(return_value="Andre Martins"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_slug",
                new=AsyncMock(return_value="andre-martins-tech"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
                new=AsyncMock(return_value=notif_entries),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_my_recent_posts",
                new=AsyncMock(return_value=posts_list),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_post_comments",
                new=AsyncMock(return_value=scan_comments),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.humanized_delay",
                return_value=0,
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await find_unreplied_comments(AsyncMock(), since_days=7, max_posts=5)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_notification_permalink_not_in_scan_kept(self):
        """If notification permalink not found in scan, entry is kept (conservative)."""
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:444"
        comment_permalink = "https://www.linkedin.com/feed/update/urn:li:activity:444?commentUrn=gone"
        notif_entries = [
            {"comment_permalink": comment_permalink, "post_url": post_url, "snippet": "..."}
        ]
        posts_list = [{"post_url": post_url}]
        scan_comments = []  # Scan returns nothing (e.g., post no longer accessible)

        with (
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_name",
                new=AsyncMock(return_value="Andre Martins"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_slug",
                new=AsyncMock(return_value="andre-martins-tech"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
                new=AsyncMock(return_value=notif_entries),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_my_recent_posts",
                new=AsyncMock(return_value=posts_list),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_post_comments",
                new=AsyncMock(return_value=scan_comments),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.humanized_delay",
                return_value=0,
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await find_unreplied_comments(AsyncMock(), since_days=7, max_posts=5)

        assert any(e.get("comment_permalink") == comment_permalink for e in result)

    @pytest.mark.asyncio
    async def test_non_notification_post_not_affected(self):
        """Posts not from notifications go through normal scan without retroactive filter."""
        post_url = "https://www.linkedin.com/feed/update/urn:li:activity:555"
        comment_permalink = (
            "https://www.linkedin.com/feed/update/urn:li:activity:555?commentUrn=abc"
        )
        carol_url = "https://www.linkedin.com/in/carol"

        posts_list = [{"post_url": post_url}]

        scan_comments = [
            {
                "comment_permalink": comment_permalink,
                "author_url": carol_url,
                "author_name": "Carol Lee",
                "text": "Interesting!",
                "has_reply_from_author": False,
            },
        ]

        with (
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_name",
                new=AsyncMock(return_value="Andre Martins"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._get_current_user_slug",
                new=AsyncMock(return_value="andre-martins-tech"),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts._unreplied_via_notifications",
                new=AsyncMock(return_value=[]),  # No notifications
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_my_recent_posts",
                new=AsyncMock(return_value=posts_list),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.get_post_comments",
                new=AsyncMock(return_value=scan_comments),
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.humanized_delay",
                return_value=0,
            ),
            patch(
                "linkedin_mcp_server.scraping.posts.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            result = await find_unreplied_comments(AsyncMock(), since_days=7, max_posts=5)

        assert any(e.get("comment_permalink") == comment_permalink for e in result)
