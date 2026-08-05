"""Tests for BrowserManager cookie import/export helpers."""

import contextlib
import json
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager


def test_a_no_viewport_override_is_refused(tmp_path):
    """The geometry decision must not be overridable through launch options.

    `_geometry()` is spread *before* `**launch_options`, so a caller-supplied
    `no_viewport` would win. `no_viewport=False` on a headed launch puts the
    emulated screen back and restores the window-larger-than-screen
    contradiction this change exists to remove; `no_viewport=True` on a
    headless one sends both geometry keys at once. Nothing produces either
    today, which is why it is refused now rather than after something does.
    """
    with pytest.raises(TypeError, match="no_viewport"):
        BrowserManager(user_data_dir=tmp_path / "profile", no_viewport=False)


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

    def test_the_mode_comes_from_the_launch_not_the_configuration(
        self, tmp_path, monkeypatch
    ):
        """The constructor argument wins over the configuration, which differs.

        This is the confusion worth pinning. The manual login constructs with
        `headless=False` while `BrowserConfig.headless` stays `True`, so a
        decision read from configuration would be wrong for exactly the launch
        that opens a window. Asserted by making the two disagree.
        """
        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.headless = True
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config", lambda: config, raising=False
        )

        headed = BrowserManager(user_data_dir=tmp_path, headless=False)

        assert headed._geometry() == {"no_viewport": True}

    async def test_the_launch_actually_receives_the_geometry(self, tmp_path):
        """The helper is not the behaviour.

        Every other test here would stay green if `start()` stopped calling
        `_geometry()` and went back to sending a viewport unconditionally. This
        one reads the kwargs Patchright is actually handed.
        """
        captured: dict = {}

        class _FakeChromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                captured.update(kwargs)
                raise _StopLaunch()

        class _FakePlaywright:
            chromium = _FakeChromium()

            async def stop(self):
                return None

        class _StopLaunch(Exception):
            """Ends the launch once the options have been observed."""

        manager = BrowserManager(user_data_dir=tmp_path, headless=False)

        async def fake_start():
            return _FakePlaywright()

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = fake_start
            with contextlib.suppress(Exception):
                await manager.start()

        assert captured.get("no_viewport") is True
        assert "viewport" not in captured
        # The locale sits after the spread on purpose and must stay pinned.
        assert captured.get("locale") == "en-US"


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
