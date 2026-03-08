"""Tests for auth barrier detection helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.auth import detect_auth_barrier


@pytest.mark.asyncio
async def test_detect_auth_barrier_for_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login"
    page.title = AsyncMock(return_value="LinkedIn Login, Sign in | LinkedIn")
    page.evaluate = AsyncMock(
        return_value="Welcome Back\nSign in using another account\nJoin now"
    )

    result = await detect_auth_barrier(page)

    assert result is not None
    assert "auth blocker URL" in result


@pytest.mark.asyncio
async def test_detect_auth_barrier_returns_none_for_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier(page)

    assert result is None
