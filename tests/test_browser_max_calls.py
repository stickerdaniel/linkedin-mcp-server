"""``browser_max_calls``: close the browser between calls after N of them.

A busy client never leaves the idle timeout a gap, so without this setting one
page serves every call until the process exits, and in a memory-limited
container its renderer grows until the OOM killer ends it. The close goes
through the same conditional path as the idle timeout and the handoff, so it
only ever happens between calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config import reset_config
from linkedin_mcp_server.drivers import browser as browser_module
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)


def _call_context() -> MagicMock:
    context = MagicMock()
    context.message.name = "get_person_profile"
    context.fastmcp_context = None
    return context


def _open_browser(profile: Path) -> MagicMock:
    """Install a fake live browser holding *profile*'s lease, as a launch does."""
    lease = get_profile_lease(profile)
    assert lease.try_acquire()
    lease.mark_browser_open()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock(return_value=True)
    browser_module._browser = fake_browser
    browser_module._browser_lease = lease
    return fake_browser


@pytest.fixture
def middleware(tmp_path: Path):
    profile = tmp_path / "profile"
    with patch(
        "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
        return_value=get_profile_lease(profile),
    ):
        yield SequentialToolExecutionMiddleware()


async def _calls(middleware: SequentialToolExecutionMiddleware, count: int) -> None:
    for _ in range(count):
        await middleware.on_call_tool(_call_context(), AsyncMock())


async def test_the_browser_closes_after_its_allowance_of_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, middleware
) -> None:
    monkeypatch.setenv("BROWSER_MAX_CALLS", "3")
    reset_config()
    fake_browser = _open_browser(tmp_path / "profile")

    await _calls(middleware, 2)
    fake_browser.close.assert_not_awaited()
    assert browser_module._browser is fake_browser

    await _calls(middleware, 1)
    fake_browser.close.assert_awaited_once()
    assert browser_module._browser is None, "the next call would reuse the page"
    assert not get_profile_lease(tmp_path / "profile").browser_open


async def test_each_new_browser_gets_the_whole_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, middleware
) -> None:
    monkeypatch.setenv("BROWSER_MAX_CALLS", "2")
    reset_config()
    first = _open_browser(tmp_path / "profile")
    await _calls(middleware, 2)
    first.close.assert_awaited_once()

    second = _open_browser(tmp_path / "profile")
    await _calls(middleware, 1)
    second.close.assert_not_awaited()
    await _calls(middleware, 1)
    second.close.assert_awaited_once()


async def test_off_by_default(tmp_path: Path, middleware) -> None:
    reset_config()
    fake_browser = _open_browser(tmp_path / "profile")

    await _calls(middleware, 50)

    fake_browser.close.assert_not_awaited()
    assert browser_module._browser is fake_browser


async def test_a_running_call_is_never_cut_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller runs outside the tool-call lock and may see the cap first."""
    monkeypatch.setenv("BROWSER_MAX_CALLS", "1")
    reset_config()
    fake_browser = _open_browser(tmp_path / "profile")

    browser_module.note_call_started()
    browser_module.note_activity()  # the allowance is used up
    browser_module.note_call_started()  # and the next call already started

    assert not await browser_module.release_profile_if_idle_or_requested()
    fake_browser.close.assert_not_awaited()

    browser_module.note_activity()
    assert await browser_module.release_profile_if_idle_or_requested()
    fake_browser.close.assert_awaited_once()


async def test_a_call_without_a_browser_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, middleware
) -> None:
    monkeypatch.setenv("BROWSER_MAX_CALLS", "1")
    reset_config()
    await _calls(middleware, 1)  # failed before any browser started

    fake_browser = _open_browser(tmp_path / "profile")

    assert not await browser_module.release_profile_if_idle_or_requested()
    fake_browser.close.assert_not_awaited()
