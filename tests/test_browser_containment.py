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

import os
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.process_tree import _scan_marked_posix_processes


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
    never installed. ``BrowserConfig()`` rather than ``get_config()``: the
    global parses ``sys.argv`` and aborts under pytest.
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

    closed = False
    try:
        if os.name == "nt":
            containment = manager._containment
            assert containment is not None, "the launch was never contained"
            members = _windows_job_members(containment)
            # More than the Node driver. Job membership is inherited at process
            # creation, so this is the measurement that says assigning the
            # driver is enough to reach the browser it launches next.
            assert len(members) >= 2, (
                f"Chromium did not join the launch Job (members={members})"
            )
        else:
            assert manager._containment is None, "POSIX grew a Windows Job"
            marked = _scan_marked_posix_processes(manager._process_marker).processes
            assert marked, "no process carried this launch's marker"

        closed = await manager.close()
        assert closed is True, "the close could not prove the launch had gone"

        if os.name == "nt":
            assert manager._containment is not None
            assert manager._containment.closed
            assert manager._containment.drained
        else:
            assert not _scan_marked_posix_processes(manager._process_marker).processes
    finally:
        if not closed:
            await manager.close()
