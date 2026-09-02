"""Tests for the strict deterministic scraping trace doubles."""

from __future__ import annotations

from typing import Any, cast

import asyncio
import logging

import pytest

from linkedin_mcp_server.scraping import extractor as extractor_module

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
)


def test_recorder_and_page_fail_fast_on_undeclared_operations():
    recorder = TraceRecorder("strict", {"evaluate"})
    page = ScriptedPage(recorder)

    with pytest.raises(AssertionError, match="undeclared operation"):
        recorder.record("navigate")
    with pytest.raises(AssertionError, match="undeclared locator"):
        page.locator("main")
    with pytest.raises(AssertionError, match="unrecognized evaluate program"):
        asyncio.run(page.evaluate("() => window.unknownPolicySurface"))


def test_listener_removal_requires_the_registered_callback_identity():
    recorder = TraceRecorder("listener-identity", {"listener.add", "listener.remove"})
    page = ScriptedPage(recorder)

    def registered(_value: Any) -> None:
        return None

    def equal_behavior(_value: Any) -> None:
        return None

    page.on("response", registered)
    with pytest.raises(AssertionError, match="callback identity was not registered"):
        page.remove_listener("response", equal_behavior)
    page.remove_listener("response", registered)
    page.assert_clean()

    assert [event["callback_id"] for event in recorder.events] == [
        "callback-1",
        "callback-1",
    ]


async def test_response_body_accounting_covers_success_failure_and_pending_reads():
    allowed = {"response.body.start", "response.body.finish"}
    recorder = TraceRecorder("response-accounting", allowed)
    release = asyncio.Event()
    blocked = ScriptedResponse(
        recorder, "https://example.test/blocked", b"ok", release=release
    )

    task = asyncio.create_task(blocked.body())
    await asyncio.sleep(0)
    assert recorder.pending_response_reads == 1
    release.set()
    assert await task == b"ok"
    recorder.assert_no_pending_reads()

    failed = ScriptedResponse(
        recorder, "https://example.test/failed", RuntimeError("body failed")
    )
    with pytest.raises(RuntimeError, match="body failed"):
        await failed.body()
    recorder.assert_no_pending_reads()
    assert [event["kind"] for event in recorder.events] == [
        "response.body.start",
        "response.body.finish",
        "response.body.start",
        "response.body.finish",
    ]


async def test_fake_clock_advances_without_wall_clock_delay():
    recorder = TraceRecorder("clock", {"sleep"})
    clock = FakeClock(recorder, start=10.0)

    await clock.sleep(2.5)

    assert clock.monotonic() == 12.5
    assert recorder.events == [
        {"seq": 1, "kind": "sleep", "reason": "sleep", "seconds": 2.5}
    ]


async def test_listener_drain_waits_two_seconds_then_cancels_with_one_second_cap(
    monkeypatch,
):
    waits: list[float | None] = []
    wait_for_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def blocked() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(blocked())

    async def wait(
        pending: Any, *, timeout: float | None = None
    ) -> tuple[set[Any], set[Any]]:
        waits.append(timeout)
        return set(), set(pending)

    async def wait_for(value: Any, timeout: float | None = None) -> Any:
        wait_for_timeouts.append(timeout)
        return await real_wait_for(value, timeout=timeout)

    monkeypatch.setattr(extractor_module.asyncio, "wait", wait)
    monkeypatch.setattr(extractor_module.asyncio, "wait_for", wait_for)

    await extractor_module._drain_listener_tasks([task])

    assert task.cancelled()
    assert waits == [2.0]
    assert wait_for_timeouts == [1.0]


async def test_listener_drain_logs_an_uncooperative_task(monkeypatch, caplog):
    class PendingTask:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def done(self) -> bool:
            return False

    task = PendingTask()

    async def wait(
        _pending: Any, *, timeout: float | None = None
    ) -> tuple[set[Any], set[Any]]:
        assert timeout == 2.0
        return set(), {task}

    def gather(*pending: Any, return_exceptions: bool = False) -> object:
        assert pending == (task,)
        assert return_exceptions is True
        return object()

    async def wait_for(value: Any, timeout: float | None = None) -> None:
        assert value is not None
        assert timeout == 1.0
        raise asyncio.TimeoutError

    monkeypatch.setattr(extractor_module.asyncio, "wait", wait)
    monkeypatch.setattr(extractor_module.asyncio, "gather", gather)
    monkeypatch.setattr(extractor_module.asyncio, "wait_for", wait_for)

    with caplog.at_level(logging.WARNING):
        await extractor_module._drain_listener_tasks([cast(asyncio.Task[None], task)])

    assert task.cancelled is True
    assert "leaking 1 task(s)" in caplog.text
