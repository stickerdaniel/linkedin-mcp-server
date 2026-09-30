"""H-R3's and H-R2's verdicts, their producer and their place in the row.

Three families, none of them with a browser. The **contract** tests start
from an explicit, valid raw record per row, mode and platform, change one
observation, and run the verdict the row runs (``host_comparison.r3_problems``
and ``r2_problems``); none derives its expectation from the verdict. The **producer** tests run ``harness.observe_checkpoint`` over a
modelled process table and hand what it read to that verdict. The **wiring**
tests go through the real row entry, ``measure_host_quit_row``, on the
preservation gate's modelled row, with the host session and the checkpoint
reader replaced by doubles that keep the real seams: the row's own script,
its post-exit hook, its settlement gate and its published ``failures.json``.
For H-R2 the second host goes through that same host seam, started by the
row's own script, and every read adds a request to the origin while it runs.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import io
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import psutil
import pytest

from differential import harness, lease_probe, unconfirmed_close
from differential import test_preservation_gate as gate
from differential.baseline import Runtime
from differential.events import EventLog
from differential.host_comparison import (
    _HANDED_OVER,
    AFTER_A1,
    AFTER_B_QUIT,
    BEFORE_QUIT,
    CHECKPOINTS,
    FIRST_POST_EXIT,
    K2_NOT_APPLICABLE,
    OWNER_READS,
    R2_CALLS,
    ROW_H_R2,
    ROW_H_R3,
    SETTLED,
    attribute_requests,
    census_roots,
    comparison_refusals,
    distinct_roots,
    handoff_reading,
    problems_for,
    r2_problems,
    r3_problems,
    semantic_differences,
    semantics,
)
from differential import synthetic_origin
from differential.synthetic_origin import OriginRequest
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _SETTLED,
    profile,
    row,
)
from differential.unconfirmed_close import R7Setup, UnsettledWorker
from linkedin_mcp_server.config.loaders import EnvironmentKeys

#: Taken before any fixture replaces them.
REAL_ACTOR_ENVIRONMENT = harness.actor_environment

KEY = "/tmp/h-r3-profile"
OWNER = (4321, 100.0)
SERVER = (4242, 90.0)
LOCK = [7, 99]
ROOT = (500, 110.0)
MS = 1_000_000


def _entry(pid: int, ppid: int, start: float, *, child: bool = False) -> dict:
    flags = ["--type=renderer"] if child else []
    return {
        "pid": pid,
        "ppid": ppid,
        "start": start,
        "profile": None if child else KEY,
        "cmdline": ["chrome", *flags, f"--user-data-dir={KEY}"],
    }


def _point(
    label: str,
    began_ms: int,
    ended_ms: int,
    *,
    actor: Sequence[Any],
    alive: bool,
    occupied: bool,
    lock: str,
    root: tuple[int, float] = ROOT,
) -> dict:
    """One checkpoint as ``observe_checkpoint`` writes it: the root and a
    renderer of it on the profile when *occupied*, the root's lineage through
    a driver to *actor*, and the contender and holder answers."""
    census: dict[str, Any] = {"entries": [], "unresolved": []}
    lineages = []
    if occupied:
        census["entries"] = [
            _entry(root[0], 499, root[1]),
            _entry(root[0] + 1, root[0], root[1] + 1.0, child=True),
        ]
        lineages = [
            {
                "pid": root[0],
                "start": root[1],
                "ancestors": [[499, 105.0], list(actor)],
                "complete": True,
            }
        ]
    return {
        "label": label,
        "began": 1_000.0 + began_ms / 1000,
        "ended": 1_000.0 + ended_ms / 1000,
        "began_ns": began_ms * MS,
        "ended_ns": ended_ms * MS,
        "lifetime": list(actor),
        "census": census,
        "lineages": lineages,
        "lock": {
            "now": list(LOCK),
            "answer": {"state": lock, "reason": "", "device": 7, "inode": 99},
            "association": {
                "state": "holder" if lock == "held" else "not the holder",
                "holder": list(actor),
                "identity": list(LOCK),
                "same_before": True,
                "same_after": True,
            },
        },
        "actor_alive": [alive, alive],
    }


def _record(*, daemon: bool = True, platform: str = "linux") -> dict:
    """A valid H-R3 record. The read is sent at 100s and returns at 104s; the
    windows before the quit and, for the daemon, after the exit end well
    inside the 15s the 20s idle timeout leaves."""
    actor = OWNER if daemon else SERVER
    record: dict[str, Any] = {
        "row": ROW_H_R3,
        "mode": "daemon" if daemon else "direct",
        "platform": platform,
        "browser_key": KEY,
        "idle_timeout_seconds": 20.0,
        "k2": dict(K2_NOT_APPLICABLE),
        "actor": list(actor),
        "lock": list(LOCK),
        "observation_problems": [],
        "script_error": None,
        "after_exit_error": None,
        "call": {
            "began": 1_100.0,
            "ended": 1_104.0,
            "began_monotonic_ns": 100_000 * MS,
            "ended_monotonic_ns": 104_000 * MS,
            "is_error": False,
            "read_the_post": True,
        },
        "host": {
            "error": None,
            "alive_before_quit": True,
            "stdin_closed": True,
            "exited_on_quit": True,
            "exit_code": 0,
            "killed_by_harness": False,
            "stderr_closed": True,
            "eof_ns": 107_000 * MS,
            "exit_seen_ns": 108_000 * MS,
        },
        "checkpoints": [
            _point(
                BEFORE_QUIT,
                105_000,
                106_000,
                actor=actor,
                alive=True,
                occupied=True,
                lock="held",
            ),
            _point(
                FIRST_POST_EXIT,
                108_100,
                109_000,
                actor=actor,
                alive=daemon,
                occupied=daemon,
                lock="held" if daemon else "free",
            ),
            _point(
                SETTLED,
                131_000,
                132_000,
                actor=actor,
                alive=False,
                occupied=False,
                lock="free",
            ),
        ],
        "cleanup_began_ns": 133_000 * MS,
    }
    if daemon:
        record["owner_exit"] = {
            "how": "exited",
            "seen_ns": 130_000 * MS,
            "seconds_after_quit": 22.0,
        }
    return record


def _at(record: dict, label: str) -> dict:
    return next(p for p in record["checkpoints"] if p["label"] == label)


# --- Contract: a valid record, then one observation changed ---------------------


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("daemon", [True, False], ids=["daemon", "direct"])
def test_a_valid_record_passes(daemon, platform):
    assert r3_problems(_record(daemon=daemon, platform=platform), daemon=daemon) == []


@pytest.mark.parametrize("names_the_profile", [False, True])
def test_a_process_inside_the_roots_tree_is_no_root_of_its_own(names_the_profile):
    # The census holds the root and its renderer: two processes, one root.
    # A child whose own reading names the profile stays in its parent's tree.
    record = _record(daemon=True)
    point = _at(record, BEFORE_QUIT)
    assert len(point["census"]["entries"]) == 2
    if names_the_profile:
        point["census"]["entries"][1]["profile"] = KEY
    assert census_roots(point["census"], KEY) == [ROOT]
    assert r3_problems(record, daemon=True) == []


def _set(path: str, value: Any) -> Callable[[dict], None]:
    """Set the field at *path*, ``checkpoint label/field/...`` or ``field/...``."""

    def change(record: dict) -> None:
        parts = path.split("/")
        target: Any = record
        if parts[0] in (*CHECKPOINTS, AFTER_A1, AFTER_B_QUIT):
            target = _at(record, parts.pop(0))
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        last = parts[-1]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return change


def _second_root(label: str) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        _at(record, label)["census"]["entries"].append(_entry(600, 1, 120.0))

    return change


def _drop(label: str) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        record["checkpoints"] = [
            p for p in record["checkpoints"] if p["label"] != label
        ]

    return change


def _swap(record: dict) -> None:
    points = record["checkpoints"]
    points[0], points[1] = points[1], points[0]


def _late_owner_gone(record: dict) -> None:
    """A late first post-exit window in which the owner and its browser have
    already gone, as a healthy owner's idle exit leaves them."""
    point = _at(record, FIRST_POST_EXIT)
    fresh = _point(
        FIRST_POST_EXIT,
        115_500,
        116_000,
        actor=OWNER,
        alive=False,
        occupied=False,
        lock="free",
    )
    point.update(fresh)
    record["host"]["eof_ns"] = 107_000 * MS
    record["host"]["exit_seen_ns"] = 115_000 * MS


