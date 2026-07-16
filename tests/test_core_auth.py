"""Tests for auth barrier detection helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.core.auth import (
    AuthBarrierKind,
    detect_auth_barrier,
    detect_auth_barrier_quick,
    detect_empty_profile_barrier,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_session_resume_redirect,
    wait_for_manual_login,
)
from linkedin_mcp_server.core.utils import TIMEOUT_ERRORS


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
@pytest.mark.parametrize(
    "url",
    [
        "http://captive.portal/login",
        "https://wifi.example.test/checkpoint/challenge",
        "https://www.linkedin.com.evil.test/authwall",
        "https://linkedin.com.attacker.test/uas/login",
    ],
)
async def test_detect_auth_barrier_ignores_auth_paths_on_external_hosts(url):
    page = MagicMock()
    page.url = url
    page.context.cookies = AsyncMock(return_value=[])

    assert await detect_auth_barrier_quick(page) is None
    page.context.cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_auth_barrier_accepts_linkedin_hostname_with_trailing_dot():
    page = MagicMock()
    page.url = "https://www.linkedin.com./login"

    barrier = await detect_auth_barrier_quick(page)

    assert barrier is not None
    assert barrier.kind == AuthBarrierKind.BLOCK


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
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_quick_skips_body_text_on_authenticated_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn Feed")
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs\nMessaging")
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    result = await detect_auth_barrier_quick(page)

    assert result is None
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_auth_barrier_rejects_logged_out_preview_by_cookie_presence():
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/example/"
    page.title = AsyncMock(return_value="Example Person | LinkedIn")
    page.context.cookies = AsyncMock(return_value=[])

    result = await detect_auth_barrier_quick(page)

    assert result is not None
    assert result.kind == AuthBarrierKind.BLOCK
    assert "li_at" in result


@pytest.mark.asyncio
async def test_detect_auth_barrier_accepts_nonempty_li_at():
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/example/"
    page.title = AsyncMock(return_value="Example Person | LinkedIn")
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    assert await detect_auth_barrier_quick(page) is None


@pytest.mark.asyncio
async def test_is_logged_in_rejects_empty_authenticated_only_page():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value="")
    page.context.cookies = AsyncMock(return_value=[])

    result = await is_logged_in(page)

    assert result is False


@pytest.mark.asyncio
async def test_is_logged_in_accepts_authenticated_only_page_with_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.evaluate = AsyncMock(return_value="Home\nMy Network\nJobs")
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    result = await is_logged_in(page)

    assert result is True


@pytest.mark.asyncio
async def test_is_logged_in_uses_only_structural_navigation_selectors():
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/example/"
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    assert await is_logged_in(page) is True

    selector = page.locator.call_args.args[0]
    assert "href" in selector
    assert "has-text" not in selector
    assert "global-nav" not in selector
    assert "aria-label=" not in selector


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://www.linkedin.com/feed/",
        "https://www.linkedin.com:444/feed/",
        "https://linkedin.com.evil.test/feed/",
        "http://wifi.example.test/login?next=https://www.linkedin.com/feed/",
    ],
)
async def test_is_logged_in_rejects_non_https_linkedin_origin_before_dom(url):
    page = MagicMock()
    page.url = url
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    assert await is_logged_in(page) is False
    page.locator.assert_not_called()
    page.context.cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_logged_in_does_not_match_authenticated_path_in_query():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/?next=/feed/"
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    assert await is_logged_in(page) is False
    page.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_is_logged_in_propagates_cookie_probe_failure():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.context.cookies = AsyncMock(side_effect=RuntimeError("driver disconnected"))

    with pytest.raises(NetworkError, match="could not inspect"):
        await is_logged_in(page)


@pytest.mark.asyncio
async def test_is_logged_in_rejects_redirect_during_nav_probe():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    locator = MagicMock()

    async def redirect_during_count():
        page.url = "https://captive.portal.example/login"
        return 1

    locator.count = AsyncMock(side_effect=redirect_during_count)
    page.locator.return_value = locator
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )
    page.evaluate = AsyncMock(return_value="External portal content")

    assert await is_logged_in(page) is False
    page.context.cookies.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_detect_auth_barrier_surfaces_redirect_during_cookie_probe():
    page = MagicMock()
    page.url = "https://www.linkedin.com/in/example/"

    async def redirect_during_cookies(*_args, **_kwargs):
        page.url = "https://captive.portal.example/login"
        return [{"name": "li_at", "value": "session"}]

    page.context.cookies = AsyncMock(side_effect=redirect_during_cookies)

    with pytest.raises(NetworkError, match="redirected outside"):
        await detect_auth_barrier_quick(page)


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_continue_as_in_page_content():
    page = MagicMock()
    page.url = "https://www.linkedin.com/jobs/view/123456/"
    page.title = AsyncMock(return_value="Software Engineer at Acme - LinkedIn")
    page.evaluate = AsyncMock(
        return_value="We need someone to continue as a senior engineer on our team."
    )
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
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
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_detect_auth_barrier_ignores_auth_substrings_in_slugs():
    page = MagicMock()
    page.url = "https://www.linkedin.com/company/challenge-labs/"
    page.title = AsyncMock(return_value="Challenge Labs | LinkedIn")
    page.evaluate = AsyncMock(return_value="Challenge Labs builds developer tools.")
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )

    result = await detect_auth_barrier(page)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_clicks_saved_account():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/"
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
    page.url = "https://www.linkedin.com/login/"
    page.wait_for_selector = AsyncMock(side_effect=Exception("missing"))

    result = await resolve_remember_me_prompt(page)

    assert result is False


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_returns_false_when_container_has_no_button():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/"
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
@pytest.mark.parametrize(
    "url",
    [
        "https://captive.portal.example/login",
        "http://www.linkedin.com/login/",
        "https://www.linkedin.com.evil.test/login/",
    ],
)
async def test_resolve_remember_me_prompt_never_touches_external_page(url):
    page = MagicMock()
    page.url = url
    page.wait_for_selector = AsyncMock()

    assert await resolve_remember_me_prompt(page) is False
    page.wait_for_selector.assert_not_awaited()
    page.locator.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_remember_me_prompt_refuses_redirect_during_wait():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/"

    async def redirect_during_wait(*_args, **_kwargs):
        page.url = "https://captive.portal.example/login"

    page.wait_for_selector = AsyncMock(side_effect=redirect_during_wait)

    assert await resolve_remember_me_prompt(page) is False
    page.locator.assert_not_called()


@pytest.mark.asyncio
async def test_session_resume_wait_requires_nonempty_li_at():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/?session_redirect=%2Ffeed%2F"
    page.context.cookies = AsyncMock(return_value=[{"name": "li_at", "value": "  "}])
    page.wait_for_url = AsyncMock()

    assert await wait_for_session_resume_redirect(page) is False
    page.wait_for_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_resume_ignores_external_captive_portal_login():
    page = MagicMock()
    page.url = "http://wifi.example.test/login?next=https://www.linkedin.com/feed/"
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )
    page.wait_for_url = AsyncMock()

    assert await wait_for_session_resume_redirect(page) is False
    page.context.cookies.assert_not_awaited()
    page.wait_for_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_resume_waits_when_login_redirect_has_li_at():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/?session_redirect=%2Ffeed%2F"
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )
    page.wait_for_url = AsyncMock()

    assert await wait_for_session_resume_redirect(page) is True
    page.wait_for_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_resume_timeout_still_allows_one_explicit_retry():
    page = MagicMock()
    page.url = "https://www.linkedin.com/login/?session_redirect=%2Ffeed%2F"
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )
    page.wait_for_url = AsyncMock(side_effect=TIMEOUT_ERRORS[0]("timed out"))

    assert await wait_for_session_resume_redirect(page) is True


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


def _barrier_page(*, url: str, title: str = "LinkedIn", body: str = "") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.evaluate = AsyncMock(return_value=body)
    page.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "value": "session"}]
    )
    return page


class TestAuthBarrierKind:
    """AuthBarrier subclasses str (backward compat) and tags .kind."""

    @pytest.mark.asyncio
    async def test_login_url_is_a_block(self):
        page = _barrier_page(url="https://www.linkedin.com/login")
        barrier = await detect_auth_barrier(page)
        assert barrier is not None
        assert barrier.kind == AuthBarrierKind.BLOCK

    @pytest.mark.asyncio
    async def test_authwall_url_is_a_block(self):
        page = _barrier_page(url="https://www.linkedin.com/authwall")
        barrier = await detect_auth_barrier(page)
        assert barrier is not None
        assert barrier.kind == AuthBarrierKind.BLOCK

    @pytest.mark.asyncio
    async def test_checkpoint_url_is_a_challenge(self):
        page = _barrier_page(url="https://www.linkedin.com/checkpoint/challenge/abc123")
        barrier = await detect_auth_barrier(page)
        assert barrier is not None
        assert barrier.kind == AuthBarrierKind.CHALLENGE

    @pytest.mark.asyncio
    async def test_email_challenge_url_is_a_challenge(self):
        page = _barrier_page(
            url="https://www.linkedin.com/uas/consumer-email-challenge"
        )
        barrier = await detect_auth_barrier(page)
        assert barrier is not None
        assert barrier.kind == AuthBarrierKind.CHALLENGE

    @pytest.mark.asyncio
    async def test_localized_title_is_not_a_classification_signal(self):
        page = _barrier_page(
            url="https://www.linkedin.com/feed/",
            title="Sign In | LinkedIn",
        )
        page.context.cookies = AsyncMock(
            return_value=[{"name": "li_at", "value": "session"}]
        )
        barrier = await detect_auth_barrier(page)
        assert barrier is None

    @pytest.mark.asyncio
    async def test_localized_body_text_is_not_a_classification_signal(self):
        page = _barrier_page(
            url="https://www.linkedin.com/feed/",
            body="Welcome Back\nSign in using another account\nJoin now",
        )
        page.context.cookies = AsyncMock(
            return_value=[{"name": "li_at", "value": "session"}]
        )
        barrier = await detect_auth_barrier(page)
        assert barrier is None

    @pytest.mark.asyncio
    async def test_cookie_probe_failure_is_not_no_barrier(self):
        page = _barrier_page(url="https://www.linkedin.com/feed/")
        page.context.cookies = AsyncMock(
            side_effect=RuntimeError("driver disconnected")
        )

        with pytest.raises(NetworkError, match="could not inspect"):
            await detect_auth_barrier_quick(page)

    @pytest.mark.asyncio
    async def test_barrier_still_behaves_as_a_plain_string(self):
        page = _barrier_page(url="https://www.linkedin.com/login")
        barrier = await detect_auth_barrier(page)
        assert isinstance(barrier, str)
        assert barrier == "auth blocker URL: https://www.linkedin.com/login"
        assert "auth blocker" in barrier


def _main_page(*, main_text: str | Exception) -> MagicMock:
    """A page whose ``main`` locator's inner_text either returns text or
    raises (simulating <main> never appearing)."""
    page = MagicMock()
    main_locator = MagicMock()
    if isinstance(main_text, Exception):
        main_locator.inner_text = AsyncMock(side_effect=main_text)
    else:
        main_locator.inner_text = AsyncMock(return_value=main_text)
    page.locator = MagicMock(return_value=main_locator)
    return page


class TestDetectEmptyProfileBarrier:
    """detect_empty_profile_barrier keys off the caller's own url param, not
    page.url -- see the function docstring for why (redirects, test doubles)."""

    @pytest.mark.asyncio
    async def test_non_profile_url_is_never_flagged(self):
        page = _main_page(main_text="")
        result = await detect_empty_profile_barrier(
            page, "https://www.linkedin.com/search/results/people/"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_thin_main_text_on_profile_url_is_a_challenge(self):
        page = _main_page(main_text="Sign in")
        result = await detect_empty_profile_barrier(
            page, "https://www.linkedin.com/in/testuser/"
        )
        assert result is not None
        assert result.kind == AuthBarrierKind.CHALLENGE
        assert "testuser" in result

    @pytest.mark.asyncio
    async def test_realistic_main_text_on_profile_url_is_not_flagged(self):
        page = _main_page(
            main_text="Jane Doe\n\nSoftware Engineer at Acme\n\nSan Francisco, CA\n\n"
            "500+ connections\n\nAbout\n\nBuilding developer tools for 10 years."
        )
        result = await detect_empty_profile_barrier(
            page, "https://www.linkedin.com/in/janedoe/"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_main_never_appearing_is_inconclusive_not_flagged(self):
        page = _main_page(main_text=TIMEOUT_ERRORS[0]("timed out"))
        result = await detect_empty_profile_barrier(
            page, "https://www.linkedin.com/in/testuser/"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_details_subpage_is_also_covered_by_the_in_marker(self):
        page = _main_page(main_text="x")
        result = await detect_empty_profile_barrier(
            page, "https://www.linkedin.com/in/testuser/details/certifications/"
        )
        assert result is not None
        assert result.kind == AuthBarrierKind.CHALLENGE
