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
async def test_detect_auth_barrier_for_continue_as_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(
        return_value="Continue as Daniel Sticker\nSign in using another account"
    )

    result = await detect_auth_barrier(page)

    assert result is not None


@pytest.mark.asyncio
async def test_detect_auth_barrier_returns_none_for_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_continue_as_in_page_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="We need someone to continue as a senior engineer on our team."
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_auth_substrings_in_slugs():
    page = MagicMock()
    page.url = "https://www.linkedin.com/company/challenge-labs/"
    page.title = AsyncMock(return_value="Challenge Labs | LinkedIn")
    page.evaluate = AsyncMock(return_value="Challenge Labs builds developer tools.")

    result = await detect_auth_barrier(page)

    assert result is None
