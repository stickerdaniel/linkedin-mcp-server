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
async def test_import_storage_state_imports_all_linkedin_cookies(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    payload = {
        "cookies": [
            _make_cookie("li_at", domain=".www.linkedin.com"),
            _make_cookie("JSESSIONID", domain="www.linkedin.com"),
            _make_cookie("_px3", domain="www.linkedin.com"),
            _make_cookie("li_theme", domain=".www.linkedin.com"),
            _make_cookie("session", domain=".example.com"),
        ],
        "origins": [],
    }
    storage_state_path.write_text(json.dumps(payload))

    imported = await browser.import_storage_state(storage_state_path)

    assert imported is True
    context.add_cookies.assert_awaited_once_with(
        [
            _make_cookie("li_at"),
            _make_cookie("JSESSIONID"),
            _make_cookie("_px3"),
            _make_cookie("li_theme"),
        ]
    )


@pytest.mark.asyncio
async def test_import_storage_state_requires_li_at(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    _make_cookie("JSESSIONID", domain=".www.linkedin.com"),
                    _make_cookie("bcookie"),
                ],
                "origins": [],
            }
        )
    )

    imported = await browser.import_storage_state(storage_state_path)

    assert imported is False
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialize_storage_state_auth_warms_cookies_via_temporary_context(
    tmp_path,
):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    _make_cookie("li_at", domain=".www.linkedin.com"),
                    _make_cookie("JSESSIONID", domain="www.linkedin.com"),
                ],
                "origins": [],
            }
        )
    )

    temp_page = MagicMock()
    temp_page.goto = AsyncMock()
    temp_context = MagicMock()
    temp_context.new_page = AsyncMock(return_value=temp_page)
    temp_context.cookies = AsyncMock(
        return_value=[
            _make_cookie("li_at", domain=".www.linkedin.com"),
            _make_cookie("JSESSIONID", domain="www.linkedin.com"),
            _make_cookie("_px3", domain=".www.linkedin.com"),
        ]
    )
    temp_context.close = AsyncMock()
    temp_browser = MagicMock()
    temp_browser.new_context = AsyncMock(return_value=temp_context)
    temp_browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=temp_browser)
    browser._playwright = playwright

    imported = await browser.materialize_storage_state_auth(storage_state_path)

    assert imported is True
    playwright.chromium.launch.assert_awaited_once_with(**browser.launch_options)
    temp_browser.new_context.assert_awaited_once_with(
        storage_state=storage_state_path,
        viewport=browser.viewport,
    )
    temp_page.goto.assert_awaited_once_with(
        "https://www.linkedin.com/feed/",
        wait_until="domcontentloaded",
        timeout=15000,
    )
    context.add_cookies.assert_awaited_once_with(
        [
            _make_cookie("li_at"),
            _make_cookie("JSESSIONID"),
            _make_cookie("_px3"),
        ]
    )
    temp_context.close.assert_awaited_once()
    temp_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_materialize_storage_state_auth_requires_li_at_in_source_state(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [_make_cookie("JSESSIONID", domain=".www.linkedin.com")],
                "origins": [],
            }
        )
    )
    browser._playwright = MagicMock()

    imported = await browser.materialize_storage_state_auth(storage_state_path)

    assert imported is False
    context.add_cookies.assert_not_awaited()
    browser._playwright.chromium.launch.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_storage_state_auth_rejects_when_li_at_disappears_after_warming(
    tmp_path,
):
    """LinkedIn may accept the source cookies but invalidate li_at during the
    warm-up navigation (e.g. IP mismatch between macOS source and Docker).
    The method must return False and NOT inject cookies into the persistent context."""
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    _make_cookie("li_at", domain=".www.linkedin.com"),
                    _make_cookie("JSESSIONID", domain="www.linkedin.com"),
                ],
                "origins": [],
            }
        )
    )

    temp_page = MagicMock()
    temp_page.goto = AsyncMock()
    temp_context = MagicMock()
    temp_context.new_page = AsyncMock(return_value=temp_page)
    # After warming: li_at is gone, only JSESSIONID remains
    temp_context.cookies = AsyncMock(
        return_value=[
            _make_cookie("JSESSIONID", domain="www.linkedin.com"),
        ]
    )
    temp_context.close = AsyncMock()
    temp_browser = MagicMock()
    temp_browser.new_context = AsyncMock(return_value=temp_context)
    temp_browser.close = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(return_value=temp_browser)
    browser._playwright = playwright

    imported = await browser.materialize_storage_state_auth(storage_state_path)

    assert imported is False
    context.add_cookies.assert_not_awaited()
    temp_context.close.assert_awaited_once()
    temp_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_materialize_storage_state_auth_returns_false_on_temp_browser_exception(
    tmp_path,
):
    """If the temporary browser launch or navigation fails, the method must
    return False without crashing and without injecting cookies."""
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "source-storage-state.json"
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    _make_cookie("li_at", domain=".www.linkedin.com"),
                ],
                "origins": [],
            }
        )
    )

    playwright = MagicMock()
    playwright.chromium.launch = AsyncMock(
        side_effect=RuntimeError("browser launch failed")
    )
    browser._playwright = playwright

    imported = await browser.materialize_storage_state_auth(storage_state_path)

    assert imported is False
    context.add_cookies.assert_not_awaited()


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