DAEMON_CASES = [
    # Clock and lifecycle boundaries.
    pytest.param(
        # Sent at 100s, received at 119s, checkpoint 120s to 121s: 2s after
        # receipt, 21s after the send, so the owner may already be idle.
        [
            _set("call/ended_monotonic_ns", 119_000 * MS),
            _set(f"{BEFORE_QUIT}/began_ns", 120_000 * MS),
            _set(f"{BEFORE_QUIT}/ended_ns", 121_000 * MS),
        ],
        f"{BEFORE_QUIT}: the window is late",
        id="receipt-would-look-fresh",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/ended_ns", 115_000 * MS)],
        f"{FIRST_POST_EXIT}: the window is late",
        id="checkpoint-ended-past-the-bound",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/began_ns", 106_500 * MS)],
        f"{BEFORE_QUIT}: the checkpoint's own times are not in order",
        id="reversed-checkpoint",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/ended_ns", None)],
        f"{BEFORE_QUIT}: the checkpoint's own times are not in order",
        id="missing-end",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/ended_ns", 106.0e9)],
        f"{BEFORE_QUIT}: the checkpoint's own times are not in order",
        id="float-time",
    ),
    pytest.param(
        [_set("call/began_monotonic_ns", None)],
        "the call's or the checkpoint's times are missing",
        id="missing-send",
    ),
    pytest.param(
        [_set("call/began_monotonic_ns", True)],
        "the call's or the checkpoint's times are missing",
        id="bool-send",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/began_ns", 103_000 * MS)],
        "out of order",
        id="checkpoint-before-receipt",
    ),
    pytest.param(
        [_set("idle_timeout_seconds", float("nan"))],
        "leaves no window",
        id="non-finite-idle-timeout",
    ),
    pytest.param(
        [_set("host/eof_ns", 105_500 * MS)],
        f"{BEFORE_QUIT}: it ended after the EOF was sent",
        id="before-quit-after-eof",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/began_ns", 107_500 * MS)],
        f"{FIRST_POST_EXIT}: it began before the exit was seen",
        id="post-exit-before-exit",
    ),
    pytest.param(
        [_set("host/exit_seen_ns", None)],
        "the EOF and the exit after it are not recorded in order",
        id="exit-not-seen",
    ),
    pytest.param(
        # Fresh, and the owner is gone: a truly early loss.
        [_set(f"{FIRST_POST_EXIT}/actor_alive", [False, False])],
        f"{FIRST_POST_EXIT}: the owner was gone, not alive",
        id="owner-lost-in-a-fresh-window",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/actor_alive", [True, False])],
        f"{FIRST_POST_EXIT}: the owner was transition, not alive",
        id="owner-left-during-the-checkpoint",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/actor_alive", [True, None])],
        f"{FIRST_POST_EXIT}: the owner was unknown, not alive",
        id="owner-liveness-unread",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/lock/answer/state", "free")],
        f"{FIRST_POST_EXIT}: the lock was free, not held",
        id="lease-released-in-a-fresh-window",
    ),
    pytest.param(
        [_set("owner_exit/how", "still running")],
        "the owner was not seen to exit by itself",
        id="owner-never-left",
    ),
    pytest.param(
        [_set("owner_exit/seen_ns", 131_500 * MS)],
        f"{SETTLED}: it began before the owner's exit was seen",
        id="settled-before-owner-exit",
    ),
    pytest.param(
        [_set("cleanup_began_ns", 131_500 * MS)],
        f"{SETTLED}: it is not shown to precede the cleanup",
        id="settled-after-cleanup",
    ),
    pytest.param(
        [_set("cleanup_began_ns", None)],
        f"{SETTLED}: it is not shown to precede the cleanup",
        id="cleanup-unmarked",
    ),
    pytest.param(
        [_set(f"{SETTLED}/began_ns", 108_500 * MS)],
        f"{SETTLED}: it began before {FIRST_POST_EXIT} ended",
        id="settled-overlaps-post-exit",
    ),
    pytest.param(
        [_set(f"{SETTLED}/actor_alive", [True, True])],
        f"{SETTLED}: the owner was alive, not gone",
        id="owner-alive-at-settlement",
    ),
    # Classification and capabilities.
    pytest.param(
        [_second_root(FIRST_POST_EXIT)],
        f"{FIRST_POST_EXIT}: 2 browser roots on the profile, not one",
        id="a-second-independent-root",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/census/unresolved", [777])],
        f"{BEFORE_QUIT}: unknown browser roots on the profile, not one",
        id="unresolved-census-is-not-a-count",
    ),
    pytest.param(
        [_set(f"{SETTLED}/census/unresolved", [777])],
        f"{SETTLED}: the profile census was incomplete, not empty",
        id="unresolved-census-is-not-empty",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/census/entries/0/ppid", None)],
        f"{BEFORE_QUIT}: unknown browser roots on the profile, not one",
        id="unread-parent",
    ),
    pytest.param(
        [_set(f"{SETTLED}/census/entries", [_entry(501, 1, 111.0, child=True)])],
        f"{SETTLED}: the profile census was occupied, not empty",
        id="orphaned-child-is-not-empty",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/census/entries/0/start", 150.0)],
        f"{FIRST_POST_EXIT}: the root is not the one {BEFORE_QUIT} read",
        id="same-pid-another-root",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/lineages/0/ancestors/1", [OWNER[0], 50.0])],
        f"{BEFORE_QUIT}: the root is not shown to descend from the owner",
        id="owner-pid-reused-in-the-lineage",
    ),
    pytest.param(
        [
            _set(f"{BEFORE_QUIT}/lineages/0/ancestors", [[499, 105.0]]),
            _set(f"{BEFORE_QUIT}/lineages/0/complete", False),
        ],
        f"{BEFORE_QUIT}: the root is not shown to descend from the owner",
        id="lineage-cut-short",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/lock/now", [7, 100])],
        f"{FIRST_POST_EXIT}: the lock was replaced, not held",
        id="lock-file-replaced",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/lock/answer/inode", 100)],
        f"{BEFORE_QUIT}: the lock was replaced, not held",
        id="contender-opened-another-file",
    ),
    pytest.param(
        [_set("lock", None)],
        f"{BEFORE_QUIT}: the lock was unknown, not held",
        id="no-lock-identified",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/lock/answer/state", "unknown")],
        f"{BEFORE_QUIT}: the lock was unknown, not held",
        id="linux-contender-failed",
    ),
    pytest.param(
        [_set(f"{SETTLED}/lock/answer/state", "held")],
        f"{SETTLED}: the lock was held, not free",
        id="lock-held-after-settlement",
    ),
    pytest.param(
        [_set(f"{SETTLED}/lock/answer/state", "unknown")],
        f"{SETTLED}: the lock was unknown, not free",
        id="settlement-contender-failed",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/lock/association/state", "not the holder")],
        f"{BEFORE_QUIT}: the holder is not the actor, not the owner",
        id="another-holder",
    ),
    pytest.param(
        [_set(f"{FIRST_POST_EXIT}/lock/association/same_after", False)],
        f"{FIRST_POST_EXIT}: the holder is unknown, not the owner",
        id="holder-pid-reused",
    ),
    pytest.param(
        [_set(f"{BEFORE_QUIT}/lock/association/holder", [OWNER[0], 50.0])],
        f"{BEFORE_QUIT}: the holder is unknown, not the owner",
        id="holder-another-lifetime",
    ),
    # The whole window, and what the row recorded around it.
    pytest.param([_drop(FIRST_POST_EXIT)], "the checkpoints were", id="missing"),
    pytest.param([_swap], "the checkpoints were", id="out-of-order"),
    pytest.param(
        [lambda r: r["checkpoints"].append(copy.deepcopy(_at(r, SETTLED)))],
        "the checkpoints were",
        id="duplicate",
    ),
    pytest.param(
        [_set(f"{SETTLED}/error", "UnsettledWorker: before checkpoint: settled")],
        f"{SETTLED}: the checkpoint failed",
        id="checkpoint-error",
    ),
    pytest.param(
        [_set("script_error", "RuntimeError: planted")],
        "the row's script failed",
        id="script-error",
    ),
    pytest.param(
        [_set("after_exit_error", "RuntimeError: planted")],
        "the post-exit hook failed",
        id="hook-error",
    ),
    pytest.param(
        [_set("observation_problems", ["the owner was never identified"])],
        "the owner was never identified",
        id="observation-problem",
    ),
    pytest.param(
        [_set("actor", None)], "the owner was never identified", id="no-actor"
    ),
    pytest.param([_set("call", None)], "the read call was not recorded", id="no-call"),
    pytest.param(
        [_set("call/read_the_post", False)],
        "the read did not return the synthetic post",
        id="read-failed",
    ),
    pytest.param(
        [_set("host/killed_by_harness", True)],
        "the host's quit was not a normal EOF exit",
        id="forced-cleanup",
    ),
    pytest.param(
        [_set("host/exit_code", 1)],
        "the host's quit was not a normal EOF exit",
        id="nonzero-exit",
    ),
    pytest.param([_set("mode", "direct")], "the record is for mode", id="wrong-mode"),
    pytest.param([_set("row", "H-R1")], "the record is for row", id="wrong-row"),
]


@pytest.mark.parametrize(("changes", "reported"), DAEMON_CASES)
def test_one_changed_observation_fails_the_daemon_record(changes, reported):
    record = _record(daemon=True)
    for change in changes:
        change(record)
    problems = r3_problems(record, daemon=True)
    assert any(reported in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        pytest.param(
            [_set(f"{BEFORE_QUIT}/lock/association/state", "not the holder")],
            f"{BEFORE_QUIT}: the holder is not the actor, not the server",
            id="linux-direct-holder-before-quit",
        ),
        pytest.param(
            [_set(f"{FIRST_POST_EXIT}/actor_alive", [True, True])],
            f"{FIRST_POST_EXIT}: the server was alive, not gone",
            id="server-still-running-after-its-exit",
        ),
        pytest.param(
            [_set(f"{FIRST_POST_EXIT}/lock/answer/state", "unknown")],
            f"{FIRST_POST_EXIT}: the lock was unknown",
            id="post-exit-contender-failed",
        ),
        pytest.param(
            [_second_root(SETTLED)],
            f"{SETTLED}: the profile census was occupied, not empty",
            id="root-left-at-settlement",
        ),
        pytest.param([_drop(SETTLED)], "the checkpoints were", id="missing-settlement"),
    ],
)
def test_one_changed_observation_fails_the_direct_record(changes, reported):
    record = _record(daemon=False)
    for change in changes:
        change(record)
    problems = r3_problems(record, daemon=False)
    assert any(reported in problem for problem in problems), problems


def test_a_late_window_is_evidence_only_never_a_premature_exit():
    # A healthy owner idles out once the window has passed: the late reading
    # is reported as late, and nothing it saw is judged as a loss.
    record = _record(daemon=True)
    _late_owner_gone(record)
    problems = r3_problems(record, daemon=True)
    about = [p for p in problems if p.startswith(FIRST_POST_EXIT)]
    assert len(about) == 1 and "the window is late" in about[0], problems


def test_the_macos_contender_is_not_forgiven():
    record = _record(daemon=True, platform="darwin")
    _set(f"{BEFORE_QUIT}/lock/answer/state", "unknown")(record)
    problems = r3_problems(record, daemon=True)
    assert f"{BEFORE_QUIT}: the lock was unknown, not held" in problems
    # macOS has no holder association to ask; its absence is not a problem.
    _set(f"{BEFORE_QUIT}/lock/answer/state", "held")(record)
    for point in record["checkpoints"]:
        point["lock"].pop("association")
    assert r3_problems(record, daemon=True) == []


@pytest.mark.parametrize("daemon", [True, False], ids=["daemon", "direct"])
def test_windows_lock_state_is_unobserved_and_never_credited(daemon):
    record = _record(daemon=daemon, platform="win32")
    for point in record["checkpoints"]:
        point["lock"] = {"now": list(LOCK)}
    assert r3_problems(record, daemon=daemon) == []
    read = semantics(record)["checkpoints"]
    assert {read[label]["lock"] for label in CHECKPOINTS} == {"unobserved"}
    assert {read[label]["holder"] for label in CHECKPOINTS} == {"unobserved"}


def test_a_direct_first_reading_that_is_not_yet_empty_is_kept_as_read():
    record = _record(daemon=False)
    lingering = _point(
        FIRST_POST_EXIT,
        110_100,
        111_000,
        actor=SERVER,
        alive=False,
        occupied=True,
        lock="free",
    )
    _at(record, FIRST_POST_EXIT).update(lingering)
    record["host"]["exit_seen_ns"] = 110_000 * MS
    assert r3_problems(record, daemon=False) == []
    assert semantics(record)["checkpoints"][FIRST_POST_EXIT]["census"] == "occupied"


@pytest.mark.parametrize("daemon", [True, False], ids=["daemon", "direct"])
def test_the_verdict_survives_the_published_packet(daemon):
    good = json.loads(json.dumps(_record(daemon=daemon)))
    assert r3_problems(good, daemon=daemon) == []
    bad = _record(daemon=daemon)
    _drop(SETTLED)(bad)
    assert r3_problems(json.loads(json.dumps(bad)), daemon=daemon)


# --- K0 and the comparison with K1 ----------------------------------------------


def _another_daemon_run() -> dict:
    """A valid daemon record from another run: other pids, times and lock."""
    record = _record(daemon=True)
    record["actor"] = [9876, 300.0]
    record["lock"] = [8, 42]
    shift = 400_000 * MS
    for key in ("began_monotonic_ns", "ended_monotonic_ns"):
        record["call"][key] += shift
    for key in ("eof_ns", "exit_seen_ns"):
        record["host"][key] += shift
    record["owner_exit"]["seen_ns"] += shift
    record["cleanup_began_ns"] += shift
    for point in record["checkpoints"]:
        moved = _point(
            point["label"],
            point["began_ns"] // MS + 400_000,
            point["ended_ns"] // MS + 400_000,
            actor=record["actor"],
            alive=point["actor_alive"][0],
            occupied=bool(point["census"]["entries"]),
            lock=point["lock"]["answer"]["state"],
            root=(700, 310.0),
        )
        moved["lock"]["now"] = [8, 42]
        moved["lock"]["answer"].update(device=8, inode=42)
        moved["lock"]["association"]["identity"] = [8, 42]
        point.update(moved)
    return record


def test_k0_compares_classifications_not_pids_or_times():
    other = _another_daemon_run()
    assert r3_problems(other, daemon=True) == []
    assert semantic_differences(_record(daemon=True), other, daemon=True) == []


def test_k0_reports_a_different_classification():
    # Two valid Direct records whose first post-exit readings differ.
    empty, lingering = _record(daemon=False), _record(daemon=False)
    _at(lingering, FIRST_POST_EXIT).update(
        _point(
            FIRST_POST_EXIT,
            108_100,
            109_000,
            actor=SERVER,
            alive=False,
            occupied=True,
            lock="free",
        )
    )
    differences = semantic_differences(empty, lingering, daemon=False)
    assert len(differences) == 1 and differences[0].startswith(FIRST_POST_EXIT)


@pytest.mark.parametrize(
    ("reference", "repeat"),
    [
        pytest.param(None, _record(), id="no-reference"),
        pytest.param(_record(), None, id="no-repeat"),
    ],
)
def test_k0_without_both_records_is_a_refusal(reference, repeat):
    assert semantic_differences(reference, repeat, daemon=True)


def test_k0_refuses_an_invalid_repeat_even_when_it_reads_alike():
    # Same classifications, and the owner's exit unobserved: invalid, so no
    # equality is read from it.
    repeat = _record(daemon=True)
    repeat["owner_exit"]["seen_ns"] = None
    problems = semantic_differences(_record(daemon=True), repeat, daemon=True)
    assert any("the repeat record is not valid" in p for p in problems), problems


def test_k3_is_held_to_k1_only_with_both_valid_records():
    assert comparison_refusals(_record(daemon=False), _record(daemon=True)) == []
    assert comparison_refusals(None, _record(daemon=True))
    bad = _record(daemon=False)
    _drop(BEFORE_QUIT)(bad)
    assert comparison_refusals(bad, _record(daemon=True))


