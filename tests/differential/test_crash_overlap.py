"""H-R6-contended's verdict on synthetic records, and its contender on a
temporary lock, on any platform.

The verdict reads only the raw record, so each case here is a record the row
could have written. The contender runs for real against the product's own
lease held in this process: on Windows that is ``LockFileEx`` and process
handles, elsewhere ``flock`` and signal 0.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from typing import Any

import psutil
import pytest

from differential import harness
from differential.crash_contender import ACTOR, FREE, HELD, Contender
from differential.crash_overlap import (
    BROWSER,
    BROWSER_ROOT,
    IDLE_TIMEOUT_SECONDS,
    INCONCLUSIVE,
    K2_NOT_APPLICABLE,
    NO_OVERLAP,
    OTHER,
    OVERLAP,
    ROW,
    crash_reading,
    lane_failures,
    o1_findings,
    problems_for,
)
from linkedin_mcp_server.profile_lease import get_profile_lease

MS = 1_000_000
#: The kill, on the wall clock in nanoseconds.
KILL = 1_800_000_000 * 10**9
#: When the old processes began, long before the kill.
OLD = 1_799_999_000.0
#: The old browser's root.
ROOT = 10


def _at(ms: float) -> float:
    """*ms* after the kill, as a create time in seconds."""
    return (KILL + ms * MS) / 1e9


def _life(
    pid: int,
    kind: str,
    *,
    alive: float | None,
    gone: float | None,
    opened: bool = True,
    gone_before_watched: bool = False,
) -> dict[str, Any]:
    """A watched process last seen alive and first seen gone, in ms from the kill."""
    return {
        "pid": pid,
        "start": OLD,
        "kind": kind,
        "opened": opened,
        "verified": True if opened else None,
        "gone_before_watched": gone_before_watched,
        "error": None,
        "last_alive_wall_ns": None if alive is None else KILL + int(alive * MS),
        "gone_wall_ns": None if gone is None else KILL + int(gone * MS),
        "exit_wall_ns": None,
        "unanswered": 0,
    }


def _run(state: str, at: float) -> dict[str, Any]:
    return {
        "state": state,
        "reason": None,
        "first_wall_ns": KILL + int(at * MS),
        "last_wall_ns": KILL + int(at * MS),
        "first_ns": 0,
        "last_ns": 0,
        "count": 1,
    }


#: The lease held before the kill and granted once, 60 ms after it.
LEASE = [_run(HELD, -500), _run(FREE, 60)]
#: When the row asked for the successor, once the grant was given back.
ISSUED = 70


def _history(watched: list[dict[str, Any]], successor: float | None) -> list[list[Any]]:
    """``harness.browser_processes`` for *watched*: each old browser process
    in the old root's tree, gone when the contender saw it gone."""
    history = [
        [
            entry["pid"],
            entry["start"],
            None if entry["gone_wall_ns"] is None else entry["gone_wall_ns"] / 1e9,
            ROOT,
            OLD,
        ]
        for entry in watched
        if entry["kind"] in (BROWSER_ROOT, BROWSER)
    ]
    if successor is not None:
        history.append([20, _at(successor), None, 20, _at(successor)])
    return history


def _record(
    watched: list[dict[str, Any]],
    *,
    successor: float | None = 3000,
    lease: list[dict[str, Any]] = LEASE,
    issued: float | None = ISSUED,
    ready: float = -500,
    history: list[list[Any]] | None = None,
    platform: str = "win32",
) -> dict[str, Any]:
    """A daemon record whose successor's root began *successor* ms after the
    kill, or never; the old root is pid 10."""
    armed = [
        {"pid": entry["pid"], "start": entry["start"], "ppid": 1, "kind": entry["kind"]}
        for entry in watched
        if entry["kind"] != ACTOR
    ]
    roots: list[list[Any]] = [[ROOT, OLD, _at(45), 3, OLD]]
    if successor is not None:
        roots.append([20, _at(successor), _at(9000), 4, _at(1000)])
    return {
        "row": ROW,
        "mode": "daemon",
        "platform": platform,
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "calls": [
            {"tool": "get_feed", "outcome": "returned", "read_the_post": True},
            {"tool": "get_feed", "outcome": "returned", "read_the_post": True},
        ],
        "browser_roots": roots,
        "browser_processes": (
            _history(watched, successor) if history is None else history
        ),
        "contended": {
            "role": "owner",
            "actor": {"pid": 2, "start": OLD},
            "armed": armed,
            "kill": {"wall_ns": KILL, "monotonic_ns": 1, "exit": "killed"},
            "successor": {
                "issued_wall_ns": None if issued is None else KILL + int(issued * MS)
            },
            "contender": {
                "error": None,
                "ready": {"state": HELD, "wall_ns": KILL + int(ready * MS), "ns": 0},
                "lease": lease,
                "watched": watched,
            },
        },
    }


