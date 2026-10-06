"""One real browser launch, contained and provably drained, per platform.

Everything else about containment is measured against process doubles or
against a plain child tree. This file draws a real Chromium through the real
``BrowserManager`` and asks the one question those cannot: does the launch's own
attribution actually cover the browser?

That question has a different answer on each platform, which is why the file
runs on all of them. POSIX attributes by an environment marker the browser
carries and scans for it. Windows has no such marker -- an environment block
belongs to its own process, and reading another one's takes the debugger APIs
-- so a Job assigned to the Node driver before it spawns anything is the whole
of the attribution there, and whether Chromium really joins that Job is a fact
about Windows and Chromium that only Windows can answer.

**Whether a browser is installed is settled before the launch, and nothing
after it may turn into a skip.** The obvious shape, a ``try``/``except`` around
``manager.start()`` that skips on any error, would have swallowed the exact
failure this file exists to block: a Windows Job that cannot be created, cannot
be assigned, or that Chromium refuses to nest under all raise out of ``start()``
and would have been reported as "no browser installed". So readiness is asked
first, through the product's own resolution of the binary, and from there every
error fails.

CI installs a browser and then runs this file in a step of its own, because the
platform legs run their process tests before the browser exists. A missing
browser is a failure there for the same reason it is a skip here: in CI the
install step is what was supposed to provide it.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.process_tree import _MarkerScan, _scan_marked_posix_processes

#: How many inconclusive marker scans one side of the gate may see before it
#: fails. Outside Linux a scan is a ``ps`` that can time out on a loaded runner
#: (measured in CI: an empty inconclusive scan while the browser still answered,
#: then nine marked processes on the next one). The bound keeps a permanently
#: unreadable process table a failure within a few seconds, about five ``ps``
#: timeouts plus the pauses, instead of a hang or a pass.
_MARKER_SCAN_ATTEMPTS = 5
_MARKER_SCAN_PAUSE_SECONDS = 0.5


def _running_in_ci() -> bool:
    """Whether a missing browser is a failure rather than a reason to skip."""
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _unavailable(reason: str) -> None:
    """Fail in CI, skip locally. Never quietly pass."""
    if _running_in_ci():
        pytest.fail(
            f"{reason}. In CI this is a failure rather than a skip: the job "
            f"installs a browser before this step precisely so this gate can "
            f"run, and a gate that skips itself is not a gate."
        )
    pytest.skip(f"{reason}; run `uv run patchright install chromium --no-shell`")


def _manager(profile: Path) -> BrowserManager:
    """Build the browser the product builds.

    The options come from ``build_launch_options`` rather than being written out
    here, because a gate that assembles its own launch measures a browser nobody
    ships -- and in particular it would leave out ``channel="chromium"``, which
    is what stops Playwright resolving the headless shell that ``--no-shell``
    never installed. ``BrowserConfig()`` rather than ``get_config()`` keeps the
    measurement independent of process-global test state.
    """
    launch_options, viewport = build_launch_options(BrowserConfig())
    return BrowserManager(
        user_data_dir=profile,
        headless=True,
        viewport=viewport,
        **launch_options,
    )


async def _the_browser_this_launch_would_use(probe: BrowserManager) -> str | None:
    """Resolve the binary the way the launch under test will resolve it.

    ``_executable_about_to_run`` is the product's own answer, the one handed to
    ``refuse_a_downgrade`` on every start, and it honours an operator
    ``executable_path``, the configured channel and ``PLAYWRIGHT_BROWSERS_PATH``
    alike. Asking it needs a driver, so *probe* is a second manager built from
    the same options: nothing here touches the one the test is about, and a
    driver started here cannot be mistaken for the launch's own.

    Which cache it looks in is settled for both of us at once, and not by this
    function. ``reset_bootstrap_for_testing`` clears
    ``PLAYWRIGHT_BROWSERS_PATH`` before every test, so this probe and the launch
    under test both resolve patchright's default cache -- which is where a bare
    ``patchright install`` puts a browser, locally and in CI alike.
    """
    playwright = await async_playwright().start()
    try:
        probe._playwright = playwright
        return probe._executable_about_to_run()
    finally:
        probe._playwright = None
        await playwright.stop()


class _MarkedLaunch(Protocol):
    _containment: Any
    _process_marker: str

    async def close(self) -> bool: ...


def _conclusive_marker_scan(
    marker: str,
    moment: str,
    *,
    scan: Callable[[str], _MarkerScan] = _scan_marked_posix_processes,
    pause: Callable[[float], None] = time.sleep,
) -> tuple[int, ...]:
    """The marked processes from the first scan that could tell.

    An inconclusive scan is empty for the same reason a drained launch is, so
    it is asked again rather than read. One that stays inconclusive fails: an
    unreadable process table proves neither that the launch is attributed nor
    that it has gone.
    """
    for attempt in range(_MARKER_SCAN_ATTEMPTS):
        if attempt:
            pause(_MARKER_SCAN_PAUSE_SECONDS)
        result = scan(marker)
        if result.conclusive:
            return result.processes
    pytest.fail(
        f"the marker scan {moment} stayed inconclusive for "
        f"{_MARKER_SCAN_ATTEMPTS} attempts, so it proves nothing either way"
    )


async def _prove_marked_launch_drains(
    manager: _MarkedLaunch,
    *,
    scan: Callable[[str], _MarkerScan] = _scan_marked_posix_processes,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """POSIX: the marker covers the launch, and a conclusive scan finds it gone.

    The close's own verdict is not taken alone. A close that claims success
    while the independent scan cannot read the process table has proved
    nothing, so the gate fails rather than trusting it, and for the same reason
    it closes the launch again on the way out instead of leaving it running.
    """
    drained = False
    try:
        assert manager._containment is None, "POSIX grew a Windows Job"
        marked = _conclusive_marker_scan(
            manager._process_marker, "after the launch", scan=scan, pause=pause
        )
        assert marked, "no process carried this launch's marker"

        closed = await manager.close()
        assert closed is True, "the close could not prove the launch had gone"

        survivors = _conclusive_marker_scan(
            manager._process_marker, "after the close", scan=scan, pause=pause
        )
        assert not survivors, f"marked processes survived the close: {survivors}"
        drained = True
    finally:
        if not drained:
            # Cleanup only: whatever it returns, the failure above stands.
            with contextlib.suppress(Exception):
                await manager.close()


def _windows_job_members(job: Any) -> tuple[int, ...]:
    import importlib

    win32job = importlib.import_module("win32job")
    handle = job.job_handle
    assert handle is not None, "the launch Job was released before it was read"
    members = win32job.QueryInformationJobObject(
        handle, win32job.JobObjectBasicProcessIdList
    )
    return tuple(int(entry) for entry in members if entry is not None)


async def test_a_real_browser_launch_is_attributed_and_provably_drained(tmp_path):
    """The launch owns its processes, and the close proves they went."""
    executable = await _the_browser_this_launch_would_use(
        _manager(tmp_path / "probe-profile")
    )
    if executable is None:
        _unavailable("the browser executable could not be resolved")
    elif not Path(executable).exists():
        _unavailable(f"no browser installed at {executable}")

    # Past this line nothing is allowed to skip. A Job that cannot be created,
    # cannot be assigned, or that Chromium will not nest under raises out of
    # start(), and those are the failures this file is here to report.
    manager = _manager(tmp_path / "profile")
    await manager.start()

    if os.name != "nt":
        await _prove_marked_launch_drains(manager)
        return

    closed = False
    try:
        containment = manager._containment
        assert containment is not None, "the launch was never contained"
        members = _windows_job_members(containment)
        # More than the Node driver. Job membership is inherited at process
        # creation, so this is the measurement that says assigning the
        # driver is enough to reach the browser it launches next.
        assert len(members) >= 2, (
            f"Chromium did not join the launch Job (members={members})"
        )

        closed = await manager.close()
        assert closed is True, "the close could not prove the launch had gone"

        assert manager._containment is not None
        assert manager._containment.closed
        assert manager._containment.drained
    finally:
        if not closed:
            await manager.close()


_UNKNOWN = _MarkerScan((), False)
_GONE = _MarkerScan((), True)
_RUNNING = _MarkerScan((101, 102, 103), True)
_INCONCLUSIVE = pytest.fail.Exception


class _FakeLaunch:
    """A launch whose close reports *closes_as*, scanned from a script.

    Scans answer from *before_close* until the first close and from
    *after_close* from then on; ``scans`` records which side each one read.
    The launch actually stops on close number *stops_at*, whatever the close
    reports, so ``alive`` is what a lying close leaves behind.
    """

    _containment = None
    _process_marker = "marker"

    def __init__(
        self,
        before_close: Iterable[_MarkerScan],
        after_close: Iterable[_MarkerScan],
        *,
        closes_as: bool = True,
        stops_at: int = 1,
    ) -> None:
        self._before = iter(before_close)
        self._after = iter(after_close)
        self.closes_as = closes_as
        self.stops_at = stops_at
        self.alive = True
        self.close_calls = 0
        self.scans: list[str] = []
        self.pauses: list[float] = []

    async def close(self) -> bool:
        self.close_calls += 1
        if self.close_calls >= self.stops_at:
            self.alive = False
        return self.closes_as

    def scan(self, marker: str) -> _MarkerScan:
        assert marker == "marker"
        side = "after" if self.close_calls else "before"
        self.scans.append(side)
        return next(self._after if self.close_calls else self._before)

    async def prove(self) -> None:
        await _prove_marked_launch_drains(
            self, scan=self.scan, pause=self.pauses.append
        )


class TestMarkerScanVerdicts:
    @pytest.mark.parametrize(
        ("before_close", "after_close"),
        [
            pytest.param([_UNKNOWN, _UNKNOWN, _RUNNING], [_GONE], id="unknown-launch"),
            pytest.param([_RUNNING], [_UNKNOWN, _GONE], id="unknown-close"),
        ],
    )
    async def test_an_unknown_scan_is_asked_again(
        self, before_close: list[_MarkerScan], after_close: list[_MarkerScan]
    ) -> None:
        launch = _FakeLaunch(before_close, after_close)
        await launch.prove()
        assert launch.close_calls == 1
        assert launch.scans == ["before"] * len(before_close) + ["after"] * len(
            after_close
        )
        assert launch.pauses == [_MARKER_SCAN_PAUSE_SECONDS] * (
            len(before_close) + len(after_close) - 2
        )

    @pytest.mark.parametrize(
        ("before_close", "after_close", "error", "message"),
        [
            pytest.param(
                [_UNKNOWN] * _MARKER_SCAN_ATTEMPTS,
                [],
                _INCONCLUSIVE,
                "after the launch stayed inconclusive",
                id="launch-never-readable",
            ),
            pytest.param(
                [_RUNNING],
                [_UNKNOWN] * _MARKER_SCAN_ATTEMPTS,
                _INCONCLUSIVE,
                "after the close stayed inconclusive",
                id="close-never-readable",
            ),
            pytest.param(
                [_GONE],
                [],
                AssertionError,
                "no process carried this launch's marker",
                id="launch-conclusively-unmarked",
            ),
            pytest.param(
                [_RUNNING],
                [_RUNNING],
                AssertionError,
                "marked processes survived the close",
                id="close-left-survivors",
            ),
        ],
    )
    async def test_a_scan_that_cannot_prove_the_launch_fails(
        self,
        before_close: list[_MarkerScan],
        after_close: list[_MarkerScan],
        error: type[BaseException],
        message: str,
    ) -> None:
        launch = _FakeLaunch(before_close, after_close)
        with pytest.raises(error, match=message):
            await launch.prove()
        # A conclusive answer is taken at once, an unknown one is not asked
        # past its bound, and a launch the gate gave up on is still closed.
        assert launch.scans == ["before"] * len(before_close) + ["after"] * len(
            after_close
        )
        assert not launch.alive

    @pytest.mark.parametrize(
        ("after_close", "error", "message"),
        [
            pytest.param(
                [_UNKNOWN] * _MARKER_SCAN_ATTEMPTS,
                _INCONCLUSIVE,
                "after the close stayed inconclusive",
                id="unreadable-after",
            ),
            pytest.param(
                [_RUNNING],
                AssertionError,
                "marked processes survived the close",
                id="survivors-after",
            ),
        ],
    )
    async def test_a_close_claiming_success_is_not_trusted_for_cleanup(
        self,
        after_close: list[_MarkerScan],
        error: type[BaseException],
        message: str,
    ) -> None:
        # The first close says True and stops nothing. The gate must reject the
        # verdict and still not leave the launch running behind it.
        launch = _FakeLaunch([_RUNNING], after_close, closes_as=True, stops_at=2)
        with pytest.raises(error, match=message):
            await launch.prove()
        assert not launch.alive

    async def test_an_unreadable_process_table_fails_within_a_short_budget(
        self,
    ) -> None:
        # Literal numbers on purpose: this is the budget the gate promises (a
        # few `ps` timeouts plus about two seconds of pauses per side), and it
        # must not grow silently with the constants that implement it.
        launch = _FakeLaunch([_RUNNING], itertools.repeat(_UNKNOWN))
        with pytest.raises(_INCONCLUSIVE):
            await launch.prove()
        assert launch.scans.count("after") <= 5
        assert sum(launch.pauses) <= 2.0

    async def test_a_close_that_cannot_prove_itself_is_closed_again(self) -> None:
        launch = _FakeLaunch([_RUNNING], [], closes_as=False)
        with pytest.raises(AssertionError, match="could not prove"):
            await launch.prove()
        assert launch.close_calls == 2


# --- A browser that stopped under a tool call -------------------------------------
#
# Self-contained on purpose: the helpers above belong to the containment gate and
# change with it, and these tests ask a different question of the same launch.


def _alive(process: Any) -> bool:
    import psutil

    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _the_chromium_root(profile: Path) -> tuple[Any, Any]:
    """This launch's Chromium root and the Node driver that started it.

    Looked for among this test process's own descendants, by the profile it was
    handed, so a browser another test or the developer runs is never picked.
    """
    import psutil

    roots = []
    for process in psutil.Process().children(recursive=True):
        try:
            args = process.cmdline()
        except psutil.Error:
            continue
        if any(arg.startswith("--type=") for arg in args):
            continue
        for arg in args:
            if not arg.startswith("--user-data-dir="):
                continue
            try:
                if os.path.samefile(arg.partition("=")[2], profile):
                    roots.append(process)
            except OSError:
                pass
    assert len(roots) == 1, f"expected one Chromium root on {profile}: {roots}"
    root = roots[0]
    # The process that started Chromium. Node, where the browser runs out of
    # process, and this test process itself where Patchright runs Chromium
    # in-process (Linux arm64). A parent outside this test would be another
    # launch.
    driver = root.parent()
    assert driver is not None and (
        driver.pid == os.getpid() or _is_this_tests_descendant(driver)
    ), driver
    return root, driver


def _is_this_tests_descendant(process: Any) -> bool:
    me = os.getpid()
    seen: set[int] = set()
    while process is not None and process.pid not in seen:
        if process.pid == me:
            return True
        seen.add(process.pid)
        try:
            process = process.parent()
        except process.Error:
            return False
    return False


def _reports_stopped(manager: BrowserManager) -> bool:
    try:
        browser = manager.context.browser
        return manager.page.is_closed() or (
            browser is not None and not browser.is_connected()
        )
    except Exception:
        return False


def _assert_nothing_of_it_runs(manager: BrowserManager) -> None:
    """Every process of the launch has gone, by the platform's own attribution."""
    if os.name == "nt":
        # The handle is already released after a proved close, so the proof is
        # what the Job recorded before letting go, not a query on it.
        job = manager._containment
        assert job is not None and job.closed and job.drained
    else:
        # One `ps` on macOS can time out and come back inconclusive. The same
        # bounded retry the gate uses asks again; a scan that stays unreadable
        # still fails, and a survivor still fails at once.
        processes = _conclusive_marker_scan(manager._process_marker, "after the close")
        assert not processes, f"still running: {processes}"