# --- The producer: observe_checkpoint over a modelled process table ------------


class _Process:
    """A modelled ``psutil.Process``: its census reading, parent and start."""

    def __init__(self, table: dict, pid: int, ppid: int, start: float, cmdline):
        self.table, self.pid, self._ppid, self._start = table, pid, ppid, start
        self.info = {"cmdline": cmdline, "exe": "/b/chrome", "status": "running"}
        table[pid] = self

    def ppid(self) -> int:
        return self._ppid

    def create_time(self) -> float:
        return self._start

    def parent(self):
        return self.table.get(self._ppid)

    def status(self) -> str:
        return psutil.STATUS_RUNNING

    def is_running(self) -> bool:
        return True


def _table(profile_dir: Path) -> dict:
    """An owner, its driver, the browser root and one renderer on the profile."""
    flag = f"--user-data-dir={profile_dir}"
    table: dict = {}
    _Process(table, 1, 0, 1.0, ["init"])
    _Process(table, OWNER[0], 1, OWNER[1], ["python", "-m", "owner"])
    _Process(table, 499, OWNER[0], 105.0, ["node", "run-driver"])
    _Process(table, ROOT[0], 499, ROOT[1], ["chrome", flag])
    _Process(table, ROOT[0] + 1, ROOT[0], 111.0, ["chrome", "--type=renderer", flag])
    return table


def _observe(
    tmp_path,
    monkeypatch,
    table: dict,
    *,
    lock="held",
    holder="holder",
    reopened: dict | None = None,
):
    """``observe_checkpoint`` before the quit over *table*; a pid in
    *reopened* names that other process once the census has read it."""
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    account.profile.mkdir(parents=True, exist_ok=True)
    lock_path = account.auth_root / "profile.lock"
    lock_path.write_text("")
    identity = harness.lock_identity(lock_path)
    assert identity is not None

    def probe(path, **kwargs):
        return {
            "state": lock,
            "reason": "",
            "device": identity[0],
            "inode": identity[1],
        }

    monkeypatch.setattr(lease_probe, "run_probe", probe)
    monkeypatch.setattr(harness, "lock_association", lambda *a, **k: {"state": holder})

    def open_process(pid: int):
        if reopened and pid in reopened:
            return reopened[pid]
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return table[pid]

    point = harness.observe_checkpoint(
        BEFORE_QUIT,
        account,
        actor=(table.get(OWNER[0]), OWNER[0], OWNER[1]),
        lock_path=lock_path,
        lock=None,
        platform="linux",
        process_iter=lambda *a, **k: [p for p in table.values() if p.pid != 1],
        open_process=open_process,
    )
    return account, identity, point


def _judged(account, identity, point) -> list[str]:
    """What the verdict says of *point* as the only before-quit reading."""
    record = _record(daemon=True)
    record["browser_key"] = account.browser_key
    record["lock"] = list(identity)
    record["checkpoints"][0] = {
        **point,
        "began_ns": 105_000 * MS,
        "ended_ns": 106_000 * MS,
    }
    return [p for p in r3_problems(record, daemon=True) if p.startswith(BEFORE_QUIT)]


def test_the_producer_reads_one_root_of_the_owner_holding_the_lock(
    tmp_path, monkeypatch
):
    table = _table(tmp_path / "auth" / "profile")
    account, identity, point = _observe(tmp_path, monkeypatch, table)
    # The renderer is in the census and is no root.
    assert len(point["census"]["entries"]) == 2
    assert [(lin["pid"], lin["start"]) for lin in point["lineages"]] == [ROOT]
    # The walk stops at the owner and says so.
    assert point["lineages"][0]["ancestors"][-1] == list(OWNER)
    assert point["lineages"][0]["complete"] is True
    assert point["lock"]["association"]["same_before"] is True
    assert point["actor_alive"] == [True, True]
    assert _judged(account, identity, point) == []


def test_the_producer_leaves_a_reused_owner_pid_unshown(tmp_path, monkeypatch):
    table = _table(tmp_path / "auth" / "profile")
    # The owner's pid now names another process, begun later.
    _Process(table, OWNER[0], 1, 200.0, ["python", "other"])
    account, identity, point = _observe(tmp_path, monkeypatch, table)
    problems = _judged(account, identity, point)
    assert f"{BEFORE_QUIT}: the root is not shown to descend from the owner" in problems
    assert f"{BEFORE_QUIT}: the holder is unknown, not the owner" in problems


def test_the_producer_walks_no_lineage_of_a_root_pid_taken_since(tmp_path, monkeypatch):
    table = _table(tmp_path / "auth" / "profile")
    # Between the census and the lineage, the root's pid went to a younger
    # process of the same driver: its ancestry is not the root's.
    other = _Process({}, ROOT[0], 499, 150.0, ["chrome"])
    other.table = table
    account, identity, point = _observe(
        tmp_path, monkeypatch, table, reopened={ROOT[0]: other}
    )
    assert point["lineages"][0]["complete"] is False
    problems = _judged(account, identity, point)
    assert f"{BEFORE_QUIT}: the root is not shown to descend from the owner" in problems


def test_the_producer_keeps_an_unreadable_census_unknown(tmp_path, monkeypatch):
    table = _table(tmp_path / "auth" / "profile")
    monkeypatch.setattr(harness, "harness_user", lambda: "me")
    monkeypatch.setattr(harness, "process_user", lambda process: "me")
    table[ROOT[0]].info["cmdline"] = None
    account, identity, point = _observe(tmp_path, monkeypatch, table)
    assert point["census"]["unresolved"] == [ROOT[0]]
    problems = _judged(account, identity, point)
    assert f"{BEFORE_QUIT}: unknown browser roots on the profile, not one" in problems


# --- Wiring: the real row entry, with the host and the reader as doubles --------


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Fresh registries, so one test's retained worker never gates the next."""
    monkeypatch.setattr(unconfirmed_close, "_OWNED", [])
    monkeypatch.setattr(unconfirmed_close, "_RETAINED", [])
    monkeypatch.setattr(lease_probe, "_OWNED", [])


class _Scene:
    """The modelled row for H-R3: what the host does, what each checkpoint
    reads, and every checkpoint the row asked for, in order."""

    def __init__(self, modelled_row, monkeypatch, tmp_path, *, daemon: bool):
        self.row, self.daemon, self.tmp_path = modelled_row, daemon, tmp_path
        self.actor = [42, 1.0] if daemon else [4242, 7.0]
        self.asked: list[str] = []
        self.readings: dict[str, Callable[[], dict]] = {
            BEFORE_QUIT: lambda: self.reading(alive=True, occupied=True, lock="held"),
            FIRST_POST_EXIT: lambda: self.reading(
                alive=daemon, occupied=daemon, lock="held" if daemon else "free"
            ),
            SETTLED: lambda: self.reading(alive=False, occupied=False, lock="free"),
        }
        self.hold: dict[str, threading.Event] = {}
        self.exits = True
        self.timed = True
        #: How host A's quit is recorded, when not the normal one.
        self.flags: dict[str, Any] = {}
        self.preservation = AsyncMock(return_value=harness.PostQuit(valid=True))
        monkeypatch.setattr(harness, "observe_preservation", self.preservation)
        monkeypatch.setattr(harness, "observe_checkpoint", self.observe)
        inner = harness.run_host_session
        scene = self

        async def host(*args, script=None, after_exit=None, **kwargs):
            base = await inner(*args, **kwargs)
            began, began_ns = time.time(), time.monotonic_ns()
            tool = dict(base.tool or {})
            if scene.timed:
                tool.update(
                    tool=harness.READ_TOOL,
                    began=began,
                    ended=time.time(),
                    began_monotonic_ns=began_ns,
                    ended_monotonic_ns=time.monotonic_ns(),
                )
            session = dataclasses.replace(
                base,
                tool=tool,
                stderr=list(base.stderr) if daemon else [],
                user_lines=list(base.user_lines) if daemon else [],
            )
            if script is not None:
                try:
                    await script(None)
                except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                    session.script_error = f"{type(exc).__name__}: {exc}"
            session.eof_monotonic_ns = time.monotonic_ns()
            if not scene.exits:
                # The harness gave up waiting and killed the server: the
                # stub's forced cleanup, where no hook runs.
                return dataclasses.replace(
                    session, exited_on_quit=False, killed_by_harness=True
                )
            session.exit_seen_monotonic_ns = time.monotonic_ns()
            if after_exit is not None:
                try:
                    await after_exit()
                except Exception as exc:  # noqa: BLE001 - as the real transport keeps it
                    session.after_exit_error = f"{type(exc).__name__}: {exc}"
            return dataclasses.replace(session, **scene.flags)

        monkeypatch.setattr(harness, "run_host_session", host)
        if not daemon:
            # A Direct row publishes nothing, and its server is associated by
            # the watcher's record of it.
            monkeypatch.setattr(
                harness.daemon_descriptor,
                "descriptor_path",
                lambda _root: tmp_path / "no-descriptor.json",
            )
            monkeypatch.setattr(
                harness,
                "associate_server",
                lambda pid, observed, **kw: (SimpleNamespace(pid=pid), 7.0),
            )

    def reading(self, *, alive: bool, occupied: bool, lock: str) -> dict:
        point = _point(
            "", 0, 0, actor=self.actor, alive=alive, occupied=occupied, lock=lock
        )
        point["census"]["entries"] = [
            {**entry, "profile": self.key if entry["profile"] else None}
            for entry in point["census"]["entries"]
        ]
        return point

    def observe(self, label, account, *, actor, lock, **kwargs) -> dict:
        self.key = account.browser_key
        self.asked.append(label)
        began, began_ns = time.time(), time.monotonic_ns()
        if label in self.hold:
            self.hold[label].wait(10)
        point = self.readings[label]()
        point.update(
            label=label,
            began=began,
            began_ns=began_ns,
            ended=time.time(),
            ended_ns=time.monotonic_ns(),
        )
        assert actor is None or [actor[1], actor[2]] == self.actor
        return point

    async def run(self, **options):
        key = harness.ActorAccount(self.tmp_path / "auth" / "profile").browser_key
        options.setdefault("row", ROW_H_R3)
        options.setdefault("experiment", "K3" if self.daemon else "K1")
        result, _ = await self.row(
            processes=[],
            summary={**_SETTLED, "max_roots": {key: 1}},
            daemon=self.daemon,
            **options,
        )
        return result, self.preservation.await_count

    def published(self) -> dict:
        return json.loads((self.tmp_path / "row" / "failures.json").read_text())


@pytest.fixture(params=[True, False], ids=["daemon", "direct"])
def scene(request, row, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    return _Scene(row, monkeypatch, tmp_path, daemon=request.param)


async def test_a_healthy_row_passes_and_publishes_its_whole_record(scene):
    result, preserved = await scene.run()

    assert result.failures == [], result.failures
    assert preserved == 1
    assert scene.asked == list(CHECKPOINTS)
    published = scene.published()["comparison"]
    assert [p["label"] for p in published["checkpoints"]] == list(CHECKPOINTS)
    assert published["k2"] == K2_NOT_APPLICABLE
    assert published["problems"] == []
    # Read again from the packet, the verdict is the same.
    assert r3_problems(published, daemon=scene.daemon) == []
    kinds = [r["kind"] for r in scene.row.log.records()]
    assert kinds.count("host.checkpoint") == 3


async def test_the_direct_first_reading_is_published_as_it_was_read(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
):
    scene = _Scene(row, monkeypatch, tmp_path, daemon=False)
    scene.readings[FIRST_POST_EXIT] = lambda: scene.reading(
        alive=False, occupied=True, lock="free"
    )
    result, _ = await scene.run()

    assert result.failures == [], result.failures
    published = scene.published()["comparison"]
    first = next(p for p in published["checkpoints"] if p["label"] == FIRST_POST_EXIT)
    assert len(first["census"]["entries"]) == 2
    settled = next(p for p in published["checkpoints"] if p["label"] == SETTLED)
    assert settled["census"]["entries"] == []


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param({"kill_actor": True}, id="kill-actor"),
        pytest.param({"job_query_shim": object()}, id="job-query-shim"),
        pytest.param(
            {"unconfirmed_close": R7Setup(None, False, 0)}, id="unconfirmed-close"
        ),
    ],
)
async def test_another_scenario_is_refused_before_anything_is_staged(
    monkeypatch, tmp_path, scenario
):
    staged = AsyncMock()
    monkeypatch.setattr(harness, "stage_signed_in_session", staged)
    monkeypatch.setattr(
        harness,
        "claim_account",
        lambda _: pytest.fail("the profile was claimed before the refusal"),
    )
    with pytest.raises(ValueError, match=ROW_H_R3):
        await harness.measure_host_quit_row(
            profile=tmp_path / "auth" / "profile",
            experiment="K3",
            daemon=True,
            egress=cast(Any, (SimpleNamespace(), SimpleNamespace())),
            log=EventLog(tmp_path / "evidence", run="refused"),
            work_dir=tmp_path / "row",
            row=ROW_H_R3,
            **scenario,
        )
    staged.assert_not_awaited()


