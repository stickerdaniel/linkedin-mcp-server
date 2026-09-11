"""Tests for the strict deterministic scraping trace doubles."""

from __future__ import annotations

from typing import Any

import asyncio

import pytest

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    ScriptedResponse,
    TraceRecorder,
    semantic_program_id,
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


@pytest.mark.parametrize(
    "program",
    [
        "() => { const state = {}; state.editor.focus(); }",
        "() => document.querySelector('main a[href*=\"/messaging/compose/\"]')",
        "selector => document.querySelectorAll(selector)",
        "arg => arg.previous + 1",
        "expected => { const needle = normalize(expected); return needle; }",
    ],
)
def test_retired_messaging_programs_are_rejected(program):
    with pytest.raises(AssertionError, match="unrecognized evaluate program"):
        semantic_program_id(program)


def test_page_rejects_unused_required_script_outcomes():
    recorder = TraceRecorder("unused-script", {"evaluate"})
    page = ScriptedPage(recorder).script(
        "evaluate:root_content", {"source": "root", "text": "unused"}
    )

    with pytest.raises(AssertionError, match="scripted outcomes unused"):
        page.assert_clean()


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
    assert recorder.events == [{"kind": "sleep", "reason": "sleep", "seconds": 2.5}]
