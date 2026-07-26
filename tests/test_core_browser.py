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
    context.cookies = AsyncMock()
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
@pytest.mark.parametrize(
    "cookies",
    [
        [_make_cookie("JSESSIONID")],
        [_make_cookie("li_at", ""), _make_cookie("JSESSIONID")],
        [_make_cookie("li_at", "   "), _make_cookie("JSESSIONID")],
        [_make_cookie("li_at", "wrong-domain", domain=".notlinkedin.com")],
    ],
    ids=["missing-li-at", "empty-li-at", "blank-li-at", "wrong-domain-li-at"],
)
async def test_export_cookies_rejects_unusable_session(
    tmp_path, cookies: list[dict[str, str]], caplog
):
    browser, context = _make_browser_manager(tmp_path)
    context.cookies.return_value = cookies
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text("last-known-good")

    exported = await browser.export_cookies(cookie_path)

    assert exported is False
    assert cookie_path.read_text() == "last-known-good"
    assert "li_at is missing or empty" in caplog.text


@pytest.mark.asyncio
async def test_export_cookies_writes_usable_linkedin_session(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    context.cookies.return_value = [
        _make_cookie("li_at", "session-token", domain=".www.linkedin.com"),
        _make_cookie("JSESSIONID"),
        _make_cookie("unrelated", domain=".example.com"),
    ]
    cookie_path = tmp_path / "cookies.json"

    exported = await browser.export_cookies(cookie_path)

    assert exported is True
    assert json.loads(cookie_path.read_text()) == [
        _make_cookie("li_at", "session-token"),
        _make_cookie("JSESSIONID"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_first", [True, False])
async def test_export_cookies_drops_empty_duplicate_li_at(tmp_path, valid_first):
    browser, context = _make_browser_manager(tmp_path)
    valid = _make_cookie("li_at", "session-token")
    empty = _make_cookie("li_at", "", domain=".www.linkedin.com")
    context.cookies.return_value = [valid, empty] if valid_first else [empty, valid]
    cookie_path = tmp_path / "cookies.json"

    exported = await browser.export_cookies(cookie_path)

    assert exported is True
    assert json.loads(cookie_path.read_text()) == [valid]


@pytest.mark.asyncio
async def test_export_cookies_preserves_partitioned_cookie_identity(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    partitioned = {
        **_make_cookie("bcookie", "partitioned"),
        "partitionKey": "https://example.com",
    }
    context.cookies.return_value = [
        _make_cookie("li_at", "session-token"),
        _make_cookie("bcookie", "unpartitioned"),
        partitioned,
    ]
    cookie_path = tmp_path / "cookies.json"

    exported = await browser.export_cookies(cookie_path)

    assert exported is True
    assert json.loads(cookie_path.read_text()) == [
        _make_cookie("li_at", "session-token"),
        _make_cookie("bcookie", "unpartitioned"),
        partitioned,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
async def test_import_cookies_rejects_empty_li_at(tmp_path, value, caplog):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([_make_cookie("li_at", value), _make_cookie("JSESSIONID")])
    )

    imported = await browser.import_cookies(cookie_path)

    assert imported is False
    context.add_cookies.assert_not_awaited()
    assert "No non-empty li_at cookie found" in caplog.text


@pytest.mark.asyncio
async def test_import_cookies_ignores_non_linkedin_li_at(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([_make_cookie("li_at", "wrong", domain=".notlinkedin.com")])
    )

    imported = await browser.import_cookies(cookie_path)

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