async def test_a_forced_eof_cleanup_leaves_the_window_open_and_fails(scene):
    scene.exits = False
    result, _ = await scene.run()

    assert FIRST_POST_EXIT not in scene.asked
    assert any(
        f.startswith(ROW_H_R3) and "the checkpoints were" in f for f in result.failures
    ), result.failures
    assert any("not a normal EOF exit" in f for f in result.failures)


async def test_a_read_without_its_times_fails(scene):
    scene.timed = False
    result, _ = await scene.run()
    assert any(
        f.startswith(ROW_H_R3) and "times are missing or invalid" in f
        for f in result.failures
    ), result.failures


async def test_a_failed_script_fails_the_row(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
):
    scene = _Scene(row, monkeypatch, tmp_path, daemon=False)

    def association_fails(*args, **kwargs):
        raise RuntimeError("planted")

    monkeypatch.setattr(harness, "associate_server", association_fails)
    result, _ = await scene.run()

    assert BEFORE_QUIT not in scene.asked
    assert f"{ROW_H_R3}: the row's script failed: RuntimeError: planted" in (
        result.failures
    )


async def test_a_failed_post_exit_hook_fails_the_row(scene, monkeypatch):
    emit = EventLog.emit

    def failing(self, **fields):
        if fields.get("kind") == "host.checkpoint" and (
            fields.get("label") == FIRST_POST_EXIT
        ):
            raise OSError("planted: the event log is full")
        return emit(self, **fields)

    monkeypatch.setattr(EventLog, "emit", failing)
    result, _ = await scene.run()

    assert (
        f"{ROW_H_R3}: the post-exit hook failed: OSError: planted: the event log "
        f"is full" in result.failures
    ), result.failures
    # The row went on: settled was still read, and the teardown ran.
    assert scene.asked == list(CHECKPOINTS)


async def test_an_unsettled_checkpoint_blocks_every_later_measurement(
    scene, monkeypatch
):
    monkeypatch.setattr(harness, "_CHECKPOINT_SECONDS", 0.3)
    scene.hold[BEFORE_QUIT] = threading.Event()
    try:
        result, preserved = await scene.run()
    finally:
        scene.hold[BEFORE_QUIT].set()

    # Nothing else was read while the first reader still ran, and nothing
    # was launched on the profile.
    assert scene.asked == [BEFORE_QUIT]
    assert preserved == 0
    published = scene.published()["comparison"]
    errors = {p["label"]: p.get("error") or "" for p in published["checkpoints"]}
    assert "outlived its 0.3s bound" in errors[BEFORE_QUIT]
    assert "UnsettledWorker" in errors[FIRST_POST_EXIT]
    assert "UnsettledWorker" in errors[SETTLED]
    assert any("post-quit not run" in f for f in result.failures)
    deadline = time.monotonic() + 10
    while unconfirmed_close.running_workers() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def test_a_cancelled_row_preserves_nothing(scene):
    scene.hold[FIRST_POST_EXIT] = threading.Event()
    task = asyncio.ensure_future(scene.run())
    try:
        async with asyncio.timeout(10):
            while FIRST_POST_EXIT not in scene.asked:
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
    finally:
        scene.hold[FIRST_POST_EXIT].set()
    with pytest.raises(asyncio.CancelledError):
        await task
    scene.preservation.assert_not_awaited()
    assert SETTLED not in scene.asked


@pytest.mark.parametrize(
    ("row_id", "idle"),
    [
        pytest.param(ROW_H_R3, harness.COMPARISON_IDLE_TIMEOUT_SECONDS, id="H-R3"),
        pytest.param(harness.ROW_H_R1, harness.IDLE_TIMEOUT_SECONDS, id="H-R1"),
    ],
)
async def test_the_row_picks_its_idle_timeout_once_for_every_use(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
    row_id,
    idle,
):
    # A frozen runtime, so its staging environment is the row's too; the
    # owner never leaves, so the judgement names the bound it waited out.
    scene = _Scene(row, monkeypatch, tmp_path, daemon=True)
    environments: list[str] = []
    staged: list[str] = []
    waits: list[float] = []

    def actor_environment(*args, **kwargs):
        env = REAL_ACTOR_ENVIRONMENT(*args, **kwargs)
        environments.append(env[EnvironmentKeys.BROWSER_IDLE_TIMEOUT])
        return env

    def stage_frozen_session(runtime, directory, env):
        staged.append(env[EnvironmentKeys.BROWSER_IDLE_TIMEOUT])

    def wait_until_dead(process, seconds, **kwargs):
        waits.append(seconds)
        return False

    monkeypatch.setattr(harness, "actor_environment", actor_environment)
    monkeypatch.setattr(harness, "stage_frozen_session", stage_frozen_session)
    monkeypatch.setattr(harness, "wait_until_dead", wait_until_dead)
    monkeypatch.setattr(harness, "interpreter_failures", lambda *a, **k: [])
    frozen = Runtime(
        python=sys.executable,
        checkout=tmp_path / "baseline",
        browsers=tmp_path / "browsers",
        pinned="b" * 40,
    )
    result, _ = await scene.run(row=row_id, runtime=frozen)

    # The staging session and the row's actors.
    assert environments == [str(idle), str(idle)]
    assert staged == [str(idle)]
    assert waits == [idle + harness._OWNER_EXIT_SLACK_SECONDS]
    assert any(f"of its {idle}s idle timeout" in f for f in result.failures)
    if row_id == ROW_H_R3:
        assert result.comparison is not None
        assert result.comparison["idle_timeout_seconds"] == idle


# --- H-R2: a second host while the first is open ---------------------------------


def _read(host: str, call: str, began_ms: int, ended_ms: int) -> dict:
    return {
        "host": host,
        "call": call,
        "began": 1_000.0 + began_ms / 1000,
        "ended": 1_000.0 + ended_ms / 1000,
        "began_monotonic_ns": began_ms * MS,
        "ended_monotonic_ns": ended_ms * MS,
        "is_error": False,
        "read_the_post": True,
    }


def _request(at_ms: int | None, *, valid: bool = True, path: str = "/feed/") -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": valid,
        "t": 1_000.0,
        "monotonic_ns": at_ms * MS if at_ms is not None else None,
    }


def _host(eof_ms: int, exit_ms: int) -> dict:
    return {
        "error": None,
        "alive_before_quit": True,
        "stdin_closed": True,
        "exited_on_quit": True,
        "exit_code": 0,
        "killed_by_harness": False,
        "stderr_closed": True,
        "eof_ns": eof_ms * MS,
        "exit_seen_ns": exit_ms * MS,
    }


def _r2_record(*, daemon: bool = True, platform: str = "linux") -> dict:
    """A valid H-R2 record, times in seconds of the monotonic clock:

    A1 100-104, after A1 105-106, B started 107, B's read 112-114, B exits
    116, after B quit 116.1-117, A2 120-121, A exits 123, first post-exit
    123.1-124, the owner leaves at 185, settled 190-191, cleanup 192. Each
    daemon gap ends within the 55s the 60s idle timeout leaves.
    """
    actor = OWNER if daemon else SERVER
    launch = {"command": ["python", "-m", "linkedin_mcp_server"], "env_sha256": "e"}
    record: dict[str, Any] = {
        "row": ROW_H_R2,
        "mode": "daemon" if daemon else "direct",
        "platform": platform,
        "browser_key": KEY,
        "idle_timeout_seconds": 60.0,
        "k2": dict(K2_NOT_APPLICABLE),
        "actor": list(actor),
        "lock": list(LOCK),
        "observation_problems": [],
        "script_error": None,
        "after_exit_error": None,
        "launch": {"A": dict(launch), "B": dict(launch)},
        "a_open": {"at_b_start": True, "after_b_exit": True},
        "calls": [
            _read("A", "A1", 100_000, 104_000),
            _read("B", "B", 112_000, 114_000),
            _read("A", "A2", 120_000, 121_000),
        ],
        "requests": [_request(102_000), _request(113_000), _request(120_500)],
        "host": _host(122_000, 123_000),
        "host_b": {
            **_host(115_000, 116_000),
            "pid": 4343,
            "launched_ns": 107_000 * MS,
            "started_ns": 107_100 * MS,
            "after_exit_error": None,
            "forwarded": daemon,
        },
        "checkpoints": [
            _point(
                AFTER_A1,
                105_000,
                106_000,
                actor=actor,
                alive=True,
                occupied=True,
                lock="held",
            ),
            _point(
                AFTER_B_QUIT,
                116_100,
                117_000,
                actor=actor,
                alive=True,
                occupied=daemon,
                lock="held" if daemon else "free",
            ),
            _point(
                FIRST_POST_EXIT,
                123_100,
                124_000,
                actor=actor,
                alive=daemon,
                occupied=daemon,
                lock="held" if daemon else "free",
            ),
            _point(
                SETTLED,
                190_000,
                191_000,
                actor=actor,
                alive=False,
                occupied=False,
                lock="free",
            ),
        ],
        "owner_processes": [[*actor, 1]] if daemon else [],
        "owner_gates": [],
        "gate_processes": [],
        "evidence": {"distinct_roots": [[500, 110.0]], "handoff": "unobserved"},
        "cleanup_began_ns": 192_000 * MS,
    }
    if daemon:
        record["owners"] = [
            {"label": label, "lifetime": list(actor), "instance_id": "i-1"}
            for label in OWNER_READS
        ]
        record["owner_exit"] = {
            "how": "exited",
            "seen_ns": 185_000 * MS,
            "seconds_after_quit": 62.0,
        }
    else:
        record["b_settlement"] = {
            "remaining": [],
            "unresolved": [],
            "ended_ns": 118_000 * MS,
        }
        record["lock_before_a2"] = {
            "now": list(LOCK),
            "answer": {"state": "free", "reason": "", "device": 7, "inode": 99},
        }
    return record


def _shift(record: Any, *, since_ms: int, by_ms: int) -> None:
    """Move every monotonic reading at or after *since_ms* by *by_ms*."""
    if isinstance(record, dict):
        for name, value in record.items():
            if name.endswith("_ns") and type(value) is int and value >= since_ms * MS:
                record[name] = value + by_ms * MS
            else:
                _shift(value, since_ms=since_ms, by_ms=by_ms)
    elif isinstance(record, list):
        for value in record:
            _shift(value, since_ms=since_ms, by_ms=by_ms)


def _r2_call(record: dict, name: str) -> dict:
    return next(c for c in record["calls"] if c["call"] == name)


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("daemon", [True, False], ids=["daemon", "direct"])
def test_a_valid_second_host_record_passes(daemon, platform):
    record = _r2_record(daemon=daemon, platform=platform)
    assert r2_problems(record, daemon=daemon) == []
    assert problems_for(record, daemon=daemon) == []


def test_a_venv_launcher_and_the_interpreter_it_starts_are_one_launch():
    # On Windows a venv's python.exe starts the interpreter with the same
    # command line: two owner processes, one launch.
    record = _r2_record(daemon=True, platform="win32")
    launcher = [4000, 99.0, 1, "o", None, 99.01, 150.0]
    record["owner_processes"] = [launcher, [*OWNER, 4000, "o", None, 100.01, 150.0]]
    assert r2_problems(record, daemon=True) == []
    record["owner_processes"] = [launcher, [*OWNER, 1, "o", None, 100.01, 150.0]]
    assert any("the row started owners" in p for p in r2_problems(record, daemon=True))


