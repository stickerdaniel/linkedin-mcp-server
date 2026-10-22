"""``tool_calls_per_minute`` and ``tool_calls_per_day``: per-tool call limits.

Driven through the real ``SequentialToolExecutionMiddleware`` and a profile
lease on the test's temporary auth root, with a stand-in for the tool body, so
a refusal is seen the way a client sees it and "nothing reached LinkedIn" means
the body never ran.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server import tool_call_limits
from linkedin_mcp_server.config import get_config, set_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.profile_lease import ProfileLease, get_profile_lease
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)

START = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wall clock the limits read, as a value the test moves."""
    now = [START]
    monkeypatch.setattr(tool_call_limits.time, "time", lambda: now[0])
    return now


def _limits(*, per_minute: int = 0, per_day: int = 0) -> None:
    config = AppConfig()
    config.browser.tool_calls_per_minute = per_minute
    config.browser.tool_calls_per_day = per_day
    set_config(config)


async def _call(tool: str = "get_person_profile", *, middleware=None) -> AsyncMock:
    """One call through the middleware; returns the stand-in tool body."""
    context = MagicMock(fastmcp_context=None)
    context.message.name = tool
    body = AsyncMock(return_value="result")
    await (middleware or SequentialToolExecutionMiddleware()).on_call_tool(
        context, body
    )
    return body


def _state(profile: Path) -> Path:
    return profile.parent / tool_call_limits.STATE_FILE


async def test_off_by_default_counts_nothing(clock, isolate_profile_dir):
    set_config(AppConfig())
    for _ in range(50):
        await _call()

    assert not _state(isolate_profile_dir).exists()


async def test_the_call_over_the_minute_limit_is_refused_before_the_tool_runs(
    clock,
):
    _limits(per_minute=3)
    middleware = SequentialToolExecutionMiddleware()
    for _ in range(3):
        (await _call(middleware=middleware)).assert_awaited_once()
        clock[0] += 1

    body = AsyncMock()
    context = MagicMock(fastmcp_context=None)
    context.message.name = "get_person_profile"
    with pytest.raises(ToolError) as refused:
        await middleware.on_call_tool(context, body)

    body.assert_not_awaited()
    message = str(refused.value)
    assert "TOOL_CALLS_PER_MINUTE" in message
    assert "nothing was sent to LinkedIn" in message
    # The first of the three leaves the window 60 s after it was made.
    assert "2027-01-15T08:01:00Z" in message
    assert "in 57 seconds" in message


async def test_a_refused_call_lets_go_of_the_profile(clock, isolate_profile_dir):
    # Another process waiting for the browser must not be held off by a call
    # that was never going to run.
    _limits(per_minute=1)
    await _call()
    with pytest.raises(ToolError):
        await _call()

    assert not get_profile_lease(isolate_profile_dir).held


async def test_a_call_that_never_gets_the_browser_is_not_counted(
    clock, isolate_profile_dir
):
    # Counting before the lease would spend the allowance on a call that only
    # learned the browser was busy.
    _limits(per_minute=1)
    get_config().browser.browser_wait_seconds = 0
    other_process = ProfileLease(isolate_profile_dir.parent)
    assert other_process.try_acquire()
    try:
        with pytest.raises(ToolError, match="using the browser"):
            await _call()
    finally:
        other_process.release()

    (await _call()).assert_awaited_once()


async def test_each_tool_is_counted_on_its_own(clock):
    _limits(per_minute=2)
    for _ in range(2):
        await _call("get_person_profile")
    with pytest.raises(ToolError, match="get_person_profile"):
        await _call("get_person_profile")

    (await _call("get_company_profile")).assert_awaited_once()


async def test_a_tool_that_never_loads_linkedin_is_never_counted(
    clock, isolate_profile_dir
):
    _limits(per_minute=1, per_day=1)
    for _ in range(5):
        (await _call("close_session")).assert_awaited_once()

    assert not _state(isolate_profile_dir).exists()


async def test_the_window_slides(clock):
    _limits(per_minute=2)
    await _call()
    clock[0] += 30
    await _call()
    clock[0] += 29
    with pytest.raises(ToolError):
        await _call()

    clock[0] += 1  # the first call is now 60 s old
    (await _call()).assert_awaited_once()


async def test_the_day_limit_outlasts_the_minute(clock):
    _limits(per_minute=10, per_day=2)
    await _call()
    clock[0] += 3600
    await _call()
    clock[0] += 3600
    with pytest.raises(ToolError) as refused:
        await _call()

    assert "TOOL_CALLS_PER_DAY" in str(refused.value)
    assert "in 22 hours 0 minutes" in str(refused.value)
    clock[0] += 22 * 3600
    (await _call()).assert_awaited_once()


async def test_with_both_limits_reached_the_later_one_is_named(clock):
    # Naming the minute would send the caller back in a minute to a call the
    # day limit still refuses.
    _limits(per_minute=1, per_day=1)
    await _call()
    with pytest.raises(ToolError) as refused:
        await _call()

    assert "TOOL_CALLS_PER_DAY" in str(refused.value)
    assert "in 24 hours 0 minutes" in str(refused.value)


async def test_a_restart_keeps_the_count(clock, isolate_profile_dir):
    _limits(per_day=2)
    for _ in range(2):
        await _call(middleware=SequentialToolExecutionMiddleware())

    # A new server process: new middleware, new lease object, same auth root.
    from linkedin_mcp_server.profile_lease import reset_leases_for_testing

    reset_leases_for_testing()
    with pytest.raises(ToolError, match="TOOL_CALLS_PER_DAY"):
        await _call(middleware=SequentialToolExecutionMiddleware())


async def test_the_count_is_private_to_this_account(clock, isolate_profile_dir):
    _limits(per_minute=5)
    await _call()

    path = _state(isolate_profile_dir)
    assert json.loads(path.read_text())["calls"] == {"get_person_profile": [START]}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_an_unreadable_count_starts_again_from_zero(
    clock, isolate_profile_dir, caplog
):
    # Refusing every call until somebody deletes the file would turn an opt-in
    # limit into an outage.
    _limits(per_minute=1)
    path = _state(isolate_profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    (await _call()).assert_awaited_once()

    assert "counting from zero" in caplog.text
    assert json.loads(path.read_text())["calls"] == {"get_person_profile": [START]}


async def test_a_call_from_the_future_counts_as_made_now(clock, isolate_profile_dir):
    # The clock went back an hour. The stamp keeps its place for one window,
    # not for the hour it takes the clock to catch up.
    _limits(per_minute=1)
    await _call()
    clock[0] -= 3600
    with pytest.raises(ToolError, match="in 60 seconds"):
        await _call()

    clock[0] += 60
    (await _call()).assert_awaited_once()
