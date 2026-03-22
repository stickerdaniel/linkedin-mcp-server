"""Tests for stealth/anti-detection utilities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.core.stealth import (
    get_stealth_init_scripts,
    hover_random_links,
    random_mouse_move,
)


# --- get_stealth_init_scripts ---


def test_get_stealth_init_scripts_returns_list_of_strings():
    scripts = get_stealth_init_scripts()
    assert isinstance(scripts, list)
    assert len(scripts) > 0
    for script in scripts:
        assert isinstance(script, str)
        assert len(script) > 0


def test_stealth_scripts_contain_webdriver_patch():
    scripts = get_stealth_init_scripts()
    assert any("navigator.webdriver" in s or "webdriver" in s for s in scripts)


def test_stealth_scripts_contain_plugins_injection():
    scripts = get_stealth_init_scripts()
    assert any("plugins" in s for s in scripts)


def test_stealth_scripts_contain_webgl_spoof():
    scripts = get_stealth_init_scripts()
    assert any("getParameter" in s or "WebGL" in s for s in scripts)


def test_stealth_scripts_contain_memory_patch():
    scripts = get_stealth_init_scripts()
    assert any("performance.memory" in s or "jsHeapSizeLimit" in s for s in scripts)


def test_stealth_scripts_contain_user_agent_patch():
    scripts = get_stealth_init_scripts()
    assert any("HeadlessChrome" in s for s in scripts)


def test_stealth_scripts_contain_device_memory_patch():
    scripts = get_stealth_init_scripts()
    assert any("deviceMemory" in s for s in scripts)


def test_stealth_scripts_do_not_patch_connection():
    """navigator.connection should NOT be patched — real Chrome exposes it."""
    scripts = get_stealth_init_scripts()
    assert not any("navigator.connection" in s for s in scripts)


# --- random_mouse_move ---


@pytest.mark.asyncio
async def test_random_mouse_move_calls_mouse_move():
    page = MagicMock()
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()

    with patch(
        "linkedin_mcp_server.core.stealth.asyncio.sleep", new_callable=AsyncMock
    ):
        await random_mouse_move(page, count=3)

    assert page.mouse.move.call_count == 3
    for call in page.mouse.move.call_args_list:
        x, y = call.args
        assert 0 <= x < 1280
        assert 0 <= y < 720


@pytest.mark.asyncio
async def test_random_mouse_move_handles_no_viewport():
    page = MagicMock()
    page.viewport_size = None
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()

    await random_mouse_move(page)

    page.mouse.move.assert_not_called()


@pytest.mark.asyncio
async def test_random_mouse_move_handles_exception():
    page = MagicMock()
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock(side_effect=RuntimeError("browser crashed"))

    # Should not raise
    await random_mouse_move(page, count=2)


# --- hover_random_links ---


@pytest.mark.asyncio
async def test_hover_random_links_hovers_elements():
    link1 = MagicMock()
    link1.hover = AsyncMock()
    link2 = MagicMock()
    link2.hover = AsyncMock()

    page = MagicMock()
    page.query_selector_all = AsyncMock(return_value=[link1, link2])

    with patch(
        "linkedin_mcp_server.core.stealth.asyncio.sleep", new_callable=AsyncMock
    ):
        await hover_random_links(page, max_links=2)

    assert link1.hover.call_count + link2.hover.call_count == 2


@pytest.mark.asyncio
async def test_hover_random_links_handles_individual_hover_failure():
    link_ok = MagicMock()
    link_ok.hover = AsyncMock()
    link_fail = MagicMock()
    link_fail.hover = AsyncMock(side_effect=RuntimeError("detached"))

    page = MagicMock()
    page.query_selector_all = AsyncMock(return_value=[link_fail, link_ok])

    with patch(
        "linkedin_mcp_server.core.stealth.asyncio.sleep", new_callable=AsyncMock
    ):
        await hover_random_links(page, max_links=2)

    # Both links attempted; one failed but the other still hovered
    assert link_fail.hover.call_count == 1
    assert link_ok.hover.call_count == 1


@pytest.mark.asyncio
async def test_hover_random_links_handles_no_links():
    page = MagicMock()
    page.query_selector_all = AsyncMock(return_value=[])

    # Should not raise
    await hover_random_links(page)


@pytest.mark.asyncio
async def test_hover_random_links_handles_exception():
    page = MagicMock()
    page.query_selector_all = AsyncMock(side_effect=RuntimeError("page closed"))

    # Should not raise
    await hover_random_links(page)