def _settled_tree() -> list[dict[str, Any]]:
    """The owner, its driver, the browser's root and two members, all gone
    within 120 ms of the kill."""
    return [
        _life(2, ACTOR, alive=-1, gone=5),
        _life(3, OTHER, alive=30, gone=35),
        _life(ROOT, BROWSER_ROOT, alive=38, gone=42),
        _life(11, BROWSER, alive=80, gone=85),
        _life(12, BROWSER, alive=110, gone=118),
    ]


def _lingering_member() -> list[dict[str, Any]]:
    """The settled tree, but a renderer lives on to 3.5 s after the kill."""
    tree = _settled_tree()
    tree[3] = _life(11, BROWSER, alive=3500, gone=3505)
    return tree


def test_a_member_alive_after_the_successor_began_overlaps_though_its_root_was_gone():
    # The root went at 42 ms and the successor's root began at 3 s: a reading
    # of roots alone would call this clean.
    reading = crash_reading(_record(_lingering_member()))
    assert reading.verdict == OVERLAP, reading
    assert reading.overlapping == ((11, OLD, BROWSER),)
    assert reading.ms["old_root_gone"] == 42
    assert reading.ms["last_browser_gone"] == 3505
    assert reading.ms["successor_admitted"] == pytest.approx(3000, abs=0.01)


def test_every_old_browser_process_gone_first_is_no_overlap_with_its_numbers():
    reading = crash_reading(_record(_settled_tree()))
    assert reading.verdict == NO_OVERLAP, reading
    assert reading.ms == {
        "lease_free": 60,
        "actor_gone": 5,
        "old_root_gone": 42,
        "last_browser_gone": 118,
        "last_old_gone": 118,
        "successor_admitted": pytest.approx(3000, abs=0.01),
    }
    # The lease came free at 60 ms with two members still running: #808's
    # premise, measured, and no O1 finding without a successor beside them.
    assert reading.lease_free_while_alive is True


def test_a_free_lease_with_no_successor_is_inconclusive():
    tree = _settled_tree()
    tree[4] = _life(12, BROWSER, alive=8000, gone=None)
    reading = crash_reading(_record(tree, successor=None))
    assert reading.verdict == INCONCLUSIVE
    assert "not an O1 finding" in reading.reasons[0]
    assert reading.ms["lease_free"] == 60


def test_a_member_seen_alive_within_a_clock_tick_of_the_successor_decides_nothing():
    tree = _settled_tree()
    tree[4] = _life(12, BROWSER, alive=3005, gone=3020)
    reading = crash_reading(_record(tree))
    assert reading.verdict == INCONCLUSIVE
    assert "old browser process 12" in reading.reasons[0]


def test_the_successor_asked_for_after_the_grant_is_held_to_that_wait():
    # The row waited 300 ms for the grant before asking: asked for at the
    # kill, the successor could have begun at 2.7 s, beside this member.
    tree = _settled_tree()
    tree[4] = _life(12, BROWSER, alive=2790, gone=2800)
    assert crash_reading(_record(tree)).verdict == NO_OVERLAP
    reading = crash_reading(_record(tree, issued=300))
    assert reading.verdict == INCONCLUSIVE, reading
    assert "old browser process 12" in reading.reasons[0]
    # Asked for before the grant was given back, or never stamped, the
    # successor may have met the contender on the lock.
    for issued in (50, None):
        early = crash_reading(_record(_settled_tree(), issued=issued))
        assert early.verdict == INCONCLUSIVE, early
        assert "met the contender" in early.reasons[0]


