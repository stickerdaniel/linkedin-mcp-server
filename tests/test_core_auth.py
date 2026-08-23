"""Tests for auth barrier detection helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.core.auth import (
    _REMEMBER_ME_CONTAINER_SELECTOR,
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)


def _barrier_page(*, picker: bool = False) -> MagicMock:
    """A page mock whose locator answers per selector, as the real one does.

    `MagicMock().count()` is not awaitable, so a page left unwired sends the
    structural check down its own exception path and every assertion about the
    text table passes for the wrong reason. Answering one count for every
    selector is the next version of that mistake: the check could be moved to
    `main`, or to any element a LinkedIn page always carries, and the picker
    would still be found here while a real page without the container is
    missed.
    """
    page = MagicMock()

    def locator(selector: str, *args: object, **kwargs: object) -> MagicMock:
        found = MagicMock()
        found.count = AsyncMock(
            return_value=1
            if picker and selector == _REMEMBER_ME_CONTAINER_SELECTOR
            else 0
        )
        return found

    page.locator = MagicMock(side_effect=locator)
    return page


@pytest.mark.asyncio
async def test_detect_auth_barrier_for_account_picker():
    page = _barrier_page()
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
    page = _barrier_page()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(
        return_value="Continue as Daniel Sticker\nSign in using another account"
    )

    result = await detect_auth_barrier(page)

    assert result is not None


@pytest.mark.asyncio
async def test_detect_auth_barrier_for_choose_account_picker():
    page = _barrier_page()
    page.url = "https://www.linkedin.com/checkpoint/lg/login-submit"
    page.title = AsyncMock(return_value="LinkedIn Sign In")
    page.evaluate = AsyncMock(
        return_value="Choose an account\nSign in using another account"
    )

    result = await detect_auth_barrier(page)

    assert result is not None


@pytest.mark.asyncio
async def test_detect_auth_barrier_returns_none_for_authenticated_page():
    page = _barrier_page()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_quick_skips_body_text_on_authenticated_page():
    page = _barrier_page()
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
async def test_a_localized_account_picker_is_still_a_barrier():
    """The picker's words change with the interface language; its id does not.

    Served in place of the page that was asked for, a picker keeps that page's
    address and title, so the words are the only other thing the detector had
    and they are English.
    """
    page = _barrier_page(picker=True)
    page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
    page.title = AsyncMock(return_value="Emplois | LinkedIn")
    page.evaluate = AsyncMock(
        return_value="Bon retour\nSe connecter avec un autre compte"
    )

    result = await detect_auth_barrier(page)

    assert result is not None
    assert "rememberme" in result


@pytest.mark.asyncio
async def test_a_localized_search_page_is_not_a_barrier():
    """A page in another language is not a barrier for being in another language."""
    page = _barrier_page()
    page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
    page.title = AsyncMock(return_value="Emplois | LinkedIn")
    page.evaluate = AsyncMock(return_value="Accueil\nRéseau\nEmplois\nMessagerie")

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_the_quick_check_asks_the_page_for_a_picker():
    """The two signals the quick path reads are the two this page defeats.

    A picker served in place of the page that was asked for carries that
    page's address and that page's title. The quick check runs after every
    navigation, so leaving the container to the full check let a picker in an
    uncovered locale reach every scraping tool as page text. It costs one
    selector count; the body read is what the quick path exists to skip.
    """
    page = _barrier_page(picker=True)
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Startseite\nMein Netzwerk")

    result = await detect_auth_barrier_quick(page)

    assert result is not None
    assert _REMEMBER_ME_CONTAINER_SELECTOR in result
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/login",
        "https://de.linkedin.com/checkpoint/challenge/",
        "https://linkedin.com/authwall",
    ],
)
async def test_linkedin_s_own_auth_routes_still_count(url: str):
    """Every host LinkedIn serves them from, and the bare domain."""
    page = _barrier_page()
    page.url = url
    page.title = AsyncMock(return_value="LinkedIn")
    page.evaluate = AsyncMock(return_value="")

    result = await detect_auth_barrier(page)

    assert result is not None
    assert "auth blocker URL" in result


@pytest.mark.asyncio
async def test_a_healthy_page_costs_the_quick_check_nothing_but_the_count():
    """The container is absent on an ordinary page, and that ends it."""
    page = _barrier_page()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network")

    assert await detect_auth_barrier_quick(page) is None
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_continue_as_in_page_content():
    page = _barrier_page()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="We need someone to continue as a senior engineer on our team."
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_choose_account_in_page_content():
    page = _barrier_page()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="You will choose an account strategy for the next quarter."
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_auth_substrings_in_slugs():
    page = _barrier_page()
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
