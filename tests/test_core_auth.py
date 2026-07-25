"""Tests for auth barrier detection helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.core.auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)


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
async def test_detect_auth_barrier_for_choose_account_picker():
    page = MagicMock()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(
        return_value="Choose an account\nSign in using another account"
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
async def test_detect_auth_barrier_quick_skips_body_text_on_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier_quick(page)

    assert result is None
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_logged_in_rejects_empty_authenticated_only_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value="")

    result = await is_logged_in(page)

    assert result is False


@pytest.mark.asyncio
async def test_is_logged_in_accepts_authenticated_only_page_with_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs")

    result = await is_logged_in(page)

    assert result is True


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
async def test_detect_auth_barrier_ignores_choose_account_in_page_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="You will choose an account strategy for the next quarter."
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


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_clicks_saved_account():
    page = MagicMock()
    target = MagicMock()
    target.wait_for = AsyncMock()
    target.scroll_into_view_if_needed = AsyncMock()
    target.click = AsyncMock()
    target.first = target
    page.locator.return_value = target
    page.wait_for_selector = AsyncMock()
    page.wait_for_load_state = AsyncMock()

    result = await resolve_remember_me_prompt(page)

    assert result is True
    target.click.assert_awaited_once()
    page.wait_for_load_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_returns_false_when_absent():
    page = MagicMock()
    page.wait_for_selector = AsyncMock(side_effect=Exception("missing"))

    result = await resolve_remember_me_prompt(page)

    assert result is False


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_returns_false_when_container_has_no_button():
    page = MagicMock()
    target = MagicMock()
    target.wait_for = AsyncMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    locator.first = target
    page.locator.return_value = locator
    page.wait_for_selector = AsyncMock()

    result = await resolve_remember_me_prompt(page)

    assert result is False
    target.wait_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_manual_login_clicks_saved_account(monkeypatch):
    page = MagicMock()
    clicked = {"value": False}

    async def fake_resolve(_page):
        if not clicked["value"]:
            clicked["value"] = True
            return True
        return False

    async def fake_is_logged_in(_page):
        return clicked["value"]

    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.resolve_remember_me_prompt", fake_resolve
    )
    monkeypatch.setattr("linkedin_mcp_server.core.auth.is_logged_in", fake_is_logged_in)

    await wait_for_manual_login(page, timeout=1000)

    assert clicked["value"] is True


@pytest.mark.asyncio
async def test_wait_for_manual_login_times_out_when_remember_me_repeats(monkeypatch):
    page = MagicMock()

    # 120000ms = 2 minutes so the rendered "N minutes" is a clean integer.
    class _FakeLoop:
        def __init__(self):
            self._times = iter([0.0, 130.0])

        def time(self):
            return next(self._times)

    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.resolve_remember_me_prompt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.asyncio.get_running_loop",
        lambda: _FakeLoop(),
    )

    with pytest.raises(AuthenticationError, match="Manual login timeout") as exc_info:
        await wait_for_manual_login(page, timeout=120000)

    message = str(exc_info.value)
    assert "LOGIN_TIMEOUT" in message
    assert "2 minutes" in message


@pytest.mark.asyncio
async def test_wait_for_manual_login_unlimited_when_timeout_zero(monkeypatch):
    page = MagicMock()
    calls = {"value": 0}

    async def fake_is_logged_in(_page):
        calls["value"] += 1
        return calls["value"] >= 2

    class _FakeLoop:
        """Time jumps far beyond any positive bound to prove 0 disables it."""

        def time(self):
            return 10**12

    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.resolve_remember_me_prompt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("linkedin_mcp_server.core.auth.is_logged_in", fake_is_logged_in)
    monkeypatch.setattr(
        "linkedin_mcp_server.core.auth.asyncio.get_running_loop",
        lambda: _FakeLoop(),
    )
    monkeypatch.setattr("linkedin_mcp_server.core.auth.asyncio.sleep", AsyncMock())

    # timeout=0 means unlimited: the elapsed check never fires even though the
    # fake clock is enormous, so it returns once is_logged_in becomes True.
    await wait_for_manual_login(page, timeout=0)
    assert calls["value"] == 2