def test_a_member_never_watched_keeps_no_overlap_from_being_claimed():
    tree = _settled_tree()
    tree[4] = _life(12, BROWSER, alive=None, gone=None, opened=False)
    assert crash_reading(_record(tree)).verdict == INCONCLUSIVE
    # One already gone when the contender first looked ended before the kill.
    tree[4] = _life(
        12, BROWSER, alive=None, gone=-700, opened=False, gone_before_watched=True
    )
    assert crash_reading(_record(tree)).verdict == NO_OVERLAP


#: The browser's image.
CHROME = "C:\\chrome\\chrome.exe"


def _events(
    *, late: bool, orphans: tuple[tuple[int, str, float, float], ...] = ()
) -> list[dict[str, Any]]:
    """The watcher's records of the old browser and the successor's. Members
    carry no ``--user-data-dir`` here, as a Chromium child need not; *late*
    adds a renderer the old root started after the row read its tree, alive
    past the successor's root. Each of *orphans*, ``(pid, image, began,
    gone)`` in ms from the kill, is a helper whose parent was never sampled."""

    def start(
        actor: str, pid: int, ppid: int, at: float, exe: str = CHROME
    ) -> dict[str, Any]:
        return {
            "kind": "process.start",
            "actor": actor,
            "in_row": True,
            "pid": pid,
            "ppid": ppid,
            "exe": exe,
            "start_identity": at,
            "t": at,
        }

    def exit_(pid: int, at: float, gone: float) -> dict[str, Any]:
        return {"kind": "process.exit", "pid": pid, "start_identity": at, "t": gone}

    events = [
        start("browser", ROOT, 3, OLD),
        start("other", 11, ROOT, OLD),
        start("other", 12, ROOT, OLD),
        start("browser", 20, 4, _at(3000)),
        exit_(ROOT, OLD, _at(45)),
        exit_(11, OLD, _at(90)),
        exit_(12, OLD, _at(120)),
    ]
    if late:
        events += [start("other", 13, ROOT, _at(-200)), exit_(13, _at(-200), _at(3500))]
    for pid, exe, at, gone in orphans:
        # Its parent, an old browser process, began and ended between samples.
        events += [
            start("other", pid, 13, _at(at), exe),
            exit_(pid, _at(at), _at(gone)),
        ]
    return events


def test_an_old_member_the_row_never_watched_keeps_no_overlap_from_being_claimed():
    # Started after the row read the tree and before the kill: in no snapshot
    # and no watch list, only in the watcher's completed history.
    clean = _settled_tree()
    history = harness.browser_processes(_events(late=False))
    assert crash_reading(_record(clean, history=history)).verdict == NO_OVERLAP
    history = harness.browser_processes(_events(late=True))
    reading = crash_reading(_record(clean, history=history))
    assert reading.verdict == INCONCLUSIVE, reading
    assert "old browser process 13" in reading.reasons[0]
    # Without the history nothing says the watch list was whole.
    assert crash_reading(_record(clean, history=[])).verdict == INCONCLUSIVE


def test_a_helper_of_the_browsers_image_whose_parent_was_never_sampled_counts():
    # An old browser process started it and ended between two samples, so no
    # tree reaches it; it ran the browser's image and outlived the successor.
    def read(*orphan: tuple[int, str, float, float]) -> str:
        history = harness.browser_processes(_events(late=False, orphans=orphan))
        return crash_reading(_record(_settled_tree(), history=history)).verdict

    assert read((14, CHROME, -1, 3500)) == INCONCLUSIVE
    # Seen gone before the successor could have begun, it decides nothing.
    assert read((14, CHROME, -1, 40)) == NO_OVERLAP
    # Neither does another image, nor a process begun after the kill.
    assert read((14, "C:\\node\\node.exe", -1, 3500)) == NO_OVERLAP
    assert read((14, CHROME, 20, 3500)) == NO_OVERLAP


def test_a_contender_not_armed_before_the_kill_decides_nothing():
    assert crash_reading(_record(_lingering_member(), ready=10)).verdict == INCONCLUSIVE
    free = [_run(FREE, -500)]
    assert crash_reading(_record(_settled_tree(), lease=free)).verdict == INCONCLUSIVE


def test_a_lingering_driver_is_reported_and_is_no_overlap():
    tree = _settled_tree()
    tree[1] = _life(3, OTHER, alive=3400, gone=3410)
    reading = crash_reading(_record(tree))
    assert reading.verdict == NO_OVERLAP
    assert reading.others_overlapping == ((3, OLD, OTHER),)
    assert reading.ms["last_old_gone"] == 3410