def test_a_windows_release_gate_and_its_launcher_are_one_start_attempt():
    # Measured on Windows (run 36677572983): the venv's python.exe started the
    # gate's interpreter with the same command line and nonce, pid 1104 under
    # 1776. One attempt, not an extra one.
    record = _r2_record(daemon=True, platform="win32")
    record["gate_processes"] = [
        [1776, 90.0, 3100, "g", None, 90.01, 150.0],
        [1104, 90.0, 1776, "g", None, 90.01, 150.0],
    ]
    assert r2_problems(record, daemon=True) == []
    record["gate_processes"] = [
        [1776, 90.0, 3100, "g", None, 90.01, 150.0],
        [1104, 90.0, 3100, "g", None, 90.01, 150.0],
    ]
    assert any(
        "an extra owner start was attempted" in p
        for p in r2_problems(record, daemon=True)
    )


def test_release_gates_that_were_never_recorded_fail():
    record = _r2_record(daemon=True, platform="win32")
    del record["gate_processes"]
    assert "the row's release gates were not recorded" in r2_problems(
        record, daemon=True
    )


def _gone(name: str) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        record["calls"] = [c for c in record["calls"] if c["call"] != name]

    return change


def _requests(*at_ms: int | None) -> Callable[[dict], None]:
    return _set("requests", [_request(at) for at in at_ms])


def _owner(label: str, **fields: Any) -> Callable[[dict], None]:
    def change(record: dict) -> None:
        for seen in record["owners"]:
            if seen["label"] == label:
                seen.update(fields)

    return change


def _r2_second_root(record: dict) -> None:
    _at(record, AFTER_B_QUIT)["census"]["entries"].append(_entry(600, 1, 120.0))


R2_DAEMON_CASES = [
    # Clocks and gaps.
    pytest.param(
        # A1 sent at 50s and back at 104s: counted from its receipt, B's read
        # would be 10s later; from its send it is 64s, past the owner's idle.
        [_set("calls/0/began_monotonic_ns", 50_000 * MS)],
        "hot reuse not established: B's read: the window is late",
        id="receipt-would-hide-the-b-gap",
    ),
    pytest.param(
        [lambda r: _shift(r, since_ms=120_000, by_ms=50_000)],
        "hot reuse not established: A2's read: the window is late",
        id="late-a2-gap",
    ),
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/ended_ns", 173_000 * MS)],
        f"{AFTER_B_QUIT}: the window is late",
        id="late-after-b-quit",
    ),
    pytest.param(
        [_set("calls/1/began_monotonic_ns", None)],
        "B: the call's interval is missing or reversed",
        id="b-read-untimed",
    ),
    pytest.param(
        [_set("calls/1/began_monotonic_ns", 103_000 * MS)],
        "A1 and B: the call intervals overlap",
        id="overlapping-reads",
    ),
    pytest.param(
        [_set(f"{AFTER_A1}/ended_ns", 107_500 * MS)],
        f"{AFTER_A1}: it is not shown to end before B started",
        id="after-a1-overlaps-b",
    ),
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/began_ns", 115_500 * MS)],
        f"{AFTER_B_QUIT}: it began before B's exit was seen",
        id="after-b-quit-before-b-exit",
    ),
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/ended_ns", 120_500 * MS)],
        f"{AFTER_B_QUIT}: it ended after A2 was sent",
        id="after-b-quit-overlaps-a2",
    ),
    pytest.param(
        [_set("host_b/exit_seen_ns", 122_500 * MS)],
        "host B's exit is not shown before host A's EOF",
        id="b-outlives-a",
    ),
    # Nesting and launch.
    pytest.param(
        [_set("a_open/after_b_exit", False)],
        "host A was not shown open after b exit",
        id="a-closed-during-b",
    ),
    pytest.param(
        [_set("a_open/at_b_start", None)],
        "host A was not shown open at b start",
        id="a-unread-at-b-start",
    ),
    pytest.param(
        [_set("launch/B/env_sha256", "other")],
        "host B did not start as host A did",
        id="b-another-environment",
    ),
    pytest.param(
        [_set("launch/B/command", ["python", "-m", "other"])],
        "host B did not start as host A did",
        id="b-another-command",
    ),
    # Host B's own session.
    pytest.param(
        [_set("host_b", {}), _gone("B")],
        "host B: the host's quit was not a normal EOF exit",
        id="no-host-b",
    ),
    pytest.param(
        [_set("host_b/error", "McpError: initialize timed out"), _gone("B")],
        "host B: the host session failed: McpError",
        id="b-initialization-failed",
    ),
    pytest.param(
        [_set("host_b/exit_code", 1)],
        "host B: the host's quit was not a normal EOF exit",
        id="b-nonzero-exit",
    ),
    pytest.param(
        [_set("host_b/killed_by_harness", True)],
        "host B: the host's quit was not a normal EOF exit",
        id="b-forced-eof-cleanup",
    ),
    pytest.param(
        [_set("host_b/after_exit_error", "OSError: planted")],
        "host B's post-exit hook failed",
        id="b-hook-error",
    ),
    pytest.param(
        [_set("host_b/forwarded", False)],
        "host B did not report forwarding to the shared owner",
        id="b-did-not-forward",
    ),
    # The reads.
    pytest.param([_gone("B")], "the calls were", id="missing-b-read"),
    pytest.param([_gone("A2")], "the calls were", id="missing-a2"),
    pytest.param(
        [_set("calls/2/read_the_post", False)],
        "A2 did not return the synthetic post",
        id="a2-failed",
    ),
    pytest.param(
        [_set("calls/1/is_error", True)],
        "B did not return the synthetic post",
        id="b-read-failed",
    ),
    pytest.param(
        [_set("script_error", "RuntimeError: planted")],
        "the row's script failed",
        id="script-error",
    ),
    pytest.param(
        [_set("after_exit_error", "RuntimeError: planted")],
        "the post-exit hook failed",
        id="a-hook-error",
    ),
    # Request attribution.
    pytest.param(
        # B's only request arrived while A1 ran: A1's, never B's.
        [_requests(102_000, 103_000, 120_500)],
        "B: no session-carrying feed request arrived inside its own interval",
        id="b-cannot-borrow-a1s-request",
    ),
    pytest.param(
        [_requests(102_000, 113_000)],
        "A2: no session-carrying feed request arrived inside its own interval",
        id="missing-origin-request",
    ),
    pytest.param(
        [
            _set(
                "requests",
                [_request(102_000), _request(113_000, valid=False), _request(120_500)],
            )
        ],
        "B: no session-carrying feed request",
        id="b-request-without-the-session",
    ),
    pytest.param(
        [_requests(102_000, None, 120_500)],
        "B: no session-carrying feed request",
        id="b-request-unplaced",
    ),
    # Checkpoints.
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/actor_alive", [False, False])],
        f"{AFTER_B_QUIT}: the owner was gone, not alive",
        id="owner-lost-while-b-ran",
    ),
    pytest.param(
        [_r2_second_root],
        f"{AFTER_B_QUIT}: 2 browser roots on the profile, not one",
        id="a-second-root-after-b",
    ),
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/census/entries/0/start", 150.0)],
        f"{AFTER_B_QUIT}: the root is not the one {AFTER_A1} read",
        id="another-root-after-b",
    ),
    pytest.param(
        [_set(f"{AFTER_A1}/census/unresolved", [777])],
        f"{AFTER_A1}: unknown browser roots on the profile, not one",
        id="unresolved-census-after-a1",
    ),
    pytest.param(
        [_set(f"{AFTER_B_QUIT}/lock/now", [7, 100])],
        f"{AFTER_B_QUIT}: the lock was replaced, not held",
        id="lock-replaced-after-b",
    ),
    pytest.param(
        [_set(f"{AFTER_A1}/lock/answer/state", "unknown")],
        f"{AFTER_A1}: the lock was unknown, not held",
        id="contender-failed-after-a1",
    ),
    pytest.param(
        [_set(f"{SETTLED}/lock/answer/state", "unknown")],
        f"{SETTLED}: the lock was unknown, not free",
        id="unknown-is-not-free",
    ),
    pytest.param(
        [_set("cleanup_began_ns", 190_500 * MS)],
        f"{SETTLED}: it is not shown to precede the cleanup",
        id="settlement-after-cleanup",
    ),
    pytest.param([_drop(AFTER_B_QUIT)], "the checkpoints were", id="missing-window"),
    # The owner and the election.
    pytest.param(
        [_owner("after B", lifetime=[OWNER[0], 150.0])],
        "after B: the descriptor names",
        id="another-owner-lifetime-after-b",
    ),
    pytest.param(
        [_owner("after A2", instance_id="i-2")],
        "after A2: the owner's instance changed",
        id="instance-changed",
    ),
    pytest.param(
        [lambda r: r["owners"].pop(1)],
        "the owner was not read after B",
        id="owner-unread-after-b",
    ),
    pytest.param(
        [_owner("after A1", instance_id=None)],
        "the owner's instance was not read after A1",
        id="no-instance-after-a1",
    ),
    pytest.param(
        [_set("owner_processes", [[*OWNER, 1], [9999, 130.0, 1]])],
        "the row started owners",
        id="a-second-owner-elected",
    ),
    pytest.param(
        [_set("gate_processes", [[77, 90.0, 1], [78, 91.0, 1]])],
        "an extra owner start was attempted",
        id="extra-release-gate",
    ),
    pytest.param(
        [_set("owner_exit/how", "still running")],
        "the owner was not seen to exit by itself",
        id="owner-never-left",
    ),
]


@pytest.mark.parametrize(("changes", "reported"), R2_DAEMON_CASES)
def test_one_changed_observation_fails_the_daemon_second_host(changes, reported):
    record = _r2_record(daemon=True)
    for change in changes:
        change(record)
    problems = r2_problems(record, daemon=True)
    assert any(reported in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        pytest.param(
            [_set("host_b/forwarded", True)],
            "host B reached a shared owner in Direct mode",
            id="b-forwarded-in-direct",
        ),
        pytest.param(
            [_set("b_settlement/remaining", [900])],
            "B's browser was not shown gone before A2",
            id="b-browser-left",
        ),
        pytest.param(
            [_set("b_settlement/unresolved", [901])],
            "B's browser was not shown gone before A2",
            id="b-census-incomplete",
        ),
        pytest.param(
            [_set("b_settlement/ended_ns", 120_500 * MS)],
            "B's settlement is not shown to precede A2",
            id="b-settled-after-a2",
        ),
        pytest.param(
            [_set("b_settlement", {"error": "UnsettledWorker: planted"})],
            "B's browser was not shown gone before A2",
            id="b-settlement-failed",
        ),
        pytest.param(
            [_set("lock_before_a2/answer/state", "held")],
            "before A2: the lock was held, not free",
            id="lock-held-before-a2",
        ),
        pytest.param(
            [_set("lock_before_a2/now", [7, 100])],
            "before A2: the lock was replaced, not free",
            id="lock-replaced-before-a2",
        ),
        pytest.param(
            [_set("lock_before_a2", None)],
            "before A2: the lock was unknown, not free",
            id="lock-unread-before-a2",
        ),
        pytest.param(
            [_set("calls/1/began_monotonic_ns", 106_500 * MS)],
            "B's read is not shown after host B started",
            id="b-read-before-b-started",
        ),
        pytest.param(
            [_set(f"{AFTER_B_QUIT}/actor_alive", [False, False])],
            f"{AFTER_B_QUIT}: the server of host A was gone",
            id="a-server-gone-after-b",
        ),
        pytest.param(
            [_set(f"{AFTER_B_QUIT}/lock/answer/state", "unknown")],
            f"{AFTER_B_QUIT}: the lock was unknown",
            id="contender-failed-after-b",
        ),
        pytest.param(
            [_set(f"{AFTER_A1}/lock/association/state", "not the holder")],
            f"{AFTER_A1}: the holder is not the actor, not the server",
            id="linux-holder-after-a1",
        ),
    ],
)
def test_one_changed_observation_fails_the_direct_second_host(changes, reported):
    record = _r2_record(daemon=False)
    for change in changes:
        change(record)
    problems = r2_problems(record, daemon=False)
    assert any(reported in problem for problem in problems), problems


