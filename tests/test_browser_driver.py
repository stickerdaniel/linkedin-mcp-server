"""Tests for linkedin_mcp_server.drivers.browser auth startup."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from linkedin_mcp_server.config.loaders import AppConfig
from linkedin_mcp_server.drivers.browser import (
    _feed_auth_succeeds,
    get_or_create_browser,
    reset_browser_for_testing,
)
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
)


@pytest.fixture(autouse=True)
def _reset_browser():
    reset_browser_for_testing()
    yield
    reset_browser_for_testing()


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch, tmp_path):
    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    monkeypatch.setattr("linkedin_mcp_server.drivers.browser.get_config", lambda: config)


def _make_mock_browser() -> MagicMock:
    browser = MagicMock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
    browser.page = MagicMock()
    browser.page.url = "https://www.linkedin.com/feed/"
    browser.page.goto = AsyncMock()
    browser.page.set_default_timeout = MagicMock()
    browser.page.title = AsyncMock(return_value="LinkedIn")
    browser.page.evaluate = AsyncMock(return_value="Feed")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    browser.page.locator = MagicMock(return_value=locator)
    browser.import_cookies = AsyncMock(return_value=False)
    browser.export_cookies = AsyncMock(return_value=False)
    browser.export_storage_state = AsyncMock(return_value=True)
    return browser


def _write_source_state(tmp_path, *, runtime_id: str, login_generation: str = "gen-1"):
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
                "source_runtime_id": runtime_id,
                "login_generation": login_generation,
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(portable_cookie_path(profile_dir)),
            }
        )
    )
    return profile_dir


@pytest.mark.asyncio
async def test_get_or_create_browser_requires_source_state():
    from linkedin_mcp_server.core import AuthenticationError

    with pytest.raises(AuthenticationError):
        await get_or_create_browser()


@pytest.mark.asyncio
async def test_get_or_create_browser_uses_source_profile(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    ctor.assert_called_once()
    assert ctor.call_args.kwargs["user_data_dir"] == tmp_path / "profile"
    source_browser.import_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_browser_clicks_remember_me(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    assert source_browser.page.goto.await_count == 2
    assert remember_me.await_count == 1


@pytest.mark.asyncio
async def test_feed_auth_retries_feed_after_remember_me_error_recovery():
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(side_effect=[Exception("net::ERR_TOO_MANY_REDIRECTS"), None])

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await _feed_auth_succeeds(browser) is True

    assert browser.page.goto.await_count == 2
    remember_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_feed_auth_records_single_post_recovery_trace():
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(side_effect=[Exception("net::ERR_TOO_MANY_REDIRECTS"), None])

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.record_page_trace",
            new_callable=AsyncMock,
        ) as record_page_trace,
    ):
        assert await _feed_auth_succeeds(browser) is True

    steps = [call.args[1] for call in record_page_trace.await_args_list]
    assert "feed-after-remember-me-error-recovery" in steps
    assert "feed-navigation-error-before-remember-me-retry" not in steps


@pytest.mark.asyncio
async def test_start_failure_closes_browser(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()
    source_browser.start = AsyncMock(side_effect=RuntimeError("start failed"))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        await get_or_create_browser()

    source_browser.close.assert_awaited_once()
