"""Tests for BrowserManager cookie import/export helpers."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import linkedin_mcp_server.core.browser as browser_module
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import BrowserTeardownError, NetworkError


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


def test_camoufox_manager_ignores_partial_user_agent_override(tmp_path):
    browser = BrowserManager(
        user_data_dir=tmp_path / "profile",
        engine="camoufox",
        user_agent="Mozilla/5.0 Chrome/150.0.0.0",
    )

    assert browser.user_agent is None


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
async def test_import_cookies_rejects_lookalike_linkedin_domain(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([_make_cookie("li_at", domain="linkedin.com.evil.test")])
    )

    assert await browser.import_cookies(cookie_path) is False
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("li_at_value", ["", "   "])
async def test_import_cookies_rejects_empty_li_at(tmp_path, li_at_value):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([_make_cookie("li_at", li_at_value)]))

    imported = await browser.import_cookies(cookie_path)

    assert imported is False
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["not json", "{}", '[{"name": "li_at"}, "broken-entry"]'],
)
async def test_import_cookies_returns_false_for_invalid_snapshot(tmp_path, payload):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(payload)

    assert await browser.import_cookies(cookie_path) is False
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_cookies_propagates_driver_failure_as_network_error(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([_make_cookie("li_at", "session")]))
    driver_error = RuntimeError("Connection closed while injecting cookies")
    context.add_cookies = AsyncMock(side_effect=driver_error)

    with pytest.raises(NetworkError, match="Browser driver failed") as exc_info:
        await browser.import_cookies(cookie_path)

    assert exc_info.value.__cause__ is driver_error


@pytest.mark.asyncio
async def test_import_cookies_without_context_is_infrastructure_failure(tmp_path):
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([_make_cookie("li_at", "session")]))

    with pytest.raises(NetworkError, match="no browser context"):
        await browser.import_cookies(cookie_path)


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
async def test_refresh_imported_snapshot_merges_rotations_without_reviving_deletions(
    tmp_path,
):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("li_at", "old-li-at"),
                _make_cookie("JSESSIONID", "old-jsession"),
                _make_cookie("li_rm", "removed-by-linkedin"),
                _make_cookie("custom_full_set", "preserved"),
            ]
        )
    )
    context.cookies = AsyncMock(
        return_value=[
            _make_cookie("li_at", "rotated-li-at", domain="www.linkedin.com"),
            _make_cookie("JSESSIONID", "rotated-jsession"),
            _make_cookie("new_after_feed", "new"),
            _make_cookie("unrelated", "ignored", domain="example.com"),
        ]
    )

    refreshed = await browser.refresh_imported_cookie_snapshot(
        cookie_path, preset_name="bridge_core"
    )

    assert refreshed is True
    cookies = json.loads(cookie_path.read_text())
    by_name = {cookie["name"]: cookie for cookie in cookies}
    assert by_name["li_at"]["value"] == "rotated-li-at"
    assert by_name["li_at"]["domain"] == ".linkedin.com"
    assert by_name["JSESSIONID"]["value"] == "rotated-jsession"
    assert by_name["custom_full_set"]["value"] == "preserved"
    assert by_name["new_after_feed"]["value"] == "new"
    assert "li_rm" not in by_name
    assert "unrelated" not in by_name


@pytest.mark.asyncio
async def test_runtime_refresh_preserves_cookie_outside_injected_default(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("li_at", "old"),
                _make_cookie("li_rm", "not-injected-by-auth-minimal"),
            ]
        )
    )
    context.cookies = AsyncMock(return_value=[_make_cookie("li_at", "rotated")])

    assert await browser.refresh_imported_cookie_snapshot(cookie_path) is True

    by_name = {cookie["name"]: cookie for cookie in json.loads(cookie_path.read_text())}
    assert by_name["li_at"]["value"] == "rotated"
    assert by_name["li_rm"]["value"] == "not-injected-by-auth-minimal"


@pytest.mark.asyncio
async def test_refresh_imported_snapshot_without_post_feed_li_at_preserves_file(
    tmp_path,
):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    original = json.dumps(
        [
            _make_cookie("li_at", "original"),
            _make_cookie("JSESSIONID", "original"),
        ]
    ).encode()
    cookie_path.write_bytes(original)
    context.cookies = AsyncMock(return_value=[_make_cookie("bcookie", "fresh")])

    refreshed = await browser.refresh_imported_cookie_snapshot(cookie_path)

    assert refreshed is False
    assert cookie_path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cookies",
    [
        [_make_cookie("JSESSIONID")],
        [_make_cookie("li_at", "")],
        [_make_cookie("li_at", "   ")],
    ],
    ids=["missing", "empty", "whitespace"],
)
async def test_export_cookies_requires_nonempty_li_at_and_preserves_snapshot(
    tmp_path, cookies
):
    browser, context = _make_browser_manager(tmp_path)
    context.cookies = AsyncMock(return_value=cookies)
    cookie_path = tmp_path / "cookies.json"
    original_snapshot = b'[{"name": "li_at", "value": "existing"}]\n'
    cookie_path.write_bytes(original_snapshot)

    exported = await browser.export_cookies(cookie_path)

    assert exported is False
    assert cookie_path.read_bytes() == original_snapshot


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
async def test_export_storage_state_skips_indexed_db_for_camoufox(tmp_path):
    """CamoufoxAdapter.supports_indexed_db is False -- export_storage_state
    must not pass indexed_db for that engine (see core.engines)."""
    browser = BrowserManager(user_data_dir=tmp_path / "profile", engine="camoufox")
    context = MagicMock()
    context.storage_state = AsyncMock()
    browser._context = context
    storage_state_path = tmp_path / "storage-state.json"

    exported = await browser.export_storage_state(storage_state_path, indexed_db=True)

    assert exported is True
    context.storage_state.assert_awaited_once_with(path=storage_state_path)


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

    assert await browser.close() is False
    assert await browser.close() is False

    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert browser._context is None
    assert browser._page is None
    assert browser._playwright is None


@pytest.mark.asyncio
async def test_start_timeout_bounds_page_setup_and_cleans_resources(
    tmp_path, monkeypatch
):
    never_finishes = asyncio.Event()
    context = MagicMock()
    context.pages = []
    context.new_page = AsyncMock(side_effect=never_finishes.wait)
    context.close = AsyncMock()
    driver = MagicMock()
    driver.stop = AsyncMock()
    adapter = MagicMock()
    adapter.launch = AsyncMock(return_value=(driver, context))
    monkeypatch.setitem(browser_module.ENGINES, "test-timeout", adapter)
    monkeypatch.setattr(browser_module, "_LAUNCH_TIMEOUT_SECONDS", 0.01)
    browser = BrowserManager(user_data_dir=tmp_path / "profile", engine="test-timeout")

    with pytest.raises(NetworkError, match="Timed out starting browser"):
        await browser.start()

    context.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert browser._context is None
    assert browser._page is None
    assert browser._playwright is None

    # Cleanup after a timed-out start remains safely idempotent.
    assert await browser.close() is True
    context.close.assert_awaited_once()
    driver.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_adapter_launch_preserves_uncertain_teardown(
    tmp_path, monkeypatch
):
    failure = RuntimeError("persistent launch failed")
    setattr(failure, "_linkedin_mcp_launch_teardown_complete", False)
    adapter = MagicMock()
    adapter.launch = AsyncMock(side_effect=failure)
    monkeypatch.setitem(browser_module.ENGINES, "test-launch-failure", adapter)
    browser = BrowserManager(
        user_data_dir=tmp_path / "profile", engine="test-launch-failure"
    )

    with pytest.raises(BrowserTeardownError, match="ownership is uncertain"):
        await browser.start()

    # No resources were returned to BrowserManager, but the adapter could not
    # prove that its locally-started driver released the profile lock.
    assert await browser.close() is False
    with pytest.raises(RuntimeError, match="teardown was not confirmed"):
        await browser.start()


@pytest.mark.asyncio
async def test_cancelled_start_cleans_resources_and_preserves_cancellation(
    tmp_path, monkeypatch
):
    page_setup_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocked_new_page():
        page_setup_started.set()
        await never_finishes.wait()

    context = MagicMock()
    context.pages = []
    context.new_page = AsyncMock(side_effect=blocked_new_page)
    context.close = AsyncMock()
    driver = MagicMock()
    driver.stop = AsyncMock()
    adapter = MagicMock()
    adapter.launch = AsyncMock(return_value=(driver, context))
    monkeypatch.setitem(browser_module.ENGINES, "test-cancel", adapter)
    browser = BrowserManager(user_data_dir=tmp_path / "profile", engine="test-cancel")

    start_task = asyncio.create_task(browser.start())
    await page_setup_started.wait()
    start_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    context.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert browser._context is None
    assert browser._page is None
    assert browser._playwright is None


@pytest.mark.asyncio
async def test_cancel_during_close_finishes_bounded_cleanup_and_remembers_failure(
    tmp_path, monkeypatch
):
    close_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocked_close():
        close_started.set()
        await never_finishes.wait()

    context = MagicMock()
    context.close = AsyncMock(side_effect=blocked_close)
    driver = MagicMock()
    driver.stop = AsyncMock()
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    browser._context = context
    browser._playwright = driver
    browser._teardown_complete = False
    monkeypatch.setattr(browser_module, "_CLEANUP_TIMEOUT_SECONDS", 0.01)

    close_task = asyncio.create_task(browser.close())
    await close_started.wait()
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    context.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    assert browser._cleanup_task is None
    assert await browser.close() is False
    with pytest.raises(RuntimeError, match="teardown was not confirmed"):
        await browser.start()


@pytest.mark.asyncio
async def test_repeated_cancel_during_context_exit_reports_uncertain_ownership(
    tmp_path,
):
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocked_close():
        close_started.set()
        await release_close.wait()

    context = MagicMock()
    context.close = AsyncMock(side_effect=blocked_close)
    driver = MagicMock()
    driver.stop = AsyncMock()
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    browser._context = context
    browser._playwright = driver
    browser._teardown_complete = False

    exit_task = asyncio.create_task(browser.__aexit__(None, None, None))
    await close_started.wait()
    exit_task.cancel()
    await asyncio.sleep(0)
    exit_task.cancel()

    with pytest.raises(BrowserTeardownError, match="ownership is uncertain"):
        await exit_task

    assert browser.teardown_complete is False
    release_close.set()
    assert await browser.close() is True