def test_a_direct_reading_after_b_quit_is_kept_as_read():
    # B's browser still on the profile when B's own hook read it, and gone by
    # the bounded passive wait before A2: the record keeps both.
    record = _r2_record(daemon=False)
    _at(record, AFTER_B_QUIT).update(
        _point(
            AFTER_B_QUIT,
            116_100,
            117_000,
            actor=SERVER,
            alive=True,
            occupied=True,
            lock="held",
            root=(800, 113.0),
        )
    )
    assert r2_problems(record, daemon=False) == []
    assert semantics(record)["checkpoints"][AFTER_B_QUIT]["census"] == "occupied"


def test_a_late_gap_is_evidence_never_a_second_owner_finding():
    # The owner idled out while B started, so B elected its own: the row
    # reports that the reuse was not observed, and judges no owner change.
    record = _r2_record(daemon=True)
    _shift(record, since_ms=107_000, by_ms=60_000)
    _owner("after B", lifetime=[9999, 130.0], instance_id="i-2")(record)
    _owner("after A2", lifetime=[9999, 130.0], instance_id="i-2")(record)
    record["owner_processes"] = [[*OWNER, 1], [9999, 130.0, 1]]
    problems = r2_problems(record, daemon=True)
    assert any("hot reuse not established: B's read" in p for p in problems)
    assert not [
        p
        for p in problems
        if "descriptor names" in p
        or "instance changed" in p
        or "started owners" in p
        or p.startswith(f"{AFTER_B_QUIT}: the owner")
    ], problems


def test_requests_outside_every_read_are_diagnostics_and_extra_ones_count_once():
    record = _r2_record(daemon=True)
    record["requests"] += [
        _request(110_000),
        _request(113_200),
        _request(113_900),
        _request(119_000, path="/feed/?refresh=1"),
        _request(113_500, path="/voyager/api"),
    ]
    assert r2_problems(record, daemon=True) == []
    found = attribute_requests(record["calls"], record["requests"])
    assert found.claimed == {"A1": 1, "B": 3, "A2": 1}
    assert found.outside == 2
    assert semantics(record)["read"] == semantics(_r2_record())["read"]


def test_a_request_two_reads_could_claim_settles_neither():
    calls = [
        _read("A", "A1", 100_000, 104_000),
        _read("B", "B", 104_000, 108_000),
    ]
    found = attribute_requests(calls, [_request(104_000), _request(106_000)])
    assert found.contested
    assert any("claimed by two calls" in p for p in found.problems)
    assert any(p.startswith("A1: no session-carrying") for p in found.problems)


def test_k0_of_the_second_host_reports_a_different_reading_after_b_quit():
    # Two valid Direct runs: B's browser gone when its own hook read, or not
    # yet gone and settled before A2.
    empty, lingering = _r2_record(daemon=False), _r2_record(daemon=False)
    _at(lingering, AFTER_B_QUIT).update(
        _point(
            AFTER_B_QUIT,
            116_100,
            117_000,
            actor=SERVER,
            alive=True,
            occupied=True,
            lock="held",
            root=(800, 113.0),
        )
    )
    differences = semantic_differences(empty, lingering, daemon=False)
    assert len(differences) == 1 and differences[0].startswith(AFTER_B_QUIT)


def test_the_evidence_is_derived_and_never_judged():
    events = [
        {"kind": "process.start", "pid": 500, "start_identity": 110.0},
        {"kind": "browser.roots", "roots": {KEY: [500]}},
        {"kind": "browser.roots", "roots": {}},
        {"kind": "process.start", "pid": 600, "start_identity": 130.0},
        {"kind": "browser.roots", "roots": {KEY: [600], "/elsewhere": [700]}},
    ]
    assert distinct_roots(events, KEY) == [[500, 110.0], [600, 130.0]]
    assert handoff_reading(["x", f"{_HANDED_OVER} (held 20.1s)"]) == "handed over"
    assert handoff_reading(["Closing idle browser after 60s"]) == "idle release"
    assert handoff_reading([]) == "unobserved"
    record = _r2_record(daemon=False)
    record["evidence"] = {"distinct_roots": [], "handoff": "anything"}
    assert r2_problems(record, daemon=False) == []


@pytest.mark.parametrize("daemon", [True, False], ids=["daemon", "direct"])
def test_the_second_host_verdict_survives_the_published_packet(daemon):
    good = json.loads(json.dumps(_r2_record(daemon=daemon)))
    assert problems_for(good, daemon=daemon) == []
    bad = _r2_record(daemon=daemon)
    _gone("A2")(bad)
    assert problems_for(json.loads(json.dumps(bad)), daemon=daemon)


def test_k0_of_the_second_host_compares_classifications_only():
    other = _r2_record(daemon=True)
    _shift(other, since_ms=0, by_ms=500_000)
    other["actor"] = [9876, 300.0]
    other["owner_processes"] = [[9876, 300.0, 1]]
    for seen in other["owners"]:
        seen.update(lifetime=[9876, 300.0], instance_id="i-9")
    for point in other["checkpoints"]:
        point["lifetime"] = [9876, 300.0]
        for lineage in point["lineages"]:
            lineage["ancestors"][-1] = [9876, 300.0]
        association = point["lock"]["association"]
        association["holder"] = [9876, 300.0]
    assert r2_problems(other, daemon=True) == []
    assert semantic_differences(_r2_record(), other, daemon=True) == []
    changed = _r2_record(daemon=True)
    changed["a_open"]["after_b_exit"] = False
    assert semantic_differences(_r2_record(), changed, daemon=True)
    assert semantic_differences(None, _r2_record(), daemon=True)
    assert comparison_refusals(_r2_record(daemon=False), None)
    assert comparison_refusals(_r2_record(daemon=False), _r2_record()) == []


# --- H-R2 wiring: host A's script runs host B through the same seam ---------------


class _Alive:
    """A process handle that reads as running until marked gone."""

    def __init__(self, pid: int):
        self.pid, self.gone = pid, False

    def status(self) -> str:
        if self.gone:
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_RUNNING


class _SecondHostScene(_Scene):
    """H-R2 through the real row: host A's double runs the row's own script,
    whose host B goes through the same ``run_host_session`` double. Every
    read adds a feed request to the origin while it runs, with its arrival
    on the monotonic clock, as the real origin records it."""

    def __init__(self, modelled_row, monkeypatch, tmp_path, *, daemon: bool):
        super().__init__(modelled_row, monkeypatch, tmp_path, daemon=daemon)
        self.readings[AFTER_A1] = self.readings[BEFORE_QUIT]
        self.readings[AFTER_B_QUIT] = lambda: self.reading(
            alive=True, occupied=daemon, lock="held" if daemon else "free"
        )
        self.b: dict[str, Any] = {
            "exit_code": 0,
            "error": None,
            "alive_before_quit": True,
            "stdin_closed": True,
            "stdin_close_error": None,
            # The stub's bounded cleanup returned with B's process unreaped.
            "pending": False,
        }
        #: Host B's server as the transport hands it out: settled once it
        #: has a return code.
        self.b_process = SimpleNamespace(pid=4343, returncode=None)
        #: Held open while set, so a cancellation lands while B runs.
        self.b_active: asyncio.Event | None = None
        self.b_running = asyncio.Event()
        #: What the census after B reads, when set.
        self.census_after_b: Callable[[], Any] | None = None
        self.lock_before_a2 = "free"
        self.a2_fails = False
        self.a2_called = 0
        self.launched: list[dict] = []
        self.origin: Any = None
        self.handles = {4242: _Alive(4242), 4343: _Alive(4343)}
        inner = harness.run_host_session
        scene = self
        real_row = gate.measure_host_quit_row
        real_census = harness.profile_census

        def census(*args, **kwargs):
            # Only while host B's settlement is read: from B's start to A's
            # own exit, never the staging before or the row's end after.
            if (
                scene.census_after_b is not None
                and scene.b_running.is_set()
                and FIRST_POST_EXIT not in scene.asked
            ):
                return scene.census_after_b()
            return real_census(*args, **kwargs)

        monkeypatch.setattr(harness, "profile_census", census)
        monkeypatch.setattr(
            harness,
            "read_lock",
            lambda path: {
                "now": list(LOCK),
                "answer": {
                    "state": scene.lock_before_a2,
                    "reason": "",
                    "device": LOCK[0],
                    "inode": LOCK[1],
                },
            },
        )

        async def capture(**kwargs):
            scene.origin = kwargs["egress"][0]
            return await real_row(**kwargs)

        monkeypatch.setattr(gate, "measure_host_quit_row", capture)
        monkeypatch.setattr(
            harness,
            "associate_server",
            lambda pid, observed, **kw: (
                (scene.handles.get(pid), 7.0) if pid in scene.handles else (None, None)
            ),
        )

        def timed(host: str) -> dict:
            began, began_ns = time.time(), time.monotonic_ns()
            scene.origin.requests.append(
                OriginRequest(
                    "www.linkedin.com",
                    "www.linkedin.com",
                    "/feed/",
                    ("li_at",),
                    t=time.time(),
                    session_valid=True,
                    monotonic_ns=time.monotonic_ns(),
                )
            )
            return {
                "tool": harness.READ_TOOL,
                "began": began,
                "ended": time.time(),
                "began_monotonic_ns": began_ns,
                "ended_monotonic_ns": time.monotonic_ns(),
                "is_error": False,
                "read_the_post": True,
                "text": f"read by {host}",
            }

        async def host(command, *, env, cwd, on_stderr, **kwargs):
            after_call = kwargs.get("after_call")
            script, after_exit = kwargs.get("script"), kwargs.get("after_exit")
            scene.launched.append({"command": list(command), "env": dict(env)})
            if after_call is None:
                # Host B: its own session, from its own directory.
                assert Path(cwd).name == "host-b"
                on_process = kwargs.get("on_process")
                if on_process is not None:
                    on_process(scene.b_process)
                kwargs["started"](4343)
                session = harness.HostSession(pid=4343)
                scene.b_running.set()
                if scene.b_active is not None:
                    await scene.b_active.wait()
                if scene.b["error"] is not None:
                    # The stub's cleanup killed and reaped it.
                    session.error = scene.b["error"]
                    scene.b_process.returncode = -9
                    return session
                lines = [harness._FORWARDING_LINE] if daemon else []
                for line in lines:
                    on_stderr(line)
                session.stderr = lines
                session.tool = timed("B")
            else:
                base = await inner(
                    command,
                    env=env,
                    cwd=cwd,
                    on_stderr=on_stderr,
                    **{
                        name: value
                        for name, value in kwargs.items()
                        if name not in ("script", "after_exit")
                    },
                )
                session = dataclasses.replace(
                    base,
                    pid=4242,
                    stderr=list(base.stderr) if daemon else [],
                    user_lines=[],
                    tool=timed("A"),
                )
                if script is not None:

                    async def call(name, arguments):
                        scene.a2_called += 1
                        if scene.a2_fails:
                            raise RuntimeError("planted: A2 was never answered")
                        summary = timed("A")
                        session.scripted.append(summary)
                        return summary

                    try:
                        await script(call)
                    except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                        session.script_error = f"{type(exc).__name__}: {exc}"
            session.alive_before_quit = session.stdin_closed = True
            session.eof_monotonic_ns = time.monotonic_ns()
            session.exited_on_quit = True
            session.exit_code = scene.b["exit_code"] if after_call is None else 0
            session.exit_seen_monotonic_ns = time.monotonic_ns()
            if after_call is None:
                session.alive_before_quit = scene.b["alive_before_quit"]
                session.stdin_closed = scene.b["stdin_closed"]
                session.stdin_close_error = scene.b["stdin_close_error"]
                if scene.b["pending"]:
                    # Not seen to exit: the stub killed it, and its bounded
                    # wait ended with no return code. No hook runs.
                    session.exited_on_quit = False
                    session.killed_by_harness = True
                    session.exit_code = None
                    session.exit_seen_monotonic_ns = None
                    return session
                scene.b_process.returncode = session.exit_code
            else:
                scene.handles[4242].gone = True
            if after_exit is not None:
                try:
                    await after_exit()
                except Exception as exc:  # noqa: BLE001 - as the real transport keeps it
                    session.after_exit_error = f"{type(exc).__name__}: {exc}"
            return session

        monkeypatch.setattr(harness, "run_host_session", host)

    async def run(self, **options):
        observed = (
            [
                {
                    "kind": "process.start",
                    "actor": "owner",
                    "in_row": True,
                    "pid": 42,
                    "ppid": 1,
                    "start_identity": 1.0,
                    "cmdline": ["python", "-m", "linkedin_mcp_server.daemon_owner"],
                }
            ]
            if self.daemon
            else []
        )
        return await super().run(row=ROW_H_R2, observed=observed, **options)


