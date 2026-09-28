"""R7's declared fault and its judgement, without a browser.

The native row comes later. Here, on every platform, with recording doubles
standing in for the real POSIX drain, the browser context and Playwright:

* the public drain alias the core close holds reaches the replaced private
  global, in this checkout's ``process_tree`` and the pinned baseline's;
* the selected call: one real call with the given arguments, True handed back
  as False only after a complete claim and a published outcome; real False,
  exceptions, inert lifetimes, mismatches, duplicates, concurrent entrants, a
  paused entrant and every evidence failure leave the real result alone;
* the judgement: only a claim, a real-True outcome and exactly one
  consumption by the same lifetime, then the kept lease, pass;
* the real driver and core close bodies around it: queued and cancelled
  closes drain once, an idle close begun before arming is excluded only by
  the scenario (E1EZ-01), and an owner stands down on the kept lease;
* the overlay, built for real: same product, the declared fault, none under
  the guardian's ``-I -S``, and a hostile working directory refused;
* the row scenario, the guardian group table and the pre-probe checkpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from differential import fault_overlay, r7_fault
from differential.baseline import BASELINE_SHA, REPO_ROOT, git
from differential.fault_overlay import (
    SCENARIO,
    guardian_group_problems,
    make_overlay,
    pre_probe_problems,
    provenance_problems,
    publish_activation,
    run_python,
    scenario_problems,
    selection_problems,
)
from linkedin_mcp_server import process_tree

MARKER = "r7-test-marker"
#: The lifetime the tests' fault is bound to: this pid, a start of 11 ticks.
START = 11


def _identity() -> tuple[int, int]:
    return os.getpid(), START


def _pinned(relative: str) -> str:
    """One file of the frozen baseline, fetched when a shallow clone lacks it."""
    shown = git(REPO_ROOT, "show", f"{BASELINE_SHA}:{relative}")
    if shown is None:
        git(REPO_ROOT, "fetch", "--no-tags", "--depth=1", "origin", BASELINE_SHA)
        shown = git(REPO_ROOT, "show", f"{BASELINE_SHA}:{relative}")
    if shown is None:
        pytest.fail(f"{relative} at {BASELINE_SHA} could not be read")
    return shown


class _Drain:
    """The real private drain's stand-in: records each call, answers *answer*
    (or raises it), and can hold a caller until released."""

    def __init__(self, answer: Any = True) -> None:
        self.answer = answer
        self.calls: list[tuple[str, float]] = []
        self.entered = threading.Event()
        self.release: threading.Event | None = None

    def __call__(self, marker: str, deadline: float) -> Any:
        self.calls.append((marker, deadline))
        self.entered.set()
        if self.release is not None:
            self.release.wait(10)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _arm(module: types.ModuleType, directory: Path, drain: _Drain, **fault: Any):
    """Stand *drain* in for the private global, then install the fault."""
    module.__dict__["_drain_marked_posix_groups"] = drain
    installed = r7_fault.Fault(
        str(directory), identity=fault.pop("identity", _identity), **fault
    )
    installed.install(module)
    return installed


def _activate(directory: Path, *, role: str = "direct", marker: str = MARKER, **kw):
    return publish_activation(
        directory,
        row="H-R7",
        experiment=kw.pop("experiment", "K3"),
        repetition=1,
        run="test",
        pid=kw.pop("pid", os.getpid()),
        start_ticks=kw.pop("start_ticks", START),
        role=role,
        marker=marker,
        source={"test": True},
    )


@pytest.fixture
def tree(monkeypatch):
    """This checkout's ``process_tree``, with every global the tests change
    restored afterwards and one launch marker registered."""
    monkeypatch.setattr(
        process_tree,
        "_drain_marked_posix_groups",
        process_tree._drain_marked_posix_groups,
    )
    monkeypatch.setattr(process_tree, "_registered_browser_markers", {MARKER})
    monkeypatch.setattr(process_tree, "_IS_WINDOWS", False)
    return process_tree


@pytest.fixture
def row(tmp_path):
    directory = tmp_path / "fault"
    directory.mkdir()
    return directory


# --- The alias the core close holds reaches the replaced global ------------------


def _baseline_tree() -> types.ModuleType:
    module = types.ModuleType("linkedin_mcp_server.process_tree")
    source = _pinned("linkedin_mcp_server/process_tree.py")
    exec(compile(source, "process_tree.py", "exec"), module.__dict__)
    module.__dict__["_registered_browser_markers"] = {MARKER}
    module.__dict__["_IS_WINDOWS"] = False
    return module


@pytest.mark.parametrize("revision", ["candidate", "baseline"])
def test_the_saved_public_alias_reaches_the_replaced_private_global(
    tree, row, revision
):
    module = tree if revision == "candidate" else _baseline_tree()
    # Saved first, by value, exactly as core.browser imports it.
    alias = module.drain_browser_process_marker
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    module.__dict__["time"] = clock
    drain = _Drain(True)
    _arm(module, row, drain)
    _activate(row)
    try:
        assert alias(MARKER, timeout=2.5) is False
    finally:
        module.__dict__["time"] = time
    # The real function, once, with the marker and the deadline it computed.
    assert drain.calls == [(MARKER, 102.5)]
    assert alias.__globals__ is module.__dict__


def test_installing_twice_keeps_the_first_original(tree, row):
    drain = _Drain(True)
    fault = _arm(tree, row, drain)
    fault.install(tree)
    assert fault.original is drain


# --- The selected call ------------------------------------------------------------------


def test_a_selected_true_is_handed_back_as_false_after_publication(tree, row):
    drain = _Drain(True)
    _arm(tree, row, drain)
    activation = _activate(row)
    sent = time.monotonic_ns()
    assert tree.drain_browser_process_marker(MARKER) is False
    claim = json.loads((row / r7_fault.CLAIM).read_text())
    outcome = json.loads((row / r7_fault.OUTCOME).read_text())
    assert (claim["nonce"], claim["pid"], claim["start_ticks"]) == (
        activation["nonce"],
        os.getpid(),
        START,
    )
    assert claim["entered_ns"] > sent and outcome["real"] is True
    assert outcome["returned_ns"] >= claim["entered_ns"]
    assert len(drain.calls) == 1
    # The files alone: nothing consumed the False yet.
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert problems == [
        "the original lifetime consumed a False 0 time(s) after the fault was "
        "armed, not once"
    ]


@pytest.mark.parametrize("answer", [False, RuntimeError("the real drain broke")])
def test_a_real_false_or_exception_passes_unchanged_and_calibrates_nothing(
    tree, row, answer
):
    drain = _Drain(answer)
    _arm(tree, row, drain)
    activation = _activate(row)
    sent = time.monotonic_ns()
    if isinstance(answer, BaseException):
        with pytest.raises(RuntimeError) as raised:
            tree.drain_browser_process_marker(MARKER)
        assert raised.value is answer
    else:
        assert tree.drain_browser_process_marker(MARKER) is False
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert any("not True: no calibration" in p for p in problems), problems


@pytest.mark.parametrize(
    ("change", "invalid"),
    [
        pytest.param({}, False, id="no-activation"),
        pytest.param({"pid": 1}, False, id="another-pid"),
        pytest.param({"start_ticks": START + 1}, False, id="another-lifetime"),
        pytest.param({"marker": "another-launch"}, True, id="another-marker"),
        pytest.param({"role": "owner"}, True, id="another-role"),
    ],
)
def test_a_call_that_is_not_the_selected_one_changes_nothing(
    tree, row, change, invalid
):
    drain = _Drain(True)
    _arm(tree, row, drain)
    if change:
        _activate(row, **change)
    assert tree.drain_browser_process_marker(MARKER) is True
    assert not (row / r7_fault.CLAIM).exists()
    assert bool(list(row.glob(f"{r7_fault.INVALID_PREFIX}*"))) is invalid
    assert len(drain.calls) == 1


def test_an_entry_from_anywhere_but_the_public_drain_is_invalid(tree, row):
    drain = _Drain(True)
    _arm(tree, row, drain)
    _activate(row)
    assert tree._drain_marked_posix_groups(MARKER, 5.0) is True
    assert drain.calls == [(MARKER, 5.0)]
    (invalid,) = row.glob(f"{r7_fault.INVALID_PREFIX}*")
    assert "not the public drain" in json.loads(invalid.read_text())["reason"]


def test_a_second_eligible_entry_is_a_duplicate_and_changes_nothing(tree, row):
    drain = _Drain(True)
    _arm(tree, row, drain)
    activation = _activate(row)
    sent = time.monotonic_ns()
    assert tree.drain_browser_process_marker(MARKER) is False
    assert tree.drain_browser_process_marker(MARKER) is True
    assert len(drain.calls) == 2
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert any("a second eligible entry" in p for p in problems), problems


def test_concurrent_entrants_leave_one_claim(tree, row):
    drain = _Drain(True)
    _arm(tree, row, drain)
    _activate(row)
    start = threading.Barrier(8)
    answers: list[bool] = []

    def enter() -> None:
        start.wait(5)
        answers.append(tree.drain_browser_process_marker(MARKER))

    threads = [threading.Thread(target=enter) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(answers) == [False] + [True] * 7
    assert len(drain.calls) == 8
    assert len(list(row.glob(f"{r7_fault.INVALID_PREFIX}*"))) == 7


def test_an_entrant_paused_before_the_close_was_sent_fails_afterwards(tree, row):
    # It reached the drain, then stalled before reading the activation; the
    # harness armed and sent the close meanwhile. Its entry time is the one it
    # had, and that is before the close.
    paused, resume = threading.Event(), threading.Event()
    first = {"pending": True}

    def clock() -> int:
        now = time.monotonic_ns()
        if first.pop("pending", False):
            paused.set()
            resume.wait(10)
        return now

    drain = _Drain(True)
    _arm(tree, row, drain, monotonic_ns=clock)
    answers: list[bool] = []
    entrant = threading.Thread(
        target=lambda: answers.append(tree.drain_browser_process_marker(MARKER))
    )
    entrant.start()
    assert paused.wait(10)
    activation = _activate(row)
    sent = time.monotonic_ns()
    resume.set()
    entrant.join(10)
    assert answers == [False]  # it took the claim, and still proves nothing
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert any("not after the requested close was sent" in p for p in problems)


@pytest.mark.parametrize("fails", ["claim", "outcome", "activation"])
def test_an_evidence_failure_keeps_the_real_result(tree, row, monkeypatch, fails):
    drain = _Drain(True)
    _arm(tree, row, drain)
    activation = _activate(row)
    if fails == "activation":
        (row / r7_fault.ACTIVATION).write_text("{not json")
    else:
        write = r7_fault._write_new

        def refuse(path, payload):
            if (fails == "claim" and path.endswith(r7_fault.CLAIM)) or (
                fails == "outcome" and ".tmp" in path
            ):
                raise OSError(28, "No space left on device")
            return write(path, payload)

        monkeypatch.setattr(r7_fault, "_write_new", refuse)
    sent = time.monotonic_ns()
    assert tree.drain_browser_process_marker(MARKER) is True
    assert len(drain.calls) == 1
    assert selection_problems(row, activation=activation, sent_ns=sent)


def test_an_incomplete_claim_is_no_claim(tree, row):
    activation = _activate(row)
    (row / r7_fault.CLAIM).write_text("")
    problems = selection_problems(row, activation=activation, sent_ns=0)
    assert any("claim.json is not a complete record" in p for p in problems)


def test_an_activation_is_published_once(row):
    _activate(row)
    with pytest.raises(FileExistsError):
        _activate(row)
    assert not list(row.glob("*.tmp"))
    with pytest.raises(ValueError, match="start is unknown"):
        _activate(row, start_ticks=None)


# --- The judgement: consumption by the same lifetime ----------------------------------


def _selected(tree, row) -> tuple[dict, int]:
    _arm(tree, row, _Drain(True))
    activation = _activate(row)
    sent = time.monotonic_ns()
    assert tree.drain_browser_process_marker(MARKER) is False
    return activation, sent


def _event(row: Path, event: str, **fields: Any) -> None:
    line = {
        "event": event,
        "pid": os.getpid(),
        "start_ticks": START,
        "monotonic_ns": time.monotonic_ns(),
        **fields,
    }
    with (row / r7_fault.EVENTS).open("a") as stream:
        stream.write(json.dumps(line) + "\n")


@pytest.mark.parametrize(
    ("events", "why"),
    [
        pytest.param([r7_fault.CONSUMED, r7_fault.HELD], None, id="consumed-then-held"),
        pytest.param([], "0 time(s)", id="outcome-alone"),
        pytest.param([r7_fault.HELD], "0 time(s)", id="held-without-consumption"),
        pytest.param([r7_fault.CONSUMED], "did not keep the lease", id="no-lease"),
        pytest.param(
            [r7_fault.CONSUMED, r7_fault.CONSUMED, r7_fault.HELD],
            "2 time(s)",
            id="consumed-twice",
        ),
    ],
)
def test_only_one_consumption_by_the_same_lifetime_passes(tree, row, events, why):
    activation, sent = _selected(tree, row)
    for event in events:
        _event(row, event)
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    if why is None:
        assert problems == []
    else:
        assert any(why in p for p in problems), problems


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"pid": 1}, id="another-pid"),
        pytest.param({"start_ticks": START + 1}, id="another-lifetime"),
        pytest.param({"monotonic_ns": 1}, id="before-the-fault-was-armed"),
    ],
)
def test_a_consumption_by_anyone_else_or_before_counts_for_nothing(tree, row, fields):
    activation, sent = _selected(tree, row)
    _event(row, r7_fault.CONSUMED, **fields)
    _event(row, r7_fault.HELD, **fields)
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert any("0 time(s)" in p for p in problems), problems


def test_a_consumption_before_the_drain_returned_is_another(tree, row):
    _arm(tree, row, _Drain(True))
    activation = _activate(row)
    sent = time.monotonic_ns()
    _event(row, r7_fault.CONSUMED)
    assert tree.drain_browser_process_marker(MARKER) is False
    _event(row, r7_fault.HELD)
    problems = selection_problems(row, activation=activation, sent_ns=sent)
    assert "the consumption came before the selected drain returned" in problems


@pytest.mark.parametrize(
    ("logger", "message", "args", "observed"),
    [
        pytest.param(
            "linkedin_mcp_server.core.browser",
            "Browser processes from this launch are still running after close, so "
            "the shutdown stays unconfirmed.",
            (),
            True,
            id="the-template",
        ),
        # The same text formatted in by an argument is not the template.
        pytest.param(
            "linkedin_mcp_server.core.browser",
            "%s",
            (
                "Browser processes from this launch are still running after close, "
                "so the shutdown stays unconfirmed.",
            ),
            False,
            id="as-an-argument",
        ),
        pytest.param(
            "linkedin_mcp_server.drivers.browser",
            "Browser processes from this launch are still running after close, so "
            "the shutdown stays unconfirmed.",
            (),
            False,
            id="another-logger",
        ),
        pytest.param(
            "linkedin_mcp_server.core.browser", {"not": "hashable"}, (), False, id="odd"
        ),
    ],
)
def test_only_the_exact_template_on_its_logger_is_observed(
    row, logger, message, args, observed
):
    loggers: dict[str, logging.Logger] = {}

    def get_logger(name: str) -> logging.Logger:
        if name not in loggers:
            loggers[name] = logging.Logger(name, logging.DEBUG)
            loggers[name].addHandler(logging.NullHandler())
        return loggers[name]

    fault = r7_fault.Fault(str(row), identity=_identity)
    fault.observe(SimpleNamespace(getLogger=get_logger))
    get_logger(logger).error(message, *args)
    assert bool(fault_overlay.events(row)) is observed


# --- The real driver and core close bodies around it -------------------------------


@pytest.fixture
def close(tree, row, tmp_path, monkeypatch):
    """A real ``BrowserManager`` with doubles for its handles, as the driver's
    singleton, on a real lease of a temporary auth root; the fault armed and
    observing the two logger events on the real loggers."""
    from linkedin_mcp_server.core.browser import BrowserManager
    from linkedin_mcp_server.drivers import browser as driver
    from linkedin_mcp_server.profile_lease import get_profile_lease

    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    lease = get_profile_lease(profile)
    assert lease.try_acquire()
    lease.mark_browser_open()
    manager = BrowserManager(user_data_dir=profile)
    manager._context = MagicMock(close=AsyncMock())
    manager._playwright = MagicMock(stop=AsyncMock())
    manager._process_marker = MARKER
    drain = _Drain(True)
    fault = _arm(tree, row, drain)
    observer = fault.observe(logging)
    # The module's own lock, fresh: an earlier test's loop may have bound it.
    monkeypatch.setattr(driver, "_browser_lifecycle_lock", asyncio.Lock())
    monkeypatch.setattr(driver, "_browser", manager)
    monkeypatch.setattr(driver, "_browser_lease", lease)
    monkeypatch.setattr(driver, "_browser_cookie_export_path", None)
    monkeypatch.setattr(driver, "release_browser_guardian", lambda: None)
    try:
        yield SimpleNamespace(
            driver=driver,
            manager=manager,
            lease=lease,
            drain=drain,
            row=row,
            profile=profile,
        )
    finally:
        for name in (
            "linkedin_mcp_server.core.browser",
            "linkedin_mcp_server.drivers.browser",
        ):
            logging.getLogger(name).removeFilter(observer)
        if lease.held:
            lease.release()


async def test_queued_closes_drain_once_and_the_lease_stays_held(close, caplog):
    activation = _activate(close.row)
    sent = time.monotonic_ns()
    with caplog.at_level(logging.INFO):
        await asyncio.gather(*(close.driver.close_browser() for _ in range(3)))
        await close.driver.close_browser()
    assert len(close.drain.calls) == 1
    assert close.lease.held
    # Observed, and still logged: the filter changed nothing about either line.
    logged = [(r.name, r.getMessage()) for r in caplog.records]
    assert (
        "linkedin_mcp_server.core.browser",
        "Browser processes from this launch are still running after close, so the "
        "shutdown stays unconfirmed.",
    ) in logged
    assert (
        "linkedin_mcp_server.drivers.browser",
        "Browser shutdown could not be confirmed; keeping the profile lease until "
        "this process exits.",
    ) in logged
    assert selection_problems(close.row, activation=activation, sent_ns=sent) == []


async def test_a_cancelled_close_settles_before_the_cancel_arrives(close):
    activation = _activate(close.row)
    close.drain.release = threading.Event()
    sent = time.monotonic_ns()
    task = asyncio.ensure_future(close.driver.close_browser())
    while not close.drain.entered.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    close.drain.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(close.drain.calls) == 1 and close.lease.held
    assert selection_problems(close.row, activation=activation, sent_ns=sent) == []


async def test_an_idle_close_begun_before_arming_is_excluded_only_by_the_scenario(
    close, monkeypatch, tmp_path
):
    # E1EZ-01, on the real bodies: the idle close clears the singleton and
    # suspends in its export before the harness arms and sends. The requested
    # close waits on the lock and drains nothing; the one drain entry comes
    # after the send, and the judgement cannot tell whose close it was.
    from linkedin_mcp_server.config import get_config

    config = get_config()
    monkeypatch.setattr(config.browser, "browser_idle_timeout_seconds", 20.0)
    monkeypatch.setattr(close.driver, "_last_activity", time.monotonic() - 60)
    monkeypatch.setattr(close.driver, "_browser_cookie_export_path", tmp_path / "c")
    exporting, exported = asyncio.Event(), asyncio.Event()

    async def export(path):
        exporting.set()
        await exported.wait()
        return True

    close.manager.export_cookies = export
    idle = asyncio.ensure_future(close.driver.release_profile_if_idle_or_requested())
    await exporting.wait()
    assert close.driver._browser is None
    activation = _activate(close.row)
    sent = time.monotonic_ns()
    requested = asyncio.ensure_future(close.driver.close_browser())
    await asyncio.sleep(0.05)
    exported.set()
    assert await idle is True
    await requested
    assert len(close.drain.calls) == 1
    assert selection_problems(close.row, activation=activation, sent_ns=sent) == []
    # So the scenario has to make that close impossible, from actor startup.
    assert scenario_problems({"BROWSER_IDLE_TIMEOUT": "20.0"})


async def test_with_the_scenario_no_idle_close_ever_starts(close, monkeypatch):
    from linkedin_mcp_server.config import get_config

    config = get_config()
    monkeypatch.setattr(
        config.browser,
        "browser_idle_timeout_seconds",
        float(SCENARIO["BROWSER_IDLE_TIMEOUT"]),
    )
    monkeypatch.setattr(close.driver, "_last_activity", time.monotonic() - 10_000)
    assert await close.driver.release_profile_if_idle_or_requested() is False
    assert close.driver._browser is close.manager
    assert close.drain.calls == []
    assert scenario_problems(SCENARIO) == []


async def test_an_entry_paused_before_the_send_fails_through_the_real_close(
    close, monkeypatch
):
    paused, resume = threading.Event(), threading.Event()
    first = {"pending": True}

    def clock() -> int:
        now = time.monotonic_ns()
        if first.pop("pending", False):
            paused.set()
            resume.wait(10)
        return now

    fault = r7_fault.Fault(str(close.row), identity=_identity, monotonic_ns=clock)
    fault.original, fault.public_code = (
        close.drain,
        process_tree.drain_browser_process_marker.__code__,
    )
    fault.module_globals = process_tree.__dict__
    monkeypatch.setattr(
        process_tree,
        "_drain_marked_posix_groups",
        lambda marker, deadline: fault.drain(marker, deadline),
    )
    task = asyncio.ensure_future(close.driver.close_browser())
    while not paused.is_set():
        await asyncio.sleep(0.01)
    activation = _activate(close.row)
    sent = time.monotonic_ns()
    resume.set()
    await task
    problems = selection_problems(close.row, activation=activation, sent_ns=sent)
    assert any("not after the requested close was sent" in p for p in problems)


async def test_a_second_close_of_the_same_launch_is_a_duplicate(close):
    activation = _activate(close.row)
    sent = time.monotonic_ns()
    await close.driver.close_browser()
    # Another close of that launch, outside the driver's singleton.
    from linkedin_mcp_server.core.browser import BrowserManager

    again = BrowserManager(user_data_dir=close.profile)
    again._context = MagicMock(close=AsyncMock())
    again._playwright = MagicMock(stop=AsyncMock())
    again._process_marker = MARKER
    assert await again.close() is True  # the real True, unchanged
    problems = selection_problems(close.row, activation=activation, sent_ns=sent)
    assert any("a second eligible entry" in p for p in problems), problems


async def test_an_owner_stands_down_on_the_kept_lease(close):
    from linkedin_mcp_server import server_role

    server_role.set_process_role(server_role.ServerRole.OWNER)
    activation = _activate(close.row, role="owner")
    sent = time.monotonic_ns()
    await close.driver.close_browser()
    assert close.lease.held
    assert server_role.stand_down_reason() == (
        "the browser did not shut down cleanly, so the profile is held"
    )
    assert selection_problems(close.row, activation=activation, sent_ns=sent) == []


# --- The overlay, built -------------------------------------------------------------


@pytest.fixture(scope="module")
def overlay(tmp_path_factory):
    return make_overlay(sys.executable, tmp_path_factory.mktemp("overlay") / "venv")


def test_the_overlay_runs_the_source_code_and_the_declared_fault(overlay):
    installed = Path(overlay.purelib)
    assert (installed / "r7_fault.py").read_bytes() == fault_overlay.FAULT_TEXT
    assert {p.name for p in installed.iterdir()} - {"__pycache__"} == {
        "r7_fault.py",
        fault_overlay.PTH_FILE,
    }
    for mode in fault_overlay.MODES:
        report = overlay.reports[f"overlay {mode}"]
        assert report["patched"] is True and report["alias_globals"] is True
        assert overlay.reports[f"source {mode}"]["patched"] is False
    assert overlay.reports["overlay guardian"]["fault"] is None


def test_without_a_row_directory_the_overlay_changes_nothing(overlay, tmp_path):
    report = run_python(
        overlay.python,
        ["-I"],
        fault_overlay.PROBE,
        cwd=tmp_path,
        environment=fault_overlay.actor_environment(None),
    )
    assert report["fault"] == str(overlay.fault_file) and report["patched"] is False


def test_a_hostile_working_directory_is_refused(overlay, tmp_path):
    shadow = tmp_path / "linkedin_mcp_server"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    (shadow / "process_tree.py").write_text(
        "def _drain_marked_posix_groups(m, d):\n    return True\n"
        "def drain_browser_process_marker(m):\n    return True\n"
    )
    environment = fault_overlay.actor_environment(tmp_path)
    source = overlay.reports["source safe-path"]
    # With the working directory on the path, the shadow is what imports.
    exposed = run_python(
        overlay.python, [], fault_overlay.PROBE, cwd=tmp_path, environment=environment
    )
    assert provenance_problems(source, exposed, fault_file=overlay.fault_file)
    # The actors' mode keeps it off.
    safe = run_python(
        overlay.python,
        ["-P"],
        fault_overlay.PROBE,
        cwd=tmp_path,
        environment=environment,
    )
    assert provenance_problems(source, safe, fault_file=overlay.fault_file) == []


def test_the_row_directory_reaches_the_owner_environment(monkeypatch, tmp_path):
    from linkedin_mcp_server import daemon_election

    monkeypatch.setenv(r7_fault.FAULT_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    environment = daemon_election._owner_environment()
    assert environment[r7_fault.FAULT_DIR_ENV] == str(tmp_path)
    assert "PYTHONPATH" not in environment


def test_arm_waits_for_the_product_to_import_its_module(monkeypatch, tmp_path):
    # Armed before the product imports process_tree: the module is left to
    # its own loader and replaced right after it ran, once. The package's
    # attribute and sys.modules get this process's module back afterwards.
    import importlib

    import linkedin_mcp_server

    monkeypatch.setattr(linkedin_mcp_server, "process_tree", process_tree)
    monkeypatch.setitem(sys.modules, r7_fault.MODULE, process_tree)
    monkeypatch.delitem(sys.modules, r7_fault.MODULE)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.setattr(r7_fault, "_ARMED", [])
    loggers: dict[str, logging.Logger] = {}
    monkeypatch.setitem(
        sys.modules,
        "logging",
        SimpleNamespace(getLogger=lambda n: loggers.setdefault(n, logging.Logger(n))),
    )
    environ = {r7_fault.FAULT_DIR_ENV: str(tmp_path)}
    fault = r7_fault.arm(environ)
    # A second pass over the .pth, as site makes in a venv: the same fault.
    assert r7_fault.arm(environ) is fault
    monkeypatch.setitem(sys.modules, "logging", logging)
    assert fault is not None and sorted(loggers)
    assert sum(isinstance(f, r7_fault._AfterImport) for f in sys.meta_path) == 1
    fresh = importlib.import_module(r7_fault.MODULE)
    assert fresh is not process_tree
    assert getattr(fresh._drain_marked_posix_groups, "__r7_fault__", False)
    assert type(fresh.__loader__).__name__ != "_Loader"
    assert fault.public_code is fresh.drain_browser_process_marker.__code__
    monkeypatch.setattr(r7_fault, "_ARMED", [])
    assert r7_fault.arm({}) is None


# --- The scenario, the guardian's group and the pre-probe checkpoint -------------


@pytest.mark.parametrize(
    ("value", "fits"),
    [("0", True), ("0.0", True), ("20.0", False), (None, False), ("off", False)],
)
def test_only_a_zero_idle_timeout_is_the_scenario(value, fits):
    environment = {} if value is None else {"BROWSER_IDLE_TIMEOUT": value}
    assert (scenario_problems(environment) == []) is fits


@pytest.mark.parametrize(
    ("experiment", "group", "owner_pgid", "fits"),
    [
        ("K1", 0, None, True),
        ("K3", 0, None, True),
        ("K3", 4242, None, False),
        ("K2", 4242, 4242, True),
        ("K2", 0, 4242, False),
        ("K2", 4242, None, False),
    ],
)
def test_each_experiments_guardian_group(experiment, group, owner_pgid, fits):
    problems = guardian_group_problems(experiment, group, owner_pgid=owner_pgid)
    assert (problems == []) is fits


@pytest.mark.parametrize(
    ("owner", "guardian", "state", "fits"),
    [
        ("exited", "exited", "free", True),
        # A successor already holding the lock is no settlement, whoever it is.
        ("exited", "exited", "held", False),
        ("exited", "exited", "unknown", False),
        ("exited", "still running", "free", False),
        ("error", "exited", "free", False),
    ],
)
def test_the_probe_waits_for_both_exits_and_a_free_lock(owner, guardian, state, fits):
    problems = pre_probe_problems(
        owner_exit=owner, guardian_exit=guardian, lock={"state": state, "reason": "x"}
    )
    assert (problems == []) is fits
