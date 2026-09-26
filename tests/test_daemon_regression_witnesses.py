"""Negative controls for the daemon-only regressions the default-on contract names.

Each test here asserts the behaviour decided in
``docs/decisions/2026-09-26-daemon-default-on-contract.md`` and is expected to
fail against today's daemon. That failure is the witness: release gate 1 asks
for a control that reproduces every specified regression at the current code.

Every one of them is source-model evidence. The real function runs, while the
operating system, the network and the owner are doubles, so nothing here is a
native measurement and none of it observes a signal reaching a wrong target.

The markers are strict and narrowed to ``AssertionError``: a passing body fails
the marked run as XPASS, and the fixing change removes the marker only after
validating the intended correction. This filter does not identify an
exception's origin. Setup and branch-reachability checks must fail normally,
and unrelated exceptions must not be accepted as intentional refusals.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

import linkedin_mcp_server.daemon_owner as daemon_owner
import linkedin_mcp_server.process_tree as process_tree
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import daemon_would_be_used
from linkedin_mcp_server.daemon_liveness import OwnerCallLivenessMiddleware
from linkedin_mcp_server.daemon_proxy import (
    FrontendCallHeartbeatMiddleware,
    OwnerUnreachableError,
)
from linkedin_mcp_server.server_role import (
    ServerRole,
    a_held_profile_means_this_owner_must_go,
    hard_exit_required,
    set_process_role,
)


def _context() -> Any:
    context = MagicMock()
    context.message.name = "get_person_profile"
    return context


def _require_a_refusal(refused: bool, result: object) -> None:
    """Fail normally unless the call was refused on purpose.

    A refusal is a domain error raised by the middleware, or an error result it
    returned. Any other exception is never caught by the callers, so it leaves
    as an ordinary error; a silent ``None`` or a success-shaped answer ends up
    here. Neither may pass for the fix, and neither may pass for the witness.
    """
    if refused:
        return
    if isinstance(result, ToolResult) and result.is_error:
        return
    pytest.fail(f"the call was neither refused nor answered as an error: {result!r}")


class _Spawned(BaseException):
    """Raised by the process-spawn double once it has seen the argument vector."""


class _Exited(BaseException):
    """Raised by the ``os._exit`` double, so the test outlives the exit."""


def test_an_owner_never_names_its_own_group_to_the_guardian(
    monkeypatch: pytest.MonkeyPatch,
):
    """W-GUARDIAN (P1), source-model witness, not a native measurement.

    Guards the contract bullet "The guardian receives no owner group for an
    owner process, which matches a Direct server that does not lead its process
    group". The elected owner starts its own session, so it leads its group,
    and today the guardian is handed that group to kill on a crash.
    """
    set_process_role(ServerRole.OWNER)
    spawned: list[list[str]] = []

    def spawn(argv: list[str], **_kwargs: object) -> None:
        spawned.append(list(argv))
        raise _Spawned

    monkeypatch.setattr(process_tree, "_IS_WINDOWS", False)
    monkeypatch.setattr(process_tree, "_browser_guardian_process", None)
    monkeypatch.setattr(process_tree, "_browser_guardian_control_fd", None)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(os, "getpgrp", lambda: 4242, raising=False)
    monkeypatch.setattr(subprocess, "Popen", spawn)

    with pytest.raises(_Spawned):
        process_tree.start_browser_guardian(lease_fd=99)

    if len(spawned) != 1:
        pytest.fail(f"expected one guardian spawn, saw {spawned!r}")
    # The protected owner group is the guardian's last argument.
    assert spawned[0][-1] == "0"


@pytest.mark.parametrize("platform", ["posix", "windows"])
async def test_an_owner_exiting_after_an_unconfirmed_close_sends_no_signal(
    monkeypatch: pytest.MonkeyPatch, platform: str
):
    """W-HARD-EXIT (P1), source-model witness, not a native measurement.

    Guards "Browser that will not close": after an unconfirmed close the owner
    releases the daemon lock and exits at once, and sends no signal itself. The
    crash guardian and the per-launch Jobs are what end the browser, as they do
    when a Direct host quits. Today the exit sweeps every registered POSIX group
    by number and ends the owner's own group, or terminates the adopted Job's
    members on Windows. No signal is delivered here: every one is recorded.
    """
    sent: list[tuple[str, int, int]] = []
    browser = {"alive": True}

    def kill(pid: int, sig: int) -> None:
        sent.append(("kill", pid, int(sig)))

    def killpg(group: int, sig: int) -> None:
        sent.append(("killpg", group, int(sig)))
        if group == 7001:
            browser["alive"] = False

    def exit_now(status: int) -> None:
        raise _Exited(status)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    monkeypatch.setattr(os, "_exit", exit_now)

    if platform == "posix":
        # The owner leads its group, and one detached Chromium group is filed
        # under a launch marker with a leader whose identity still matches.
        # That is the state in which today's sweep signals.
        monkeypatch.setattr(process_tree, "_IS_WINDOWS", False)
        # A Windows interpreter has no ``SIGKILL``. Every signal is intercepted
        # above, so the number only has to exist for the forced POSIX path.
        monkeypatch.setattr(process_tree, "signal", SimpleNamespace(SIGKILL=9))
        monkeypatch.setattr(os, "getpid", lambda: 5001)
        monkeypatch.setattr(os, "getpgrp", lambda: 5001, raising=False)
        monkeypatch.setattr(process_tree, "_registered_browser_markers", set())
        monkeypatch.setattr(
            process_tree,
            "_registered_posix_groups",
            {
                7001: process_tree._PosixGroupRegistration(
                    leader_identity="browser-start", members={}
                )
            },
        )
        monkeypatch.setattr(
            process_tree,
            "_posix_process_rows",
            lambda: {7001: (1, 7001, "browser-start", "S")} if browser["alive"] else {},
        )
        monkeypatch.setattr(
            process_tree, "process_group_exists", lambda _group: browser["alive"]
        )
    else:
        current = os.getpid()

        class ProcessHandle:
            def __init__(self, process: int) -> None:
                self.process = process

            def Close(self) -> None:
                pass

        class Api:
            @staticmethod
            def OpenProcess(access: int, inherit: bool, process: int) -> Any:
                return ProcessHandle(process)

            @staticmethod
            def TerminateProcess(handle: ProcessHandle, status: int) -> None:
                sent.append(("TerminateProcess", handle.process, status))
                browser["alive"] = False

        class Con:
            PROCESS_TERMINATE = 1
            PROCESS_QUERY_LIMITED_INFORMATION = 2

        class Job:
            JobObjectBasicProcessIdList = 3

            @staticmethod
            def QueryInformationJobObject(
                handle: int, information: int
            ) -> tuple[int, ...]:
                return (current, 700) if browser["alive"] else (current,)

            @staticmethod
            def IsProcessInJob(handle: ProcessHandle, job: Any) -> bool:
                return True

        monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
        monkeypatch.setattr(process_tree, "_adopted_windows_job", 123)
        monkeypatch.setattr(process_tree, "_adopted_windows_gate", None)
        monkeypatch.setattr(
            process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
        )
        monkeypatch.setattr(process_tree.time, "sleep", lambda _seconds: None)

    set_process_role(ServerRole.OWNER)
    a_held_profile_means_this_owner_must_go(SimpleNamespace(browser_open=True))
    if not hard_exit_required():
        pytest.fail("the unconfirmed close did not ask this owner to exit hard")

    released: list[str] = []
    lock = cast(Any, SimpleNamespace(release=lambda: released.append("daemon-lock")))
    serving = asyncio.ensure_future(asyncio.sleep(0))
    await serving

    with pytest.raises(_Exited):
        await daemon_owner._stop_within(serving, 1.0, lock=lock)

    # Releasing the lock is required of the fix too, but it is not the witness:
    # an AssertionError here would pass for the signal regression.
    if released != ["daemon-lock"]:
        pytest.fail(f"the daemon lock was not released: {released!r}")
    assert sent == []


def test_an_unanswered_job_membership_never_terminates_or_proves_the_drain(
    monkeypatch: pytest.MonkeyPatch,
):
    """W-JOB-QUERY (P1), source-model witness, not a native measurement.

    Guards "When the owner cannot tell whether a process belongs to another Job
    it holds on Windows, it neither terminates that process in the routine
    drain nor declares the drain complete". Today ``_in_another_owned_job``
    reads the failed ``IsProcessInJob`` as "not in another Job", so the member
    is terminated and, once it has gone, the drain reports success. The Win32
    APIs are doubles, so this runs on every platform.
    """
    current = os.getpid()
    terminated: list[int] = []
    opened: list[int] = []
    in_the_adopted_job: list[int] = []
    unanswered: list[int] = []
    unknown_jobs: list[Any] = []
    clock = SimpleNamespace(now=0.0)

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            pass

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            opened.append(process)
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    class Job:
        JobObjectBasicProcessIdList = 3

        @staticmethod
        def QueryInformationJobObject(handle: int, information: int) -> tuple[int, ...]:
            # The member leaves the Job only if something terminates it.
            return (current,) if terminated else (current, 700)

        @staticmethod
        def IsProcessInJob(handle: ProcessHandle, job: Any) -> bool:
            if job == "installer-job":
                unanswered.append(handle.process)
                raise OSError("IsProcessInJob did not answer")
            if job == 123:
                in_the_adopted_job.append(handle.process)
                return True
            unknown_jobs.append(job)
            return False

    def sleep(seconds: float) -> None:
        clock.now += seconds

    monkeypatch.setattr(process_tree, "_IS_WINDOWS", True)
    monkeypatch.setattr(process_tree, "_adopted_windows_job", 123)
    monkeypatch.setattr(process_tree, "_adopted_windows_gate", None)
    monkeypatch.setattr(
        process_tree,
        "_live_windows_jobs",
        [SimpleNamespace(job_handle="installer-job")],
    )
    monkeypatch.setattr(
        process_tree, "_windows_modules", lambda: (Api(), Con(), Job(), object())
    )
    monkeypatch.setattr(
        process_tree,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )

    proved = process_tree.drain_browser_process_marker(
        "browser",
        timeout=1.0,
        containment=cast(Any, SimpleNamespace(closed=True, drained=True)),
    )

    # The drain also answers conservatively when an earlier Win32 call fails,
    # so the witness counts only once the modelled question was actually put.
    if unknown_jobs:
        pytest.fail(f"membership was asked of a Job not modelled: {unknown_jobs!r}")
    if 700 not in opened:
        pytest.fail(f"candidate 700 was never opened: {opened!r}")
    if 700 not in in_the_adopted_job:
        pytest.fail("candidate 700 was never confirmed in the adopted Job")
    if 700 not in unanswered:
        pytest.fail("the installer Job was never asked about candidate 700")

    assert terminated == []
    assert proved is False


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="W-CHROME-PATH: a custom browser executable is still a daemon "
    "candidate; fixed in P3",
)
def test_a_custom_browser_keeps_the_direct_server(monkeypatch: pytest.MonkeyPatch):
    """W-CHROME-PATH (P3), source-model witness, not a native measurement.

    Guards the scope decision "Custom browsers": only the bundled browser runs
    in default daemon mode, and ``CHROME_PATH`` keeps today's Direct behaviour.
    Every other gate is open here, so the custom executable is the only reason
    left to refuse.
    """
    monkeypatch.setattr(
        "linkedin_mcp_server.daemon.get_runtime_id", lambda: "linux-amd64-host"
    )
    config = AppConfig()
    config.server.daemon_enabled = True
    config.browser.chrome_path = "/opt/custom/chrome"
    if config.server.transport != "stdio":
        pytest.fail("the default transport is no longer stdio")

    assert daemon_would_be_used(config) is False


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="W-UNMARKED: a failed heartbeat preflight forwards the call "
    "unmarked; fixed in P2",
)
@pytest.mark.parametrize("preflight", ["raises", "404"])
async def test_a_failed_heartbeat_preflight_dispatches_no_tool_call(
    tmp_path: Path, preflight: str
):
    """W-UNMARKED (P2), source-model witness, not a native measurement.

    Guards "The frontend never forwards an unmarked tool call": every failed
    status or invalid preflight response dispatches no tool request. Today a
    preflight that cannot reach the owner, or an owner answering 404, lets the
    call through without a marker, so the owner cannot cancel it.
    """
    from test_daemon_proxy import _attachment, _backend

    middleware = FrontendCallHeartbeatMiddleware(
        _backend(_attachment(tmp_path), tmp_path)
    )

    beats: list[str] = []

    async def beat(_attachment: Any, call_id: str) -> int:
        beats.append(call_id)
        if preflight == "raises":
            raise httpx.ConnectError("the owner refused the connection")
        return 404

    middleware._beat = beat  # ty: ignore[invalid-assignment]
    dispatched: list[str] = []

    async def call_next(_context: Any) -> str:
        dispatched.append("tool call")
        return "the result"

    refused = False
    result: object = None
    try:
        result = await middleware.on_call_tool(_context(), call_next)  # ty: ignore
    except (ToolError, OwnerUnreachableError):
        refused = True

    if not beats:
        pytest.fail("the heartbeat preflight was never reached")
    assert dispatched == []
    _require_a_refusal(refused, result)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="W-OWNER-UNMARKED: the owner runs a call that carries no call id; "
    "fixed in P2",
)
async def test_the_owner_refuses_a_call_without_a_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    """W-OWNER-UNMARKED (P2), source-model witness, not a native measurement.

    Guards "the owner refuses unmarked calls". A call without a call id cannot
    be cancelled when its client goes, and today the owner runs it anyway.
    """
    asked_for_headers: list[str] = []

    def no_headers(**_kwargs: object) -> dict[str, str]:
        asked_for_headers.append("headers")
        return {}

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", no_headers)
    dispatched: list[str] = []

    async def call_next(_context: Any) -> str:
        dispatched.append("tool call")
        return "the result"

    refused = False
    result: object = None
    try:
        result = await OwnerCallLivenessMiddleware().on_call_tool(
            _context(),
            call_next,  # ty: ignore
        )
    except ToolError:
        refused = True

    if not asked_for_headers:
        pytest.fail("the owner never looked for the call marker")
    assert dispatched == []
    _require_a_refusal(refused, result)