@pytest.fixture(params=[True, False], ids=["daemon", "direct"])
def second(request, row, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    return _SecondHostScene(row, monkeypatch, tmp_path, daemon=request.param)


async def test_a_healthy_second_host_row_passes_and_publishes_one_record(second):
    result, preserved = await second.run()

    assert result.failures == [], result.failures
    assert preserved == 1
    assert second.asked == [AFTER_A1, AFTER_B_QUIT, FIRST_POST_EXIT, SETTLED]
    published = second.published()["comparison"]
    assert [(c["host"], c["call"]) for c in published["calls"]] == list(R2_CALLS)
    assert published["host_b"]["exit_code"] == 0
    assert published["launch"]["A"] == published["launch"]["B"]
    assert published["a_open"] == {"at_b_start": True, "after_b_exit": True}
    assert all(r["monotonic_ns"] is not None for r in published["requests"][-3:])
    assert r2_problems(published, daemon=second.daemon) == []
    # Both hosts from one command and environment; only B's directory differs.
    a, b = second.launched
    assert a == b
    hosts = {
        r.get("host")
        for r in second.row.log.records()
        if r["kind"] == "user.output" and r["actor"] == "frontend"
    }
    assert "B" in hosts if second.daemon else hosts <= {"A", "B"}


async def test_a_failed_host_b_fails_the_row_and_a2_is_not_taken(second):
    second.b["error"] = "McpError: initialize timed out"
    result, preserved = await second.run()
    assert any("host B: the host session failed" in f for f in result.failures)
    assert any("the calls were" in f for f in result.failures)
    assert second.a2_called == 0 and preserved == 0


async def test_a_nonzero_host_b_exit_fails_the_row(second):
    second.b["exit_code"] = 3
    result, _ = await second.run()
    assert any(
        "host B: the host's quit was not a normal EOF exit" in f
        for f in result.failures
    ), result.failures


async def test_a_failed_a2_fails_the_row(second):
    second.a2_fails = True
    result, _ = await second.run()
    assert any("the row's script failed" in f for f in result.failures)
    assert any("the calls were" in f for f in result.failures)


async def test_an_unsettled_worker_blocks_host_b_and_a2(second, monkeypatch):
    monkeypatch.setattr(harness, "_CHECKPOINT_SECONDS", 0.3)
    second.hold[AFTER_A1] = threading.Event()
    try:
        result, preserved = await second.run()
    finally:
        second.hold[AFTER_A1].set()
    # Neither B nor A2 started while the first reader still ran.
    assert len(second.launched) == 1
    assert AFTER_B_QUIT not in second.asked
    assert preserved == 0
    assert any("host B was not taken" in f for f in result.failures), result.failures
    deadline = time.monotonic() + 10
    while unconfirmed_close.running_workers() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


async def test_the_second_host_row_refuses_another_scenario(monkeypatch, tmp_path):
    staged = AsyncMock()
    monkeypatch.setattr(harness, "stage_signed_in_session", staged)
    with pytest.raises(ValueError, match=ROW_H_R2):
        await harness.measure_host_quit_row(
            profile=tmp_path / "auth" / "profile",
            experiment="K3",
            daemon=True,
            egress=cast(Any, (SimpleNamespace(), SimpleNamespace())),
            log=EventLog(tmp_path / "evidence", run="refused"),
            work_dir=tmp_path / "row",
            row=ROW_H_R2,
            kill_actor=True,
        )
    staged.assert_not_awaited()


async def test_the_second_host_row_uses_the_comparison_idle_timeout(
    second, monkeypatch
):
    environments: list[str] = []

    def actor_environment(*args, **kwargs):
        env = REAL_ACTOR_ENVIRONMENT(*args, **kwargs)
        environments.append(env[EnvironmentKeys.BROWSER_IDLE_TIMEOUT])
        return env

    monkeypatch.setattr(harness, "actor_environment", actor_environment)
    result, _ = await second.run()
    idle = str(harness.COMPARISON_IDLE_TIMEOUT_SECONDS)
    assert environments == [idle]
    assert {
        launch["env"][EnvironmentKeys.BROWSER_IDLE_TIMEOUT]
        for launch in second.launched
    } == {idle}
    assert result.comparison is not None
    assert result.comparison["idle_timeout_seconds"] == float(idle)


async def test_an_unsettled_reader_after_b_blocks_a2(second, monkeypatch):
    monkeypatch.setattr(harness, "_CHECKPOINT_SECONDS", 0.3)
    second.hold[AFTER_B_QUIT] = threading.Event()
    try:
        result, preserved = await second.run()
    finally:
        second.hold[AFTER_B_QUIT].set()
    assert second.a2_called == 0
    assert preserved == 0
    assert any("A2 was not taken" in f for f in result.failures), result.failures
    deadline = time.monotonic() + 10
    while unconfirmed_close.running_workers() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def test_the_origin_dates_each_request_on_the_harness_monotonic_clock():
    # The handler itself, without a socket: what it records for one GET.
    recorded: list[OriginRequest] = []
    handler = object.__new__(synthetic_origin._OriginHandler)
    handler.server = cast(
        Any,
        SimpleNamespace(record=recorded.append, judge_session=lambda header: True),
    )
    handler.connection = SimpleNamespace(_synthetic_server_name="www.linkedin.com")
    handler.headers = cast(Any, {"Host": "www.linkedin.com", "Cookie": "li_at=x"})
    handler.path = "/feed/"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /feed/ HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 1)
    handler.wfile = io.BytesIO()
    before = time.monotonic_ns()
    handler.do_GET()
    after = time.monotonic_ns()
    [request] = recorded
    assert request.monotonic_ns is not None
    assert before <= request.monotonic_ns <= after
    assert request.t > 0 and request.session_valid is True


# --- Repairs after review e1fo -------------------------------------------------
#
# E1FO-01: a normal quit needs the server alive when the host quit and its
# stdin closed. E1FO-02: only the measured launcher construction is one
# launch. E1FO-03: host B's settlement gates A2 and the preservation, and an
# unsettled B stays owned. E1FO-04: every required checkpoint needs a complete
# census.


_BAD_QUITS = [
    pytest.param({"alive_before_quit": False}, "not shown alive", id="gone-before-eof"),
    pytest.param(
        {"stdin_closed": False, "stdin_close_error": "BrokenPipeError: planted"},
        "stdin was not shown closed",
        id="stdin-close-failed",
    ),
    pytest.param({"alive_before_quit": None}, "not shown alive", id="alive-unread"),
    pytest.param(
        {"stdin_closed": None}, "stdin was not shown closed", id="stdin-unread"
    ),
]


@pytest.mark.parametrize(("flags", "reported"), _BAD_QUITS)
def test_a_quit_that_did_not_reach_a_live_server_fails_the_saved_records(
    flags, reported
):
    # Host B in H-R2, host A in both rows, read as the packet is read.
    for record, where in (
        (_r2_record(daemon=True), "host_b"),
        (_r2_record(daemon=False), "host"),
        (_record(daemon=True), "host"),
    ):
        daemon = record["mode"] == "daemon"
        record[where].update(flags)
        saved = json.loads(json.dumps(record))
        assert any(reported in p for p in problems_for(saved, daemon=daemon))
        reference = (
            _r2_record(daemon=daemon)
            if record["row"] == ROW_H_R2
            else _record(daemon=daemon)
        )
        assert semantic_differences(reference, saved, daemon=daemon)
    missing = _r2_record(daemon=True)
    del missing["host_b"]["stdin_closed"]
    assert any(
        "host B: the server's stdin was not shown closed" in p
        for p in problems_for(missing, daemon=True)
    )


@pytest.mark.parametrize(("flags", "reported"), _BAD_QUITS)
async def test_host_b_without_a_normal_quit_fails_the_row_and_takes_no_a2(
    second, flags, reported
):
    second.b.update(flags)
    result, preserved = await second.run()
    assert any("host B: " in f and reported in f for f in result.failures)
    assert any("A2 was not taken" in f for f in result.failures), result.failures
    assert second.a2_called == 0 and preserved == 0


@pytest.mark.parametrize(("flags", "reported"), _BAD_QUITS)
async def test_host_a_without_a_normal_quit_fails_the_comparison(
    scene, flags, reported
):
    scene.flags = flags
    result, _ = await scene.run()
    assert any(f.startswith(ROW_H_R3) and reported in f for f in result.failures), (
        result.failures
    )


def _gate_command(nonce: str) -> list[str]:
    return [
        "C:\\venv\\Scripts\\python.exe",
        "-I",
        "-S",
        "-u",
        str(harness.gate_script(harness.REPO_ROOT)),
        nonce * 64,
        "--",
        "C:\\venv\\Scripts\\python.exe",
        "-m",
        "linkedin_mcp_server.daemon_owner",
    ]


def _owner_command(job: str) -> list[str]:
    return [
        "C:\\venv\\Scripts\\python.exe",
        "-P",
        "-m",
        "linkedin_mcp_server.daemon_owner",
        "--job-name",
        job,
    ]


def _started(pid: int, ppid: int, start: float, cmdline: list[str]) -> dict:
    return {
        "kind": "process.start",
        "in_row": True,
        "pid": pid,
        "ppid": ppid,
        "start_identity": start,
        "cmdline": cmdline,
        "t": start + 0.01,
    }


def _exited(pid: int, start: float, t: float) -> dict:
    return {"kind": "process.exit", "pid": pid, "start_identity": start, "t": t}


#: What the watcher recorded of one Windows owner start, as in run
#: 36679881422's K3: a venv launcher and the interpreter it started with the
#: same command, for the gate and for the owner, each launcher outliving its
#: child's start.
_ONE_START = [
    _started(2172, 2836, 90.0, _gate_command("a")),
    _started(8316, 2172, 90.005, _gate_command("a")),
    _started(1312, 8316, 99.99, _owner_command("job-1")),
    _started(OWNER[0], 1312, OWNER[1], _owner_command("job-1")),
    _exited(2172, 90.0, 170.0),
    _exited(8316, 90.0, 170.0),
]


#: A watcher's ``sample_log`` of 10 ms samples every 50 ms from 85 s to 180 s.
_SAMPLES = [[end / 100 - 0.01, end / 100] for end in range(8500, 18000, 5)]


def _judged_launches(
    events: list[dict], platform: str = "win32", samples: list | None = None
) -> list[str]:
    """The owner and gate problems of a valid daemon H-R2 record, Windows by
    default, whose launch lifetimes the real producer read from *events* and
    the watcher's *samples*."""
    owners, gates = harness.launch_lifetimes(
        events,
        [harness.gate_script(harness.REPO_ROOT)],
        _SAMPLES if samples is None else samples,
    )
    record = _r2_record(daemon=True, platform=platform)
    record["owner_processes"], record["gate_processes"] = owners, gates
    return [
        p
        for p in problems_for(json.loads(json.dumps(record)), daemon=True)
        if "owner" in p or "gate" in p
    ]


def test_one_measured_windows_start_counts_once():
    assert _judged_launches(list(_ONE_START)) == []