@pytest.mark.parametrize("fault", ["chromium-killed", "page-closed"])
async def test_a_browser_that_stopped_is_drained_and_started_again(
    tmp_path, isolate_profile_dir, monkeypatch, fault
):
    """The call that finds it dead drains it; only the next call launches.

    Two faults, because they leave different things behind. Killing Chromium
    leaves the Node driver reporting a disconnected browser. Closing the active
    page leaves Chromium itself running, on a second page kept open, so the
    drain has a live root to end rather than one already gone.

    Everything the driver does to launch is real except signing in, which would
    visit LinkedIn: the shipped options, the lease, the guardian and the
    containment, on this test's own claimed profile.
    """
    import asyncio
    import contextlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import psutil
    from fastmcp.exceptions import ToolError

    from linkedin_mcp_server import dependencies
    from linkedin_mcp_server.dependencies import get_ready_extractor
    from linkedin_mcp_server.drivers import browser as drv
    from linkedin_mcp_server.exceptions import BrowserUnavailableError
    from linkedin_mcp_server.profile_lease import get_profile_lease
    from linkedin_mcp_server.sequential_tool_middleware import (
        SequentialToolExecutionMiddleware,
    )

    executable = await _the_browser_this_launch_would_use(
        _manager(tmp_path / "probe-profile")
    )
    if executable is None:
        _unavailable("the browser executable could not be resolved")
    elif not Path(executable).exists():
        _unavailable(f"no browser installed at {executable}")

    profile = isolate_profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(drv, "_browser_lifecycle_lock", asyncio.Lock())
    monkeypatch.setattr(drv, "_browser_create_lock", asyncio.Lock())
    monkeypatch.setattr(dependencies, "ensure_tool_ready_or_raise", AsyncMock())
    launched: list[BrowserManager] = []

    async def launch_without_signing_in() -> BrowserManager:
        manager = _manager(profile)
        launched.append(manager)
        await manager.start()
        manager.is_authenticated = True
        drv._browser = manager
        return manager

    monkeypatch.setattr(drv, "_create_browser_locked", launch_without_signing_in)
    middleware = SequentialToolExecutionMiddleware()
    request: Any = SimpleNamespace(
        message=SimpleNamespace(name="get_feed"), fastmcp_context=None
    )

    async def body(context: Any) -> Any:
        return await get_ready_extractor(None, tool_name="get_feed")

    async def call() -> Any:
        return await middleware.on_call_tool(request, body)

    try:
        await call()
        first = launched[0]
        root, node = _the_chromium_root(profile)
        lease = get_profile_lease()
        if os.name == "nt":
            assert first._containment is not None
            assert root.pid in _windows_job_members(first._containment)

        if fault == "page-closed":
            await first.context.new_page()
            await first.page.close()
        else:
            for process in [*root.children(recursive=True), root]:
                with contextlib.suppress(psutil.NoSuchProcess):
                    process.kill()
            # The driver reports the browser gone before Windows has dropped
            # the process, so both have to be true, inside the same bound.
            for _ in range(500):
                if _reports_stopped(first) and not _alive(root):
                    break
                await asyncio.sleep(0.01)
        # What the fault ended and what it left, observed apart.
        assert _reports_stopped(first), "the driver never reported the fault"
        assert _alive(node), "the fault took the Node driver as well"
        if fault == "page-closed":
            assert _alive(root), "closing the page ended Chromium"
        else:
            assert not _alive(root), "Chromium survived the kill"

        with pytest.raises(ToolError) as raised:
            await call()

        assert isinstance(raised.value.__cause__, BrowserUnavailableError)
        assert len(launched) == 1, "the rejecting call launched a browser"
        assert not _alive(root)
        assert not lease.browser_open
        # The browser's reference and the call's both returned.
        assert not lease.held
        _assert_nothing_of_it_runs(first)

        await call()

        assert len(launched) == 2 and drv._browser is launched[1]
        second_root, _ = _the_chromium_root(profile)
        assert second_root.pid != root.pid or (
            second_root.create_time() != root.create_time()
        )
        assert lease.browser_open
    finally:
        await drv.close_browser()
        for manager in launched:
            await manager.close()


def test_the_driver_may_be_this_process(tmp_path, monkeypatch):
    """Linux arm64 runs Chromium in-process, so its parent is the test itself."""
    import psutil

    profile = tmp_path / "profile"
    profile.mkdir()

    class InProcessRoot:
        def cmdline(self) -> list[str]:
            return ["chromium", f"--user-data-dir={profile}"]

        def parent(self) -> psutil.Process:
            return psutil.Process()

    class Self(psutil.Process):
        def children(self, recursive: bool = False) -> list[InProcessRoot]:
            return [InProcessRoot()]

    monkeypatch.setattr(psutil, "Process", Self)
    root, driver = _the_chromium_root(profile)
    assert isinstance(root, InProcessRoot)
    assert driver.pid == os.getpid()
