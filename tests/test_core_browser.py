"""Tests for BrowserManager cookie import/export helpers."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager


def test_a_user_agent_is_refused_rather_than_applied(tmp_path):
    """The one funnel every browser goes through must reject an override.

    ``launch_options`` is spread into the context options, so without this a
    caller reintroducing ``user_agent=`` would silently reach Patchright and
    the browser would go back to contradicting its own client hints.
    """
    with pytest.raises(TypeError, match="user_agent"):
        BrowserManager(
            user_data_dir=tmp_path / "profile",
            user_agent="Mozilla/5.0 (test) Chrome/143.0.0.0",
        )


class TestGeometry:
    """Which window geometry a launch gets, and why it depends on the mode.

    The contradiction this fixes was measured: a headed window reported an
    outer height of 805 while the same browser reported a screen 720 tall,
    because an emulated viewport was forced onto a real window.
    """

    def test_headless_keeps_an_explicit_viewport(self, tmp_path):
        """Headless plus `no_viewport` collapses the screen to 800x600, so the
        explicit size stays for the mode that has no real window."""
        manager = BrowserManager(user_data_dir=tmp_path, headless=True)

        assert manager._geometry() == {"viewport": {"width": 1280, "height": 720}}

    def test_headless_honours_a_configured_viewport(self, tmp_path):
        manager = BrowserManager(
            user_data_dir=tmp_path,
            headless=True,
            viewport={"width": 1920, "height": 1080},
        )

        assert manager._geometry() == {"viewport": {"width": 1920, "height": 1080}}

    def test_headed_sends_no_viewport_at_all(self, tmp_path):
        """A real window reports the size it really is.

        `no_viewport` has to be the only geometry key: sending a viewport
        alongside it is what emulated a screen the window did not fit on.
        """
        manager = BrowserManager(user_data_dir=tmp_path, headless=False)

        assert manager._geometry() == {"no_viewport": True}

    def test_headed_ignores_a_configured_viewport(self, tmp_path):
        """VIEWPORT stops applying to headed launches, deliberately.

        Documented behaviour change: the setting now describes the windowless
        default mode only.
        """
        manager = BrowserManager(
            user_data_dir=tmp_path,
            headless=False,
            viewport={"width": 1920, "height": 1080},
        )

        assert manager._geometry() == {"no_viewport": True}

    def test_the_mode_comes_from_the_launch_not_the_configuration(self, tmp_path):
        """Only this object knows the answer.

        The manual login always constructs with `headless=False` while the
        configuration default stays `True`, so a decision made from the
        configuration would be wrong for exactly the launch that opens a window.
        """
        assert (
            "no_viewport"
            in BrowserManager(user_data_dir=tmp_path, headless=False)._geometry()
        )
        assert (
            "viewport"
            in BrowserManager(user_data_dir=tmp_path, headless=True)._geometry()
        )


def _make_cookie(
    name: str,
    value: str = "value",
    *,
    domain: str = ".linkedin.com",
) -> dict[str, str]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
    }


def _make_browser_manager(tmp_path) -> tuple[BrowserManager, MagicMock]:
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    context = MagicMock()
    context.clear_cookies = AsyncMock()
    context.add_cookies = AsyncMock()
    context.storage_state = AsyncMock()
    browser._context = context
    return browser, context


@pytest.mark.asyncio
async def test_import_cookies_imports_bridge_subset_only(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookies = [
        _make_cookie("li_at"),
        _make_cookie("JSESSIONID"),
        _make_cookie("bcookie"),
        _make_cookie("bscookie"),
        _make_cookie("lidc"),
        _make_cookie("session", domain=".example.com"),
        _make_cookie("timezone"),
    ]
    cookie_path.write_text(json.dumps(cookies))

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_awaited_once_with(
        [cookies[0], cookies[1], cookies[2], cookies[3], cookies[4]]
    )


@pytest.mark.asyncio
async def test_import_cookies_uses_bridge_core_debug_preset(tmp_path, monkeypatch):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookies = [
        _make_cookie("li_at"),
        _make_cookie("JSESSIONID"),
        _make_cookie("bcookie"),
        _make_cookie("bscookie"),
        _make_cookie("lidc"),
        _make_cookie("liap"),
        _make_cookie("timezone"),
    ]
    cookie_path.write_text(json.dumps(cookies))
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.add_cookies.assert_awaited_once_with(cookies)


@pytest.mark.asyncio
async def test_import_cookies_requires_li_at(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("JSESSIONID"),
                _make_cookie("bcookie"),
            ]
        )
    )

    imported = await browser.import_cookies(cookie_path)

    assert imported is False
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_cookies_preserves_existing_cookies(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("li_at"),
                _make_cookie("li_rm"),
                _make_cookie("JSESSIONID"),
            ]
        )
    )

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_storage_state_calls_context_storage_state(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "storage-state.json"

    exported = await browser.export_storage_state(storage_state_path, indexed_db=True)

    assert exported is True
    context.storage_state.assert_awaited_once_with(
        path=storage_state_path,
        indexed_db=True,
    )


@pytest.mark.asyncio
async def test_export_storage_state_requires_context(tmp_path):
    browser = BrowserManager(user_data_dir=tmp_path / "profile")

    exported = await browser.export_storage_state(tmp_path / "storage-state.json")

    assert exported is False


@pytest.mark.asyncio
async def test_close_is_idempotent_and_resets_state(tmp_path):
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    browser._page = MagicMock()
    context = MagicMock()
    context.close = AsyncMock(side_effect=RuntimeError("boom"))
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    browser._context = context
    browser._playwright = playwright

    await browser.close()
    await browser.close()

    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert browser._context is None
    assert browser._page is None
    assert browser._playwright is None