@pytest.mark.parametrize(
    ("events", "reported"),
    [
        pytest.param(
            # A second gate, nested under the first, with its own nonce.
            [*_ONE_START, _started(8400, 8316, 95.0, _gate_command("b"))],
            "an extra owner start was attempted",
            id="nested-gate-another-nonce",
        ),
        pytest.param(
            # The same command again, under the launcher's number after that
            # launcher was seen gone: a later process that reused it.
            [
                *_ONE_START[:4],
                _exited(2172, 90.0, 100.0),
                _exited(8316, 90.0, 170.0),
                _started(9000, 2172, 150.0, _gate_command("a")),
            ],
            "an extra owner start was attempted",
            id="historical-parent-number",
        ),
        pytest.param(
            # The same command under the launcher's number, begun before it.
            [*_ONE_START, _started(9100, 2172, 85.0, _gate_command("a"))],
            "an extra owner start was attempted",
            id="parent-younger-than-child",
        ),
        pytest.param(
            # A second, independent start: its own launcher and interpreter.
            [
                *_ONE_START,
                _started(7000, 2836, 130.0, _gate_command("e")),
                _started(7001, 7000, 130.005, _gate_command("e")),
            ],
            "an extra owner start was attempted",
            id="independent-pair",
        ),
        pytest.param(
            # An owner under the first owner, with another job: its own start.
            [*_ONE_START, _started(4400, OWNER[0], 140.0, _owner_command("job-2"))],
            "the row started owners",
            id="nested-owner-another-command",
        ),
        pytest.param(
            # The owner launcher leaves, its number goes to another process,
            # and that one starts an owner with the same command, all between
            # two samples: the sample that first sees the new owner is the one
            # that sees the launcher gone, so nothing ties the two.
            [
                *_ONE_START,
                _exited(1312, 99.99, 110.06),
                {**_started(1312, 2836, 110.02, ["C:\\frontend.exe"]), "t": 110.06},
                {**_started(4400, 1312, 110.03, _owner_command("job-1")), "t": 110.06},
            ],
            "the row started owners",
            id="launcher-number-reused-between-samples",
        ),
    ],
)
def test_a_distinct_start_is_never_collapsed(events, reported):
    problems = _judged_launches(events)
    assert any(reported in p for p in problems), problems


def test_a_launcher_read_early_in_the_childs_own_sample_ties_nothing():
    # One sample reads the old launcher at its start (110.01), the launcher
    # leaves, its number goes to a frontend that starts an owner with the same
    # command, and the same sample reads that owner and ends at 110.06. Both
    # carry 110.06; only the next sample (110.10) finds the launcher gone. No
    # sample begun after 110.06 found the launcher, so nothing ties the two.
    samples = [
        *[entry for entry in _SAMPLES if entry[1] < 110.0],
        [110.01, 110.06],
        [110.10, 110.11],
        *[entry for entry in _SAMPLES if entry[0] > 110.2],
    ]
    events = [
        *_ONE_START,
        _exited(1312, 99.99, 110.11),
        {**_started(1312, 2836, 110.02, ["C:\\frontend.exe"]), "t": 110.11},
        {**_started(4400, 1312, 110.03, _owner_command("job-1")), "t": 110.06},
    ]
    problems = _judged_launches(events, samples=samples)
    assert any("the row started owners" in p for p in problems), problems


def test_a_reused_number_born_after_the_child_is_not_its_launcher():
    # A frontend (1312) starts the owner at 100.0 and leaves; its number goes
    # to an independent owner, same command, born 5 ms later and read alive
    # for long after. Born after the child, it cannot be the child's parent.
    events = [
        *_ONE_START[:2],
        _started(1312, 2836, 99.99, ["C:\\frontend.exe"]),
        _started(OWNER[0], 1312, OWNER[1], _owner_command("job-1")),
        _exited(1312, 99.99, 100.06),
        _started(1312, 2836, OWNER[1] + 0.005, _owner_command("job-1")),
        *_ONE_START[4:],
    ]
    problems = _judged_launches(events)
    assert any("the row started owners" in p for p in problems), problems


@pytest.mark.parametrize(
    ("events", "reported"),
    [
        pytest.param(
            # The owner leaves and its number goes, 5 ms later, to an owner
            # started for another job: two lifetimes, two starts.
            [
                *_ONE_START,
                _exited(OWNER[0], OWNER[1], 100.004),
                _started(OWNER[0], 2836, OWNER[1] + 0.005, _owner_command("job-2")),
            ],
            "the row started owners",
            id="owner-number-reused-within-ms",
        ),
        pytest.param(
            # The gate interpreter leaves and its number goes, 7 ms after its
            # birth, to a gate with another nonce. Its launcher is untouched,
            # so without the second gate the start is valid.
            [
                *_ONE_START,
                _exited(8316, 90.005, 90.02),
                {**_started(8316, 2836, 90.012, _gate_command("b")), "t": 90.03},
            ],
            "an extra owner start was attempted",
            id="gate-number-reused-within-ms",
        ),
    ],
)
def test_a_lifetime_reusing_a_number_within_milliseconds_stays_its_own(
    events, reported
):
    problems = _judged_launches(events)
    assert any(reported in p for p in problems), problems


def test_an_earlier_lifetimes_exit_is_not_lent_to_the_launcher():
    # A frontend under the launcher's number left 5 ms before the launcher
    # was born there. Its exit is its own: the launcher is still read alive
    # long after its interpreter, so the pair is one start.
    events = [
        _started(1312, 2836, 99.985, ["C:\\frontend.exe"]),
        _exited(1312, 99.985, 99.99),
        *_ONE_START,
    ]
    assert _judged_launches(events) == []


def test_launch_lifetimes_without_the_sample_log_do_not_collapse():
    assert _judged_launches(list(_ONE_START), samples=[])


def test_an_equal_command_pair_collapses_only_on_windows():
    # The venv launcher is a Windows construction; elsewhere an owner under an
    # owner with the same command is a second start.
    assert _judged_launches(list(_ONE_START), platform="linux")


@pytest.mark.parametrize("field", ["owner_processes", "gate_processes"])
def test_launch_lifetimes_without_their_invocation_do_not_collapse(field):
    record = _r2_record(daemon=True, platform="win32")
    owners, gates = harness.launch_lifetimes(
        list(_ONE_START), [harness.gate_script(harness.REPO_ROOT)], _SAMPLES
    )
    record["owner_processes"], record["gate_processes"] = owners, gates
    # Intact, the pairs collapse and the record is valid.
    assert problems_for(record, daemon=True) == []
    # The earlier record shape: no command digest, no recorded exit.
    record[field] = [entry[:3] for entry in record[field]]
    assert problems_for(record, daemon=True)
    del record[field]
    assert any("not recorded" in p for p in problems_for(record, daemon=True))


def _incomplete(point: dict) -> None:
    point["census"]["unresolved"] = [777]


@pytest.mark.parametrize(
    ("build", "label", "change"),
    [
        pytest.param(
            lambda: _record(daemon=False),
            FIRST_POST_EXIT,
            _incomplete,
            id="r3-direct-first",
        ),
        pytest.param(
            lambda: _record(daemon=True),
            FIRST_POST_EXIT,
            _incomplete,
            id="r3-daemon-first",
        ),
        pytest.param(
            lambda: _record(daemon=False), SETTLED, _incomplete, id="r3-direct-settled"
        ),
        pytest.param(
            lambda: _r2_record(daemon=False),
            AFTER_B_QUIT,
            _incomplete,
            id="r2-direct-after-b",
        ),
        pytest.param(
            lambda: _r2_record(daemon=False),
            FIRST_POST_EXIT,
            _incomplete,
            id="r2-direct-first",
        ),
        pytest.param(
            lambda: _r2_record(daemon=True),
            AFTER_A1,
            _incomplete,
            id="r2-daemon-after-a1",
        ),
        pytest.param(
            lambda: _r2_record(daemon=False),
            AFTER_B_QUIT,
            lambda point: point.__setitem__("census", "unreadable"),
            id="r2-malformed",
        ),
        pytest.param(
            lambda: _record(daemon=False),
            FIRST_POST_EXIT,
            lambda point: point.pop("census"),
            id="r3-missing",
        ),
    ],
)
def test_an_incomplete_census_at_any_required_checkpoint_fails(build, label, change):
    record = build()
    daemon = record["mode"] == "daemon"
    change(_at(record, label))
    problems = problems_for(record, daemon=daemon)
    reported = f"{label}: the census is incomplete or malformed"
    assert any(p.startswith(reported) for p in problems), problems
    same_row = _r2_record if record["row"] == ROW_H_R2 else _record
    refusals = (
        comparison_refusals(same_row(daemon=False), record)
        if daemon
        else comparison_refusals(record, same_row(daemon=True))
    )
    assert any(reported in r for r in refusals), refusals
    assert any(
        reported in d for d in semantic_differences(build(), record, daemon=daemon)
    )


async def test_an_incomplete_direct_first_census_fails_the_row(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
):
    scene = _Scene(row, monkeypatch, tmp_path, daemon=False)

    def incomplete():
        point = scene.reading(alive=False, occupied=False, lock="free")
        point["census"]["unresolved"] = [777]
        return point

    scene.readings[FIRST_POST_EXIT] = incomplete
    result, _ = await scene.run()
    assert any("the census is incomplete" in f for f in result.failures), (
        result.failures
    )


async def test_an_incomplete_census_after_b_quit_fails_the_row(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
):
    scene = _SecondHostScene(row, monkeypatch, tmp_path, daemon=False)

    def incomplete():
        point = scene.reading(alive=True, occupied=False, lock="free")
        point["census"]["unresolved"] = [777]
        return point

    scene.readings[AFTER_B_QUIT] = incomplete
    result, _ = await scene.run()
    assert any(
        f"{AFTER_B_QUIT}: the census is incomplete" in f for f in result.failures
    ), result.failures


def _direct_second(row, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    return _SecondHostScene(row, monkeypatch, tmp_path, daemon=False)


@pytest.mark.parametrize(
    ("setup", "reported"),
    [
        pytest.param(
            lambda scene, mp: setattr(
                scene,
                "census_after_b",
                lambda: harness.ProfileCensus(unresolved=[987654]),
            ),
            "host B's browser is not shown gone",
            id="unresolved-census",
        ),
        pytest.param(
            lambda scene, mp: setattr(
                scene,
                "census_after_b",
                lambda: (_ for _ in ()).throw(RuntimeError("planted census failure")),
            ),
            "host B's browser is not shown gone",
            id="failed-census",
        ),
        pytest.param(
            lambda scene, mp: (
                mp.setattr(harness, "_BROWSER_GONE_SECONDS", 0.2),
                setattr(
                    scene,
                    "census_after_b",
                    lambda: harness.ProfileCensus(processes=[SimpleNamespace(pid=900)]),
                ),
            ),
            "host B's browser is not shown gone",
            id="residual-browser",
        ),
        pytest.param(
            lambda scene, mp: setattr(scene, "lock_before_a2", "held"),
            "before A2: the lock was held, not free",
            id="lock-held-before-a2",
        ),
        pytest.param(
            lambda scene, mp: setattr(scene, "lock_before_a2", "unknown"),
            "before A2: the lock was unknown, not free",
            id="contender-failed-before-a2",
        ),
    ],
)
async def test_an_unsettled_direct_host_b_takes_no_a2_and_no_preservation(
    row,  # noqa: F811 - the imported fixture
    monkeypatch,
    tmp_path,
    setup,
    reported,
):
    scene = _direct_second(row, monkeypatch, tmp_path)
    setup(scene, monkeypatch)
    result, preserved = await scene.run()
    assert any(reported in f for f in result.failures), result.failures
    assert scene.a2_called == 0 and preserved == 0
    published = scene.published()["comparison"]
    assert any(reported in p for p in published["blocked"])


async def test_a_host_b_pending_after_cleanup_stays_owned_until_it_settles(second):
    second.b["pending"] = True
    result, preserved = await second.run()
    assert second.a2_called == 0 and preserved == 0
    assert any("it stays retained" in f for f in result.failures), result.failures
    left = unconfirmed_close.settlement_problems(grace=0.0)
    assert any("host B's server 4343" in p for p in left), left
    # A later row is refused while it stays owned.
    with pytest.raises(UnsettledWorker):
        await second.run()
    # Once its own process object has a return code, nothing is held.
    second.b_process.returncode = -9
    assert unconfirmed_close.settlement_problems(grace=0.0) == []


async def test_a_row_cancelled_while_host_b_runs_leaves_b_owned(second):
    second.b_active = asyncio.Event()
    task = asyncio.ensure_future(second.run())
    try:
        async with asyncio.timeout(10):
            await second.b_running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        second.b_active.set()
    assert second.a2_called == 0
    second.preservation.assert_not_awaited()
    left = unconfirmed_close.settlement_problems(grace=0.0)
    assert any("host B's server 4343" in p for p in left), left
    with pytest.raises(UnsettledWorker):
        await second.run()
    second.b_process.returncode = 0
    assert unconfirmed_close.settlement_problems(grace=0.0) == []
