"""Tests for BrowserManager cookie import/export helpers."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager


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


def _make_cdp_playwright(existing_context=None, existing_page=None):
    """Build a fake playwright whose chromium.connect_over_cdp returns a Browser."""
    page = existing_page or MagicMock()
    context = existing_context or MagicMock()
    context.pages = [page] if existing_page is not None else []
    context.new_page = AsyncMock(return_value=page)
    remote_browser = MagicMock()
    remote_browser.contexts = [context] if existing_context is not None else []
    remote_browser.new_context = AsyncMock(return_value=context)
    remote_browser.close = AsyncMock()
    chromium = MagicMock()
    chromium.connect_over_cdp = AsyncMock(return_value=remote_browser)
    playwright = MagicMock()
    playwright.chromium = chromium
    playwright.stop = AsyncMock()
    return playwright, remote_browser, context, page


@pytest.mark.asyncio
async def test_start_over_cdp_reuses_existing_context_and_page(monkeypatch):
    page = MagicMock()
    context = MagicMock()
    playwright, remote_browser, context, page = _make_cdp_playwright(
        existing_context=context, existing_page=page
    )

    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr(
        "linkedin_mcp_server.core.browser.async_playwright", lambda: starter
    )

    browser = BrowserManager(cdp_endpoint="http://127.0.0.1:9222", slow_mo=7)
    await browser.start()

    chromium = playwright.chromium
    chromium.connect_over_cdp.assert_awaited_once_with(
        "http://127.0.0.1:9222", slow_mo=7
    )
    remote_browser.new_context.assert_not_awaited()
    context.new_page.assert_not_awaited()
    assert browser._browser is remote_browser
    assert browser._context is context
    assert browser._page is page
    assert browser.is_cdp is True


@pytest.mark.asyncio
async def test_start_over_cdp_creates_context_and_page_when_missing(monkeypatch):
    playwright, remote_browser, context, page = _make_cdp_playwright()

    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    monkeypatch.setattr(
        "linkedin_mcp_server.core.browser.async_playwright", lambda: starter
    )

    browser = BrowserManager(cdp_endpoint="ws://remote/cdp")
    await browser.start()

    remote_browser.new_context.assert_awaited_once()
    context.new_page.assert_awaited_once()
    assert browser._page is page


@pytest.mark.asyncio
async def test_cdp_close_non_persistent_sends_browser_close_command():
    browser = BrowserManager(cdp_endpoint="http://127.0.0.1:9222")
    session = MagicMock()
    session.send = AsyncMock()
    remote_browser = MagicMock()
    remote_browser.new_browser_cdp_session = AsyncMock(return_value=session)
    remote_browser.close = AsyncMock()
    context = MagicMock()
    context.close = AsyncMock()
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    browser._browser = remote_browser
    browser._context = context
    browser._page = MagicMock()
    browser._playwright = playwright

    await browser.close()

    # The remote browser is terminated via the CDP Browser.close command, not
    # via browser.close() (which would only disconnect the client).
    remote_browser.new_browser_cdp_session.assert_awaited_once()
    session.send.assert_awaited_once_with("Browser.close")
    remote_browser.close.assert_not_awaited()
    context.close.assert_not_awaited()
    playwright.stop.assert_awaited_once()
    assert browser._browser is None
    assert browser._context is None


@pytest.mark.asyncio
async def test_cdp_close_non_persistent_falls_back_to_disconnect_on_error():
    browser = BrowserManager(cdp_endpoint="http://127.0.0.1:9222")
    remote_browser = MagicMock()
    remote_browser.new_browser_cdp_session = AsyncMock(
        side_effect=RuntimeError("no cdp session")
    )
    remote_browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    browser._browser = remote_browser
    browser._context = MagicMock()
    browser._page = MagicMock()
    browser._playwright = playwright

    await browser.close()

    remote_browser.new_browser_cdp_session.assert_awaited_once()
    remote_browser.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cdp_close_persistent_only_disconnects():
    browser = BrowserManager(cdp_endpoint="http://127.0.0.1:9222", cdp_persistent=True)
    session = MagicMock()
    session.send = AsyncMock()
    remote_browser = MagicMock()
    remote_browser.new_browser_cdp_session = AsyncMock(return_value=session)
    remote_browser.close = AsyncMock()
    context = MagicMock()
    context.close = AsyncMock()
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    browser._browser = remote_browser
    browser._context = context
    browser._page = MagicMock()
    browser._playwright = playwright

    await browser.close()

    remote_browser.new_browser_cdp_session.assert_not_awaited()
    remote_browser.close.assert_not_awaited()
    session.send.assert_not_awaited()
    context.close.assert_not_awaited()
    playwright.stop.assert_awaited_once()
    assert browser._browser is None
