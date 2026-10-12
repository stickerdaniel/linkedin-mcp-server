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
from typing import Any

import psutil
import pytest

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
    o1_findings,
    problems_for,
)
from linkedin_mcp_server.profile_lease import get_profile_lease

MS = 1_000_000
#: The kill, on the wall clock in nanoseconds.
KILL = 1_800_000_000 * 10**9
#: When the old processes began, long before the kill.
OLD = 1_799_999_000.0


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


def _run(state: str, first: float, last: float) -> dict[str, Any]:
    return {
        "state": state,
        "reason": None,
        "first_wall_ns": KILL + int(first * MS),
        "last_wall_ns": KILL + int(last * MS),
        "first_ns": 0,
        "last_ns": 0,
        "count": 1,
    }


#: The lease held before the kill, seen free 60 ms after it, and taken again
#: by the successor.
LEASE = [_run(HELD, -500, -1), _run(FREE, 60, 2900), _run(HELD, 2950, 6000)]


def _record(
    watched: list[dict[str, Any]],
    *,
    successor: float | None = 3000,
    lease: list[dict[str, Any]] = LEASE,
    ready: float = -500,
    untracked: list[dict[str, Any]] | None = None,
    platform: str = "win32",
) -> dict[str, Any]:
    """A daemon record whose successor's root began *successor* ms after the
    kill, or never; the old root is pid 10."""
    armed = [
        {"pid": entry["pid"], "start": entry["start"], "ppid": 1, "kind": entry["kind"]}
        for entry in watched
        if entry["kind"] != ACTOR
    ]
    roots: list[list[Any]] = [[10, OLD, _at(45), 3, OLD]]
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
        "contended": {
            "role": "owner",
            "actor": {"pid": 2, "start": OLD},
            "armed": armed,
            "untracked": untracked or [],
            "kill": {"wall_ns": KILL, "monotonic_ns": 1, "exit": "killed"},
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
        _life(10, BROWSER_ROOT, alive=38, gone=42),
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


def test_a_member_never_watched_keeps_no_overlap_from_being_claimed():
    tree = _settled_tree()
    tree[4] = _life(12, BROWSER, alive=None, gone=None, opened=False)
    assert crash_reading(_record(tree)).verdict == INCONCLUSIVE
    # One already gone when the contender first looked ended before the kill.
    tree[4] = _life(
        12, BROWSER, alive=None, gone=-700, opened=False, gone_before_watched=True
    )
    assert crash_reading(_record(tree)).verdict == NO_OVERLAP
    late = {"pid": 13, "start": _at(-200), "ppid": 10, "kind": BROWSER}
    reading = crash_reading(_record(_settled_tree(), untracked=[late]))
    assert reading.verdict == INCONCLUSIVE
    assert "never watched" in reading.reasons[0]


def test_a_contender_not_armed_before_the_kill_decides_nothing():
    assert crash_reading(_record(_lingering_member(), ready=10)).verdict == INCONCLUSIVE
    free = [_run(FREE, -500, 2900), _run(HELD, 2950, 6000)]
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
    if held.held:
        held.release()


def test_the_contender_sees_the_lease_come_free_and_a_watched_process_end(
    tmp_path, lease
):
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
        time.sleep(0.2)
        child.kill()
        child.wait()
        killed = time.time_ns()
        time.sleep(0.2)
        result = contender.stop(15.0)
    finally:
        child.kill()
        child.wait()
        contender.end(5.0)
    assert result["error"] is None, result
    assert [run["state"] for run in result["lease"]] == [HELD, FREE]
    assert result["lease"][1]["first_wall_ns"] >= released
    watched, gone = result["watched"]
    assert watched["opened"] is True and watched["verified"] is not False
    assert watched["last_alive_wall_ns"] < killed <= watched["gone_wall_ns"]
    assert gone["gone_before_watched"] is True
    assert gone["gone_wall_ns"] < released
    # Nothing announced, and nothing held: the product takes the lease again.
    assert not (lease.auth_root / "profile.handoff").exists()
    assert lease.try_acquire()
