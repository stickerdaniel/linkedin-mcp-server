"""H-R7's teardown, one transaction, through the real row entry.

``measure_host_quit_row`` on the preservation gate's modelled row: nothing is
launched, traced or signalled for real. The owners, their guardians, the
trace, the watcher and the canaries are doubles that record what the row did
to them, and any one step can be held until the test releases it, so a
cancellation lands exactly there. What each test reads is what the row did
before it let the cancellation or failure go: which owners it ended, which
guardians it waited for, whether the trace, the watcher and the canaries were
stopped, what it retained, and whether anything was preserved after.
"""

from __future__ import annotations

import asyncio
import functools
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import psutil
import pytest

from differential import harness, lease_probe, unconfirmed_close
from differential.signals import UNAVAILABLE, OracleOutcome
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _SETTLED,
    _Watcher,
    profile,
    row,
)
from differential.test_row_judgement import _healthy
from differential.unconfirmed_close import (
    UNSHIMMED,
    R7Setup,
    UnsettledWorker,
    settlement_problems,
)

REAL_END_OWNER = harness.end_owner
REAL_RETIRE = harness.retire_daemon_state
REAL_PUBLICATION = harness.UnresolvedPublication

ORIGINAL, SUCCESSOR = 42, 84


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Fresh registries, so one test's retained resource never gates the next."""
    monkeypatch.setattr(unconfirmed_close, "_OWNED", [])
    monkeypatch.setattr(unconfirmed_close, "_RETAINED", [])
    monkeypatch.setattr(lease_probe, "_OWNED", [])


class _Owner:
    """The handle an owner was identified by: every kill recorded, and gone
    once killed unless it will not die."""

    def __init__(
        self, pid: int, done: list, *, dies: bool = True, created: float | None = None
    ):
        self.pid, self.done, self.dies, self.alive = pid, done, dies, True
        self.created = float(pid) if created is None else created

    def is_running(self):
        return self.alive

    def status(self):
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_RUNNING

    def kill(self):
        self.done.append(("killed", self.pid))
        self.alive = not self.dies

    def create_time(self):
        if not self.alive:
            raise psutil.NoSuchProcess(self.pid)
        return self.created


