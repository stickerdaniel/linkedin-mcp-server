"""Tests for background navigation module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import linkedin_mcp_server.core.background_nav as mod
from linkedin_mcp_server.core.background_nav import (
    _get_random_sites,
    _random_google_url,
    _visit_site,
    start_background_navigation,
    stop_background_navigation,
)


@pytest.fixture(autouse=True)
def _reset_bg_task():
    """Ensure _bg_task is None before and after each test."""
    mod._bg_task = None
    yield
    if mod._bg_task is not None:
        mod._bg_task.cancel()
        mod._bg_task = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_controlled_sleep(*, max_calls: int = 1):
    """Return an async sleep replacement that raises CancelledError after max_calls."""
    call_count = 0

    async def _sleep(_duration):
        nonlocal call_count
        call_count += 1
        if call_count > max_calls:
            raise asyncio.CancelledError

    return _sleep


# ---------------------------------------------------------------------------
# start_background_navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_background_navigation_creates_task():
    """Verify asyncio task is created and _bg_task is set."""
    page = MagicMock()

    async def fake_loop(_page):
        await asyncio.sleep(9999)

    with patch.object(mod, "_background_navigation_loop", side_effect=fake_loop):
        await start_background_navigation(page)
        assert mod._bg_task is not None
        assert not mod._bg_task.done()


@pytest.mark.asyncio
async def test_start_background_navigation_is_idempotent():
    """Call start twice, verify first task is cancelled before new one starts."""
    page = MagicMock()

    async def fake_loop(_page):
        await asyncio.sleep(9999)

    with patch.object(mod, "_background_navigation_loop", side_effect=fake_loop):
        await start_background_navigation(page)
        first_task = mod._bg_task

        await start_background_navigation(page)
        second_task = mod._bg_task

        assert first_task is not second_task
        assert first_task.cancelled() or first_task.done()
        assert second_task is not None


# ---------------------------------------------------------------------------
# stop_background_navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_background_navigation_cancels_task():
    """Verify task.cancel() is called and the task is awaited."""
    cancelled = False

    async def long_coro():
        nonlocal cancelled
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            cancelled = True
            raise

    mod._bg_task = asyncio.create_task(long_coro())
    await asyncio.sleep(0)  # let the task start

    await stop_background_navigation()

    assert cancelled
    assert mod._bg_task is None


@pytest.mark.asyncio
async def test_stop_background_navigation_noop_when_no_task():
    """Verify no error when called with no running task."""
    mod._bg_task = None
    await stop_background_navigation()
    assert mod._bg_task is None


@pytest.mark.asyncio
async def test_stop_background_navigation_noop_when_task_done():
    """Verify no error when task is already done."""
    future = asyncio.get_event_loop().create_future()
    future.set_result(None)
    mod._bg_task = asyncio.ensure_future(future)
    await asyncio.sleep(0)

    await stop_background_navigation()
    assert mod._bg_task is None


@pytest.mark.asyncio
async def test_stop_catches_cancelled_error():
    """Verify CancelledError is caught during stop — no exception escapes."""
    page = MagicMock()

    async def fake_loop(_page):
        await asyncio.sleep(9999)

    with patch.object(mod, "_background_navigation_loop", side_effect=fake_loop):
        await start_background_navigation(page)
        assert mod._bg_task is not None

        # Must not raise
        await stop_background_navigation()
        assert mod._bg_task is None


# ---------------------------------------------------------------------------
# _background_navigation_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_background_loop_acquires_lock():
    """Verify browser_lock is acquired during navigation."""
    page = MagicMock()
    page.goto = AsyncMock()

    lock_acquired = False
    real_lock = asyncio.Lock()
    original_acquire = real_lock.acquire

    async def tracking_acquire():
        nonlocal lock_acquired
        result = await original_acquire()
        lock_acquired = True
        return result

    real_lock.acquire = tracking_acquire

    with (
        patch.object(mod, "browser_lock", real_lock),
        patch.object(mod, "_visit_site", new_callable=AsyncMock),
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            side_effect=_make_controlled_sleep(max_calls=1),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._background_navigation_loop(page)

    assert lock_acquired


@pytest.mark.asyncio
async def test_background_loop_skips_when_lock_held():
    """When browser_lock.locked() returns True the cycle is skipped."""
    page = MagicMock()
    page.goto = AsyncMock()

    mock_lock = MagicMock()
    mock_lock.locked.return_value = True

    visit_called = False

    async def spy_visit(*args, **kwargs):
        nonlocal visit_called
        visit_called = True

    with (
        patch.object(mod, "browser_lock", mock_lock),
        patch.object(mod, "_visit_site", side_effect=spy_visit),
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            side_effect=_make_controlled_sleep(max_calls=1),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._background_navigation_loop(page)

    assert not visit_called
    mock_lock.locked.assert_called()


@pytest.mark.asyncio
async def test_background_loop_returns_to_blank():
    """Verify page.goto('about:blank') called after a navigation cycle."""
    page = MagicMock()
    page.goto = AsyncMock()

    real_lock = asyncio.Lock()

    with (
        patch.object(mod, "browser_lock", real_lock),
        patch.object(mod, "_visit_site", new_callable=AsyncMock),
        patch.object(mod, "_get_random_sites", return_value=["https://example.com"]),
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            side_effect=_make_controlled_sleep(max_calls=1),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._background_navigation_loop(page)

    blank_calls = [c for c in page.goto.call_args_list if c.args[0] == "about:blank"]
    assert len(blank_calls) == 1


@pytest.mark.asyncio
async def test_background_loop_handles_about_blank_error():
    """If page.goto('about:blank') raises, the loop continues gracefully."""
    page = MagicMock()
    page.goto = AsyncMock(side_effect=Exception("goto failed"))

    real_lock = asyncio.Lock()

    with (
        patch.object(mod, "browser_lock", real_lock),
        patch.object(mod, "_visit_site", new_callable=AsyncMock),
        patch.object(mod, "_get_random_sites", return_value=["https://example.com"]),
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            side_effect=_make_controlled_sleep(max_calls=1),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._background_navigation_loop(page)

    # page.goto was attempted for about:blank even though it raised
    page.goto.assert_called()


@pytest.mark.asyncio
async def test_background_loop_handles_cycle_error():
    """If the entire cycle raises (e.g. lock error), the loop continues."""
    page = MagicMock()

    mock_lock = MagicMock()
    mock_lock.locked.return_value = False

    # Make the async context manager raise
    async def raise_on_enter():
        raise RuntimeError("lock broken")

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("lock broken"))
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_lock.__aenter__ = ctx.__aenter__
    mock_lock.__aexit__ = ctx.__aexit__

    with (
        patch.object(mod, "browser_lock", mock_lock),
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            side_effect=_make_controlled_sleep(max_calls=1),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await mod._background_navigation_loop(page)

    # The loop survived the RuntimeError and only stopped due to CancelledError
    mock_lock.locked.assert_called()


# ---------------------------------------------------------------------------
# _visit_site
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_visit_site_handles_navigation_error():
    """page.goto raises — _visit_site must not crash."""
    page = MagicMock()
    page.goto = AsyncMock(side_effect=Exception("Network error"))

    with patch(
        "linkedin_mcp_server.core.background_nav.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        # Must not raise
        await _visit_site(page, "https://example.com")


@pytest.mark.asyncio
async def test_visit_site_performs_interactions():
    """Verify mouse.wheel and random_mouse_move are called."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()

    mock_random_mouse_move = AsyncMock()
    mock_hover_random_links = AsyncMock()

    # random_mouse_move and hover_random_links are imported locally inside
    # _visit_site via ``from .stealth import ...``, so patch in stealth module.
    with (
        patch(
            "linkedin_mcp_server.core.background_nav.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch(
            "linkedin_mcp_server.core.background_nav.random.randint",
            return_value=2,
        ),
        patch(
            "linkedin_mcp_server.core.background_nav.random.uniform",
            return_value=1.0,
        ),
        patch(
            "linkedin_mcp_server.core.stealth.random_mouse_move",
            mock_random_mouse_move,
        ),
        patch(
            "linkedin_mcp_server.core.stealth.hover_random_links",
            mock_hover_random_links,
        ),
    ):
        await _visit_site(page, "https://example.com")

    page.goto.assert_called_once()
    mock_random_mouse_move.assert_called_once()
    assert page.mouse.wheel.call_count == 2


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_get_random_sites_returns_correct_count():
    """Verify returns list of correct length."""
    sites = _get_random_sites(count=2)
    assert len(sites) == 2
    assert all(isinstance(s, str) for s in sites)


def test_get_random_sites_returns_single():
    """Verify count=1 works."""
    sites = _get_random_sites(count=1)
    assert len(sites) == 1


def test_random_google_url_format():
    """Verify URL starts with the expected Google search prefix."""
    url = _random_google_url()
    assert url.startswith("https://www.google.com/search?q=")
    assert " " not in url
