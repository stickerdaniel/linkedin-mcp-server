"""H-R3's verdict, its producer and its place in the row, without a browser.

Three families. The **contract** tests start from an explicit, valid raw
record per mode and platform, change one observation, and run the verdict the
row runs (``host_comparison.r3_problems``); none derives its expectation from
the verdict. The **producer** tests run ``harness.observe_checkpoint`` over a
modelled process table and hand what it read to that verdict. The **wiring**
tests go through the real row entry, ``measure_host_quit_row``, on the
preservation gate's modelled row, with the host session and the checkpoint
reader replaced by doubles that keep the real seams: the row's own script,
its post-exit hook, its settlement gate and its published ``failures.json``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
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
from differential.baseline import Runtime
from differential.events import EventLog
from differential.host_comparison import (
    BEFORE_QUIT,
    CHECKPOINTS,
    FIRST_POST_EXIT,
    K2_NOT_APPLICABLE,
    ROW_H_R3,
    SETTLED,
    census_roots,
    comparison_refusals,
    r3_problems,
    semantic_differences,
    semantics,
)
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _SETTLED,
    profile,
    row,
)
from differential.unconfirmed_close import R7Setup
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
        if parts[0] in CHECKPOINTS:
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
            return session

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