def test_only_an_overlap_the_daemon_alone_shows_is_an_o1_finding():
    clean = crash_reading(_record(_settled_tree()))
    overlap = crash_reading(_record(_lingering_member()))
    undecided = crash_reading(_record(_settled_tree(), successor=None))
    [finding] = o1_findings(clean, overlap)
    assert finding.startswith("O1:") and "11" in finding
    # Shared with Direct: hardened for both, not a gate of the default.
    assert o1_findings(overlap, overlap) == []
    assert o1_findings(clean, clean) == []
    assert o1_findings(overlap, clean) == []
    assert o1_findings(clean, undecided) != []
    assert o1_findings(None, clean) != []


def test_a_second_root_is_the_comparisons_and_an_unhealthy_watcher_still_fails():
    single = harness.RowVector(
        mode="direct",
        o1_single_browser=True,
        browser_seen=True,
        watcher_healthy=True,
        o4_session=harness.RETAINED,
        origin_saw_feed=True,
        feed_carried_session=True,
        tool_succeeded=True,
        owner_published=False,
        fell_back=False,
        host_exit_clean=True,
        cleanup_clean=True,
    )
    assert harness.row_expectations(single) == []
    # Two roots in one sample: an overlap in both lanes is a measurement.
    second = replace(single, o1_single_browser=False)
    assert harness.row_expectations(second) != []
    assert lane_failures(harness.row_expectations(second)) == []
    blind = replace(second, watcher_healthy=False)
    assert lane_failures(harness.row_expectations(blind)) != []


def test_the_row_verdict_fails_an_undecided_lane_and_records_an_overlap():
    assert problems_for(_record(_lingering_member()), daemon=True) == []
    assert problems_for(_record(_settled_tree()), daemon=True) == []
    [undecided] = problems_for(_record(_settled_tree(), successor=None), daemon=True)
    assert "inconclusive" in undecided
    assert problems_for(_record(_settled_tree(), platform="linux"), daemon=True) != []


# --- The contender, for real ------------------------------------------------------


@pytest.fixture
def lease(tmp_path):
    """The product's lease on a temporary auth root, held by this process."""
    held = get_profile_lease(tmp_path / "auth" / "profile")
    assert held.try_acquire()
    yield held
    while held.held:
        held.release()


def test_the_contender_takes_the_lease_once_and_sees_a_watched_process_end(
    tmp_path, lease
):
    # Ended and reaped, though on Windows this Popen still holds its handle.
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended_start = psutil.Process(ended.pid).create_time()
    ended.wait()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    start = psutil.Process(child.pid).create_time()
    contender = Contender(
        tmp_path / "contender",
        lease.auth_root / "profile.lock",
        [
            {"pid": child.pid, "start": start, "kind": BROWSER},
            {"pid": ended.pid, "start": ended_start, "kind": BROWSER},
        ],
        interval=0.005,
        deadline=60.0,
    )
    try:
        contender.start()
        ready = contender.wait_ready(30.0)
        assert ready is not None and ready["state"] == HELD, ready
        released = time.time_ns()
        lease.release()
        assert contender.wait_granted(30.0) is not None
        # Granted once, the contender never asks again: the product takes
        # the lease and holds it with nobody refused or counted.
        assert lease.try_acquire()
        time.sleep(0.2)
        before_kill = time.time_ns()
        child.kill()
        child.wait()
        after_wait = time.time_ns()
        time.sleep(0.2)
        result = contender.stop(15.0)
    finally:
        child.kill()
        child.wait()
        contender.end(5.0)
    assert result["error"] is None, result
    assert [(run["state"], run["count"]) for run in result["lease"]] == [
        (HELD, result["lease"][0]["count"]),
        (FREE, 1),
    ]
    assert result["lease"][1]["first_wall_ns"] >= released
    assert 0 < result["hold_ns"] < 200 * MS
    watched, gone = result["watched"]
    assert watched["opened"] is True and watched["verified"] is not False
    assert watched["last_alive_wall_ns"] < after_wait
    assert before_kill <= watched["gone_wall_ns"]
    assert watched["last_alive_wall_ns"] < watched["gone_wall_ns"]
    assert gone["gone_before_watched"] is True
    assert gone["gone_wall_ns"] < released
    # Nothing announced.
    assert not (lease.auth_root / "profile.handoff").exists()
