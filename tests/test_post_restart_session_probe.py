"""Regression tests: post-restart session probe uses /in/me/ with bounded retry.

Root cause: after service restart _feed_auth_succeeds() navigated to /feed/,
which returns a generic HTTP 400 on fresh Chromium in some environments.
_feed_auth_succeeds() correctly raised NetworkError (bb3c346 fix), but
_authenticate_existing_profile() had no retry, so the first tool call was
unusable.

Fix: _probe_post_restart_session() uses _probe_auth_via_profile_nav() (/in/me/)
with max 3 attempts and 2s delay between them.

TDD order documented below:
  RED  - test_post_restart_profile_nav_http400_first_call_unusable: confirms
         the OLD behavior (no retry, first call fails with NetworkError) when
         the probe is the bare _feed_auth_succeeds path.  We simulate this
         by calling _feed_auth_succeeds directly on a browser whose goto
         always raises HTTP 400 without auth evidence — it must raise
         NetworkError immediately without retrying.
  GREEN - test_post_restart_profile_probe_retries_and_succeeds: confirms the
         NEW behavior: _probe_post_restart_session() retries and succeeds on
         attempt 2 after the first attempt raises NetworkError.
  GREEN - test_post_restart_profile_probe_barrier_does_not_retry: confirms that
         an auth barrier (session invalid) does not trigger retry.
  GREEN - test_post_restart_profile_probe_exhausts_retries_raises_last_error:
         confirms that 3 consecutive failures raises NetworkError from last
         attempt.
  GREEN - test_authenticate_existing_profile_uses_probe_not_feed: confirms
         get_or_create_browser() on same-runtime path does NOT call
         _feed_auth_succeeds for the startup probe (uses _probe_post_restart_session
         instead), preserving the bb3c346 session-retention fix.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.drivers.browser import (
    _feed_auth_succeeds,
    _probe_auth_via_profile_nav,
    _probe_post_restart_session,
    get_or_create_browser,
    reset_browser_for_testing,
)
import linkedin_mcp_server.drivers.browser as browser_module


@pytest.fixture(autouse=True)
def _reset_browser():
    reset_browser_for_testing()
    yield
    reset_browser_for_testing()


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch, tmp_path):
    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.get_config", lambda: config
    )


def _make_mock_browser() -> MagicMock:
    browser = MagicMock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
    browser.page = MagicMock()
    browser.page.url = "https://www.linkedin.com/in/johndoe/"
    browser.page.goto = AsyncMock()
    browser.page.set_default_timeout = MagicMock()
    browser.page.title = AsyncMock(return_value="LinkedIn")
    browser.page.evaluate = AsyncMock(return_value="")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    browser.page.locator = MagicMock(return_value=locator)
    browser.import_cookies = AsyncMock(return_value=False)
    browser.export_cookies = AsyncMock(return_value=False)
    browser.export_storage_state = AsyncMock(return_value=True)
    return browser


# ──────────────────────────────────────────────────────────────────────────────
# RED: confirm old path (_feed_auth_succeeds on /feed/) has no retry
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_restart_profile_nav_http400_first_call_unusable():
    """RED: _feed_auth_succeeds propagates NetworkError immediately (no retry).

    This documents the pre-fix behavior: a generic HTTP 400 from /feed/ with no
    auth barrier evidence raises NetworkError on the first attempt and the caller
    has no mechanism to retry.  The fix (replacing this call with
    _probe_post_restart_session) adds the retry on top of a different URL.
    """
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=Exception("Page.goto: HTTP 400 while navigating to /feed/")
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,  # no auth evidence — pure transport error
        ),
        pytest.raises(NetworkError, match="Feed navigation failed; try again"),
    ):
        await _feed_auth_succeeds(browser)

    # Confirm: goto was called exactly once — no retry at all
    assert browser.page.goto.await_count == 1


# ──────────────────────────────────────────────────────────────────────────────
# GREEN: new probe function behavior
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_restart_profile_probe_retries_and_succeeds():
    """GREEN: _probe_post_restart_session retries after NetworkError and succeeds."""
    browser = _make_mock_browser()
    # First attempt: transport error; second attempt: succeeds
    browser.page.goto = AsyncMock(
        side_effect=[
            Exception("Page.goto: HTTP 400 while navigating to /in/me/"),
            None,  # success on retry
        ]
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.asyncio",
            wraps=asyncio,
        ) as mock_asyncio,
    ):
        # Patch sleep to avoid actual delay in tests
        mock_asyncio.sleep = AsyncMock()
        result = await _probe_post_restart_session(browser)

    assert result is True
    assert browser.page.goto.await_count == 2
    mock_asyncio.sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_restart_profile_probe_barrier_does_not_retry():
    """GREEN: auth barrier on first attempt returns False immediately, no retry."""
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock()  # navigation succeeds

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value="login_wall",  # real auth barrier
        ),
    ):
        result = await _probe_post_restart_session(browser)

    assert result is False
    # Only one navigation attempted — barrier is definitive, no retry
    assert browser.page.goto.await_count == 1


@pytest.mark.asyncio
async def test_post_restart_profile_probe_exhausts_retries_raises_last_error():
    """GREEN: three consecutive NetworkErrors raises NetworkError from last attempt."""
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=Exception("Page.goto: HTTP 400 while navigating to /in/me/")
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.asyncio",
            wraps=asyncio,
        ) as mock_asyncio,
        pytest.raises(NetworkError, match="Profile probe navigation failed; try again"),
    ):
        mock_asyncio.sleep = AsyncMock()
        await _probe_post_restart_session(browser)

    assert browser.page.goto.await_count == browser_module._PROBE_MAX_ATTEMPTS
    # Sleep called between each failure: max_attempts - 1 times
    assert mock_asyncio.sleep.await_count == browser_module._PROBE_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_probe_auth_via_profile_nav_navigates_to_profile_url():
    """GREEN: _probe_auth_via_profile_nav navigates to /in/me/ not /feed/."""
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock()

    with patch(
        "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await _probe_auth_via_profile_nav(browser)

    assert result is True
    called_url = browser.page.goto.call_args[0][0]
    assert "/in/me/" in called_url, f"Expected /in/me/ navigation, got: {called_url}"
    assert "/feed/" not in called_url, "Should NOT navigate to /feed/"


# ──────────────────────────────────────────────────────────────────────────────
# GREEN: integration — bb3c346 session-retention fix is preserved
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authenticate_existing_profile_uses_probe_not_feed(tmp_path):
    """GREEN: post-restart startup uses _probe_post_restart_session, not _feed_auth_succeeds.

    Confirms bb3c346's session-retention fix is preserved: the old behavior
    (calling _feed_auth_succeeds directly) is replaced, and the new probe is
    what runs.  Verifies via mock call tracking that _probe_post_restart_session
    is invoked during same-runtime startup authentication.
    """
    import json
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        source_state_path,
    )

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile_dir).write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com"}])
    )
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(portable_cookie_path(profile_dir)),
            }
        )
    )

    source_browser = _make_mock_browser()
    probe_called = []

    async def mock_probe(browser):
        probe_called.append(browser)
        return True

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._probe_post_restart_session",
            side_effect=mock_probe,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
        ) as feed_mock,
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    assert len(probe_called) == 1, "Expected _probe_post_restart_session called once"
    feed_mock.assert_not_awaited()  # _feed_auth_succeeds must NOT be called during startup