class _Scene:
    """Owner 42 closes; the host may replace it with successor 84 before it
    quits or is cancelled. Each owner's guardian is its pid plus one.

    ``at_pid`` is the process table the row's unidentified publications are
    read from, by pid: whatever holds a pid now, which need not be the
    owner that once did; ``denied`` pids cannot be read."""

    def __init__(self, modelled_row, monkeypatch, tmp_path, staged_profile):
        self.row = modelled_row
        self.done: list[Any] = []
        self.owners = {pid: _Owner(pid, self.done) for pid in (ORIGINAL, SUCCESSOR)}
        self.at_pid: dict[int, _Owner] = dict(self.owners)
        self.denied: set[int] = set()
        self.published: list[int | None] = [ORIGINAL]
        self.unreadable = False
        self.guardians = {ORIGINAL + 1: "exited", SUCCESSOR + 1: "exited"}
        self.identifiable = {ORIGINAL, SUCCESSOR}
        self.on_end: dict[int, Any] = {}
        self.traced_out = True
        self.retired: list[Any] = []
        self.held: dict[str, tuple[threading.Event, threading.Event]] = {}
        self.healthy = _healthy(staged_profile, daemon=True)
        self.preservation = AsyncMock(return_value=harness.PostQuit(valid=True))
        scene = self

        state = tmp_path / "owned-daemon-state"
        state.mkdir()
        monkeypatch.setattr(harness.daemon_descriptor, "daemon_dir", lambda _: state)

        def read(_root):
            if self.unreadable:
                raise OSError("planted: the descriptor was unreadable")
            if self.published[0] is None:
                return None
            return SimpleNamespace(
                pid=self.published[0],
                instance_id=str(self.published[0]),
                protocol_version=2,
                log_path="",
            )

        monkeypatch.setattr(harness.daemon_descriptor, "read", read)
        monkeypatch.setattr(
            harness,
            "UnresolvedPublication",
            functools.partial(REAL_PUBLICATION, open_process=self.open_process),
        )

        def identify(descriptor, *args):
            if descriptor.pid not in self.identifiable:
                return None, f"pid {descriptor.pid} is no owner of this row"
            handle = self.owners[descriptor.pid]
            return (
                harness.OwnerIdentity(
                    handle.pid,
                    handle.create_time(),
                    str(handle.pid),
                    modelled_row.owner.auth_root,
                    handle,
                ),
                None,
            )

        def end_owner(identity):
            self.step(f"end {identity.pid}")
            ended = REAL_END_OWNER(identity)
            then = self.on_end.pop(identity.pid, None)
            if then is not None:
                then()
            return ended

        def guardian_exit(observed, pid, seconds, **kwargs):
            self.step(f"guardian {pid}")
            self.done.append(("waited", pid))
            return self.guardians[pid]

        def retire(account, owner):
            found = REAL_RETIRE(account, owner, wait_seconds=0.2)
            self.retired.append((owner.pid if owner else None, found))
            return found

        class Tracer:
            available = False
            unavailable = "modelled"
            scope = None

            def __init__(self, directory, *, required=False):
                self.required = required
                self.out = directory / "absent-strace.txt"

            def start(self, pids):
                raise AssertionError("the oracle is not available")

            def stop(self, **kwargs):
                scene.step("stop the trace")
                scene.done.append("trace stopped")
                return OracleOutcome(status=UNAVAILABLE, required=self.required)

            def settled(self):
                return scene.traced_out

            def end(self):
                scene.done.append("trace ended")
                return scene.traced_out

        class Canaries:
            def start(self):
                return []

            def outside_the_harness(self):
                return []

            def deaths(self):
                return []

            def stop(self):
                scene.done.append("canaries stopped")

        def stop_watcher(watcher):
            self.done.append("watcher stopped")
            return watcher.summary

        monkeypatch.setattr(harness, "identify_owner", identify)
        monkeypatch.setattr(harness, "end_owner", end_owner)
        monkeypatch.setattr(harness, "_OWNER_KILL_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(harness, "guardian_launch", lambda _o, pid: (pid + 1, 0))
        monkeypatch.setattr(harness, "lifetime_exit_state", guardian_exit)
        monkeypatch.setattr(harness, "retire_daemon_state", retire)
        monkeypatch.setattr(harness, "SignalOracle", Tracer)
        monkeypatch.setattr(harness, "Canaries", Canaries)
        monkeypatch.setattr(_Watcher, "stop", stop_watcher)
        monkeypatch.setattr(harness, "observe_preservation", self.preservation)
        monkeypatch.setattr(
            lease_probe,
            "run_probe",
            lambda path, **kwargs: {"state": "free", "device": 0, "inode": 0},
        )

    def open_process(self, pid: int) -> _Owner:
        if pid in self.denied:
            raise psutil.AccessDenied(pid)
        held = self.at_pid.get(pid)
        if held is None or not held.alive:
            raise psutil.NoSuchProcess(pid)
        return held

    def hold(self, label: str) -> None:
        self.held[label] = (threading.Event(), threading.Event())

    def step(self, label: str) -> None:
        """Where a step runs: held here, on its worker thread, if asked."""
        if label in self.held:
            entered, release = self.held[label]
            entered.set()
            release.wait(10)

    def host(self, *, replace: bool, then: BaseException | None):
        async def host(*args, **kwargs):
            await kwargs["after_call"]()
            if replace:
                # The original owner gives way and a successor is elected on
                # the same root, after the row's last look.
                self.owners[ORIGINAL].alive = False
                self.published[0] = SUCCESSOR
            if then is not None:
                raise then
            return self.healthy.host

        return host

    def ended(self) -> dict[int, dict]:
        return {
            record["pid"]: record
            for record in self.row.log.records()
            if record["kind"] == "r7.ended"
        }

    async def run(self):
        return await self.row(
            processes=[],
            summary=_SETTLED,
            unconfirmed_close=R7Setup(None, False, 0, UNSHIMMED),
        )


@pytest.fixture
def scene(row, monkeypatch, tmp_path, profile):  # noqa: F811 - the imported fixtures
    return _Scene(row, monkeypatch, tmp_path, profile)


async def _cancel_while_held(scene: _Scene, task: asyncio.Task, label: str) -> None:
    entered, release = scene.held[label]
    async with asyncio.timeout(10):
        while not entered.is_set():
            if task.done():
                await task
            await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()


def _release_all(scene: _Scene) -> None:
    for _, release in scene.held.values():
        release.set()


def _assert_whole_teardown(scene: _Scene) -> None:
    ended = scene.ended()
    assert ended[SUCCESSOR]["result"] == "stopped"
    assert ended[ORIGINAL]["result"] == "gone"
    assert ended[SUCCESSOR]["guardian_exit"] == "exited"
    assert ended[ORIGINAL]["guardian_exit"] == "exited"
    assert ("waited", SUCCESSOR + 1) in scene.done
    assert ("waited", ORIGINAL + 1) in scene.done
    assert not scene.owners[SUCCESSOR].alive
    for helper in ("trace stopped", "watcher stopped", "canaries stopped"):
        assert helper in scene.done, helper
    assert scene.retired and scene.retired[-1][0] == SUCCESSOR
    assert scene.retired[-1][1].owner_gone
    assert scene.preservation.await_count == 0
    assert settlement_problems() == []


@pytest.mark.parametrize(
    "label",
    [
        pytest.param(f"end {SUCCESSOR}", id="serving-owner-end"),
        pytest.param(f"guardian {SUCCESSOR + 1}", id="serving-guardian-wait"),
        pytest.param(f"end {ORIGINAL}", id="original-owner-end"),
        pytest.param(f"guardian {ORIGINAL + 1}", id="original-guardian-wait"),
        pytest.param("stop the trace", id="trace-stop"),
    ],
)
async def test_a_cancellation_mid_teardown_still_runs_all_of_it(
    scene, monkeypatch, label
):
    monkeypatch.setattr(
        harness, "run_host_session", scene.host(replace=True, then=None)
    )
    scene.hold(label)
    task = asyncio.create_task(scene.run())
    try:
        await _cancel_while_held(scene, task, label)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        _release_all(scene)
    _assert_whole_teardown(scene)


async def test_every_cancellation_is_held_and_one_is_raised(scene, monkeypatch):
    monkeypatch.setattr(
        harness, "run_host_session", scene.host(replace=True, then=None)
    )
    scene.hold(f"end {SUCCESSOR}")
    scene.hold(f"guardian {ORIGINAL + 1}")
    scene.hold("stop the trace")
    task = asyncio.create_task(scene.run())
    try:
        await _cancel_while_held(scene, task, f"end {SUCCESSOR}")
        await _cancel_while_held(scene, task, f"guardian {ORIGINAL + 1}")
        await _cancel_while_held(scene, task, "stop the trace")
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        _release_all(scene)
    _assert_whole_teardown(scene)


@pytest.mark.parametrize(
    "replace", [False, True], ids=["original-still-serving", "successor-elected"]
)
async def test_a_host_cancelled_before_settling_ends_whoever_serves(
    scene, monkeypatch, replace
):
    monkeypatch.setattr(
        harness,
        "run_host_session",
        scene.host(replace=replace, then=asyncio.CancelledError("planted")),
    )
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    serving = SUCCESSOR if replace else ORIGINAL
    assert not scene.owners[serving].alive
    # Only the owner that still ran was signalled, through its own handle.
    assert [op for op in scene.done if op[0] == "killed"] == [("killed", serving)]
    assert scene.retired[-1][0] == serving and scene.retired[-1][1].owner_gone
    for helper in ("trace stopped", "watcher stopped", "canaries stopped"):
        assert helper in scene.done, helper
    assert settlement_problems() == []


def _kills(scene: _Scene) -> list[int]:
    return [op[1] for op in scene.done if op[0] == "killed"]


async def _refused_everywhere(scene: _Scene, named: str) -> None:
    """Nothing measures while *named* is held: not the gate, not a row."""
    assert any(named in p for p in settlement_problems()), settlement_problems()
    with pytest.raises(UnsettledWorker, match=named):
        unconfirmed_close.gate("a later measurement")
    with pytest.raises(UnsettledWorker, match="an earlier row left"):
        await scene.row(processes=[], summary=_SETTLED)


UNIDENTIFIED = f"pid {SUCCESSOR}, which the row could not identify"


async def test_an_owner_the_row_cannot_identify_is_never_signalled_nor_passed(
    scene, monkeypatch
):
    monkeypatch.setattr(
        harness,
        "run_host_session",
        scene.host(replace=True, then=asyncio.CancelledError("planted")),
    )
    scene.identifiable.discard(SUCCESSOR)
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    assert scene.owners[SUCCESSOR].alive
    assert SUCCESSOR not in _kills(scene)
    # Cleanup falls back to the original, and the descriptor naming another
    # owner keeps the row's state as evidence rather than signalling it.
    pid, cleanup = scene.retired[-1]
    assert pid == ORIGINAL and not cleanup.owner_gone and not cleanup.signalled
    assert any(
        f"names pid {SUCCESSOR}" in failure and "not signalled" in failure
        for failure in cleanup.failures
    ), cleanup.failures
    # And nothing measures after it while it runs, asked again and again.
    await _refused_everywhere(scene, UNIDENTIFIED)
    await _refused_everywhere(scene, UNIDENTIFIED)
    assert SUCCESSOR not in _kills(scene)


@pytest.mark.parametrize(
    ("then", "settled"),
    [
        pytest.param("it-exits", True, id="it-exits"),
        pytest.param("its-pid-is-reused", True, id="its-pid-is-then-reused"),
        pytest.param("still-running", False, id="still-running"),
        pytest.param(
            "still-running-descriptor-gone", False, id="descriptor-gone-it-runs"
        ),
        pytest.param(
            "still-running-descriptor-replaced", False, id="descriptor-replaced"
        ),
        pytest.param("unreadable", False, id="its-process-unreadable"),
    ],
)
async def test_only_its_own_lifetime_ending_lets_measurement_resume(
    scene, monkeypatch, then, settled
):
    monkeypatch.setattr(
        harness,
        "run_host_session",
        scene.host(replace=True, then=asyncio.CancelledError("planted")),
    )
    scene.identifiable.discard(SUCCESSOR)
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    successor = scene.owners[SUCCESSOR]
    if then in ("it-exits", "its-pid-is-reused"):
        successor.alive = False
    if then == "its-pid-is-reused":
        # Another process takes the number: not the lifetime that published.
        scene.at_pid[SUCCESSOR] = _Owner(SUCCESSOR, scene.done, created=999.0)
    if then == "still-running-descriptor-gone":
        scene.published[0] = None
    if then == "still-running-descriptor-replaced":
        scene.published[0] = ORIGINAL
    if then == "unreadable":
        scene.denied.add(SUCCESSOR)
    if settled:
        assert settlement_problems() == []
        unconfirmed_close.gate("a later measurement")
    else:
        await _refused_everywhere(scene, UNIDENTIFIED)
    assert SUCCESSOR not in _kills(scene)


async def test_a_pid_unreadable_when_published_waits_for_its_first_read(
    scene, monkeypatch
):
    monkeypatch.setattr(
        harness,
        "run_host_session",
        scene.host(replace=True, then=asyncio.CancelledError("planted")),
    )
    scene.identifiable.discard(SUCCESSOR)
    scene.denied.add(SUCCESSOR)
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    await _refused_everywhere(scene, UNIDENTIFIED)
    # Readable at last and still running: that lifetime is now the one held.
    scene.denied.discard(SUCCESSOR)
    await _refused_everywhere(scene, UNIDENTIFIED)
    # Then it ends, and its number goes to another process.
    scene.owners[SUCCESSOR].alive = False
    scene.at_pid[SUCCESSOR] = _Owner(SUCCESSOR, scene.done, created=999.0)
    assert settlement_problems() == []
    assert SUCCESSOR not in _kills(scene)


async def test_an_unreadable_descriptor_is_never_read_as_settled(scene, monkeypatch):
    host = scene.host(replace=False, then=asyncio.CancelledError("planted"))

    async def unreadable_after_the_call(*args, **kwargs):
        after_call = kwargs["after_call"]

        async def then_unreadable():
            await after_call()
            scene.unreadable = True

        return await host(*args, **{**kwargs, "after_call": then_unreadable})

    monkeypatch.setattr(harness, "run_host_session", unreadable_after_the_call)
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    # The owner the row identified is still ended; nothing else is signalled.
    assert _kills(scene) == [ORIGINAL]
    named = "whose descriptor could not be read"
    await _refused_everywhere(scene, named)
    # Readable again and naming nothing, every owner gone: still nothing ties
    # what was published to a lifetime, so nothing measures.
    scene.unreadable = False
    scene.published[0] = None
    await _refused_everywhere(scene, named)


async def test_an_owner_published_after_settling_is_held_not_signalled(
    scene, monkeypatch
):
    monkeypatch.setattr(
        harness, "run_host_session", scene.host(replace=True, then=None)
    )
    third = _Owner(126, scene.done)
    scene.at_pid[126] = third

    def publish_a_third():
        scene.published[0] = 126

    # Published between settling and cleanup, by nothing the row identified.
    scene.on_end[SUCCESSOR] = publish_a_third
    result, preserved = await scene.run()
    assert preserved == 0 and third.alive and 126 not in _kills(scene)
    assert result.unconfirmed is not None
    named = "pid 126, which the row could not identify"
    assert any(named in p for p in result.unconfirmed.validity)
    await _refused_everywhere(scene, named)
    third.alive = False
    assert settlement_problems() == []


@pytest.mark.parametrize(
    ("seen", "then", "named"),
    [
        pytest.param("unidentified", "gone", UNIDENTIFIED, id="then-descriptor-gone"),
        pytest.param(
            "unidentified", "the-original", UNIDENTIFIED, id="then-names-the-original"
        ),
        pytest.param(
            "unreadable",
            "gone",
            "whose descriptor could not be read",
            id="unreadable-then-readable-and-empty",
        ),
    ],
)
async def test_what_settling_saw_published_stays_held_whatever_cleanup_reads(
    scene, monkeypatch, seen, then, named
):
    # Settling looks the root up once. Before cleanup reads it again, the
    # descriptor changes, and the owner it named still runs: what cleanup
    # reads then says nothing of that owner.
    host = scene.host(replace=True, then=asyncio.CancelledError("planted"))

    async def settling_reads(*args, **kwargs):
        after_call = kwargs["after_call"]

        async def then_unreadable():
            await after_call()
            scene.unreadable = seen == "unreadable"

        return await host(*args, **{**kwargs, "after_call": then_unreadable})

    def change():
        scene.unreadable = False
        scene.published[0] = None if then == "gone" else ORIGINAL

    if seen == "unidentified":
        scene.identifiable.discard(SUCCESSOR)
    # Settling ends the original after its look at the root.
    scene.on_end[ORIGINAL] = change
    monkeypatch.setattr(harness, "run_host_session", settling_reads)
    with pytest.raises(asyncio.CancelledError):
        await scene.run()
    assert scene.owners[SUCCESSOR].alive and SUCCESSOR not in _kills(scene)
    # Cleanup found nothing left to settle, and still nothing measures.
    assert scene.retired[-1][1].owner_gone
    await _refused_everywhere(scene, named)


@pytest.mark.parametrize(
    ("pid", "first", "then", "settled"),
    [
        # Nothing names a pid, whatever the table shows later.
        pytest.param(None, None, 5.0, (False, False), id="unreadable-descriptor"),
        pytest.param(7, None, 5.0, (True, True), id="no-process-held-it"),
        # Held from its first read: the lifetime then is the one waited for.
        pytest.param(7, 5.0, 5.0, (False, False), id="the-same-lifetime"),
        pytest.param(7, 5.0, 6.0, (False, True), id="another-lifetime-at-its-pid"),
        pytest.param(7, 5.0, None, (False, True), id="its-pid-then-free"),
        # Unread at first, the first lifetime read is the one waited for.
        pytest.param(7, "denied", 6.0, (False, False), id="read-only-later"),
        pytest.param(7, "denied", None, (False, True), id="free-when-read"),
    ],
)
def test_an_unresolved_publication_is_settled_only_by_its_lifetime(
    pid, first, then, settled
):
    table: dict[int, Any] = {7: first}

    def open_process(number):
        found = table.get(number)
        if found == "denied":
            raise psutil.AccessDenied(number)
        if found is None:
            raise psutil.NoSuchProcess(number)
        return _Owner(number, [], created=found)

    held = REAL_PUBLICATION(pid, open_process=open_process)
    now = held.check(0.0)
    table[7] = then
    assert (now, held.check(0.0)) == settled


async def test_a_failure_then_a_cancellation_raises_the_cancellation_after_cleanup(
    scene, monkeypatch
):
    monkeypatch.setattr(
        harness,
        "run_host_session",
        scene.host(replace=True, then=ValueError("planted host failure")),
    )
    scene.hold("stop the trace")
    task = asyncio.create_task(scene.run())
    try:
        await _cancel_while_held(scene, task, "stop the trace")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
    finally:
        _release_all(scene)
    assert isinstance(raised.value.__context__, ValueError)
    assert any("planted host failure" in note for note in raised.value.__notes__)
    _assert_whole_teardown(scene)


@pytest.mark.parametrize(
    ("left", "named"),
    [
        pytest.param(
            "owner", f"the serving owner {SUCCESSOR}", id="owner-will-not-die"
        ),
        pytest.param(
            "guardian",
            f"the serving owner's guardian {SUCCESSOR + 1}",
            id="guardian-still-running",
        ),
        pytest.param("trace", "the row's trace", id="trace-not-shown-ended"),
    ],
)
async def test_what_a_row_cannot_settle_refuses_every_later_row_until_it_is(
    scene, monkeypatch, left, named
):
    monkeypatch.setattr(
        harness, "run_host_session", scene.host(replace=True, then=None)
    )
    if left == "owner":
        scene.owners[SUCCESSOR].dies = False
    elif left == "guardian":
        scene.guardians[SUCCESSOR + 1] = "still running"
    else:
        scene.traced_out = False
    result, _ = await scene.run()
    assert scene.preservation.await_count == 0
    assert result.unconfirmed is not None
    assert any(named in p for p in result.unconfirmed.validity), result.unconfirmed
    assert any(named in p for p in settlement_problems()), settlement_problems()
    # Not only H-R7: no row of any kind starts while it is retained.
    with pytest.raises(UnsettledWorker, match="an earlier row left"):
        await scene.row(processes=[], summary=_SETTLED)
    # Once it is gone, the next row may start.
    if left == "owner":
        scene.owners[SUCCESSOR].dies = True
    elif left == "guardian":
        scene.guardians[SUCCESSOR + 1] = "exited"
    else:
        scene.traced_out = True
    assert settlement_problems() == []
