"""Losing the host mid-call: the losses, the stub host, the verdict, the rows.

No browser. The **losses** run through the real ``run_host_session`` and
``run_stub_host_session`` against a stand-in stdio server that holds a call
open and, as it exits, says whether its stdin ended and whether its stdout
could still be written: what a server can observe of each loss. The
**verdict** starts from an explicit valid record of each losing row and
changes one observation at a time. The **wiring** runs the real row entry in
Direct mode on the preservation gate's modelled actors, with a host double
whose reads are real requests to a real origin, so the row's own script
arms, waits for, loses, releases and watches a real gate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from differential import call_loss, harness, lease_probe
from differential.call_loss import (
    ACTOR_KILLED,
    CAUSE_EXPIRY,
    CAUSE_UNOBSERVED,
    CONTINUATION_SECONDS,
    EOF_LOSS,
    HELD_SECTION,
    HOST_KILLED,
    HOT_REUSE_WINDOW_SECONDS,
    INVALID,
    LOSS_CASES,
    LOSS_IDLE_TIMEOUT_SECONDS,
    LOSS_K2_NOT_APPLICABLE,
    LOSS_USERNAME,
    NEXT_SECTION,
    PERSON_TOOL,
    PIPE_LOSS,
    RELEASE_SECONDS,
    RELEASE_TOLERANCE_SECONDS,
    ROW_H_R4_EOF,
    ROW_H_R4_HOST,
    ROW_H_R4_PIPE,
    ROW_H_R4_TWO,
    ROW_H_R5,
    SECOND_USERNAME,
    TIMING_UNCLAIMED,
    invalid_evidence,
    loss_comparison,
    loss_problems,
    loss_semantic_differences,
)
from differential.events import LOSSES, validate
from differential.harness import (
    RAISED,
    RETURNED,
    StubHostGone,
    judge_row,
    measure_host_quit_row,
    run_host_session,
    run_stub_host_session,
)
from differential.synthetic_origin import (
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    RELEASED_BY_TEARDOWN,
    SERVED,
    person_path,
)
from differential.test_call_loss import (  # noqa: F401 - fixtures
    _CalibrationScene,
    certificates,
    origin,
    owned,
)
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _Actor,
    _Watcher,
    profile,
)
from differential.test_row_judgement import _healthy

MS = 1_000_000
S = 1_000 * MS


# --- The losses, against a stand-in server ---------------------------------------

#: A stdio server with the warm-up read and a call it holds open. Each time
#: the held call starts it touches ``<die>.entered``; as it exits it writes
#: ``<die>.end``: whether the held call was cancelled, and whether a write to
#: its stdout still went through.
_LOSS_STAND_IN = """
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

from fastmcp import FastMCP

die = sys.argv[1]
seen = {"cancelled": False}


@asynccontextmanager
async def closing(app):
    with open(die + ".pid", "w") as pid:
        pid.write(str(os.getpid()))
    try:
        yield {}
    finally:
        try:
            sys.stdout.buffer.write(b"\\n")
            sys.stdout.buffer.flush()
            seen["stdout"] = "open"
        except OSError as exc:
            seen["stdout"] = type(exc).__name__
        with open(die + ".end", "w") as end:
            json.dump(seen, end)


mcp = FastMCP("stand-in", lifespan=closing)


@mcp.tool
def get_feed(num_posts: int = 10) -> dict:
    return {"url": "https://www.linkedin.com/feed/", "sections": {"feed": "read"}}


@mcp.tool
async def hold(seconds: float = 30.0) -> dict:
    open(die + ".entered", "w").close()
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        seen["cancelled"] = True
        raise
    return {"sections": {}}


mcp.run(transport="stdio", show_banner=False)
"""


def _stand_in(tmp_path: Path) -> tuple[list[str], Path]:
    server = tmp_path / "loss_stand_in.py"
    server.write_text(_LOSS_STAND_IN)
    die = tmp_path / "die"
    return [sys.executable, str(server), str(die)], die


async def _until(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        await asyncio.sleep(0.02)


def _losing_script(die: Path, termination: str, seen: dict[str, Any]):
    """Hold a call open, lose the host once the server holds it, and read
    what the host saw from this body, before the client leaves."""

    async def script(call, host) -> None:
        held = asyncio.ensure_future(call("hold", {"seconds": 30.0}))
        await _until(Path(f"{die}.entered"))
        if termination == HOST_KILLED:
            host.server_handle = psutil.Process(host.pid)
        seen["lost_with_call_open"] = not held.done()
        await host.lose(termination)
        try:
            await asyncio.wait_for(held, 30)
        except Exception as exc:  # noqa: BLE001 - what the host saw
            seen["call_raised"] = type(exc).__name__
        seen["exit"] = await host.server_exit(30.0)
        seen["killed_before_the_client_left"] = host.killed_by_harness

    return script


@pytest.mark.parametrize("termination", [EOF_LOSS, PIPE_LOSS])
async def test_a_lost_host_is_not_quit_and_its_server_ends_by_itself(
    tmp_path, termination
):
    command, die = _stand_in(tmp_path)
    seen: dict[str, Any] = {}

    session = await run_host_session(
        command,
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lambda _line: None,
        row_script=_losing_script(die, termination, seen),
    )

    assert session.error is None, session.error
    assert session.lost == termination and session.loss_error is None
    assert seen["lost_with_call_open"] is True
    assert seen["call_raised"]
    # Seen gone by the script, so the corrective stop had nothing to end.
    assert seen["exit"]["how"] == "exited"
    assert session.killed_by_harness is False and session.stopped_monotonic_ns is None
    # No quit was made: no EOF of the quit's own, no wait.
    assert session.eof_monotonic_ns is None and session.exited_on_quit is None
    [held] = [call for call in session.calls if call["tool"] == "hold"]
    assert held["outcome"] == RAISED
    end = json.loads(Path(f"{die}.end").read_text())
    # Only the pipe loss leaves the server a stdout it cannot write.
    assert (end["stdout"] == "open") is (termination == EOF_LOSS), end


async def test_a_killed_stub_host_breaks_every_pipe_of_its_server(tmp_path):
    command, die = _stand_in(tmp_path)
    seen: dict[str, Any] = {}

    session = await run_stub_host_session(
        command,
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lambda _line: None,
        row_script=_losing_script(die, HOST_KILLED, seen),
    )

    assert session.error is None, session.error
    assert session.lost == HOST_KILLED and session.loss_error is None
    assert session.tool is not None and session.tool["outcome"] == RETURNED
    assert seen["lost_with_call_open"] is True
    # The harness learns of the loss from its own channel to the stub.
    assert seen["call_raised"] == StubHostGone.__name__
    assert seen["exit"]["how"] == "exited"
    assert session.killed_by_harness is False
    end = json.loads(Path(f"{die}.end").read_text())
    assert end["stdout"] != "open", end


async def test_a_stub_host_quits_its_server_as_a_host_does(tmp_path):
    command, _ = _stand_in(tmp_path)

    session = await run_stub_host_session(
        command, env=dict(os.environ), cwd=tmp_path, on_stderr=lambda _line: None
    )

    assert session.error is None, session.error
    assert session.lost is None
    assert session.tool is not None and session.tool["outcome"] == RETURNED
    assert harness.host_failures(session) == []


async def test_a_stub_host_refuses_a_phase_it_does_not_run(tmp_path):
    with pytest.raises(ValueError, match="second_call"):
        await run_stub_host_session(
            ["unused"],
            env={},
            cwd=tmp_path,
            on_stderr=lambda _line: None,
            second_call=True,
        )


# --- The verdict --------------------------------------------------------------------

_OWNER_PID, _OWNER_START = 4242, 1000.5
_OWNER = [_OWNER_PID, _OWNER_START, "instance-a"]


def _owner_lifetime(
    pid: int,
    start: float,
    *,
    ppid: int = 1,
    digest: str = "owner",
    first: float | None = None,
    last: float | None = None,
) -> list:
    """One lifetime as ``harness.launch_lifetimes`` records it: pid, start,
    ppid, command digest, exit sample, first sample, last read."""
    return [
        pid,
        start,
        ppid,
        digest,
        None,
        start + 0.1 if first is None else first,
        start + 100.0 if last is None else last,
    ]


def _request(path: str, ms: int, *, valid: bool | None = True) -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": valid,
        "t": 1000.0 + ms / 1000,
        "monotonic_ns": ms * MS,
    }


def _valid(row: str = ROW_H_R4_EOF, *, daemon: bool = True) -> dict:
    """One valid record of *row*: the warm-up, then the held read, its page
    entered at 4 s and the host lost at 5.1 s; the release asked for 15 s
    after the entry and the origin watched until cleanup at 120 s. Direct
    reads its profile settled at 7 s, and the daemon's owner stays the one
    identified; a fresh host reads the feed at 31 s."""
    case = LOSS_CASES[row]
    entered, lost = 4_000, 5_100
    released = entered + int(RELEASE_SECONDS * 1_000) + 5
    calls = [
        {
            "tool": harness.READ_TOOL,
            "outcome": RETURNED,
            "is_error": False,
            "began_monotonic_ns": 100 * MS,
            "ended_monotonic_ns": 900 * MS,
        },
        {
            "tool": PERSON_TOOL,
            "outcome": RAISED,
            "exception": "MCPError",
            "began_monotonic_ns": 1_000 * MS,
            "ended_monotonic_ns": 5_200 * MS,
        },
    ]
    if case.second:
        calls.append(
            {
                "tool": PERSON_TOOL,
                "outcome": RAISED,
                "exception": "MCPError",
                "began_monotonic_ns": 4_050 * MS,
                "ended_monotonic_ns": 5_200 * MS,
            }
        )
    name, target = call_loss.loss_event(case.termination, daemon=daemon)
    record: dict[str, Any] = {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": LOSS_IDLE_TIMEOUT_SECONDS,
        "k2": dict(LOSS_K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "username": LOSS_USERNAME,
        "case": {"termination": case.termination, "second": case.second},
        "timing_objective": TIMING_UNCLAIMED,
        "owner_identified": list(_OWNER) if daemon else None,
        "host": {
            "error": None,
            "lost": case.termination,
            "lost_ns": lost * MS,
            "loss_error": None,
            "killed_by_harness": False,
            "stop_ns": None,
        },
        "calls": calls,
        "loss": {
            "kind": case.termination,
            "loss": name,
            "target": target,
            "monotonic_ns": lost * MS,
            "error": None,
        },
        "release": {"scheduled_ns": (released - 5) * MS, "requested_ns": released * MS},
        "server_exit": {"how": "exited", "code": 0, "seen_ns": 5_500 * MS},
        "gates": [
            {
                "path": person_path(LOSS_USERNAME, HELD_SECTION),
                "ordinal": 1,
                "entered_monotonic_ns": entered * MS,
                "release_requested_monotonic_ns": released * MS,
                "released_by": RELEASED_BY_ROW,
                # Direct's browser is gone well before the release; the
                # owner's browser stays, and is answered.
                "released_monotonic_ns": (released + 5 if daemon else 6_000) * MS,
                "terminal": SERVED if daemon else PEER_GONE,
                "wrote": daemon,
            }
        ],
        "requests": [
            _request("/feed/", 500),
            _request(person_path(LOSS_USERNAME, "main_profile"), 1_100),
            _request(person_path(LOSS_USERNAME, HELD_SECTION), 3_990),
            _request("/feed/", 32_000),
        ],
        "watched_until_ns": (released + 10_005) * MS,
        "fresh": {
            "made": True,
            "launched_ns": 30_000 * MS,
            "call": {
                "tool": harness.READ_TOOL,
                "outcome": RETURNED,
                "is_error": False,
                "read_the_post": True,
                "began_monotonic_ns": 31_000 * MS,
                "ended_monotonic_ns": 33_000 * MS,
            },
            "forwarded": daemon,
            "quit_problems": [],
            "retained": False,
        },
        "cleanup_began_ns": 120_000 * MS,
    }
    if case.second:
        record["second"] = {"username": SECOND_USERNAME}
    if daemon:
        record.update(
            owner_after_loss={
                "lifetime": _OWNER[:2],
                "instance_id": _OWNER[2],
                "alive": True,
                "seen_ns": 30_000 * MS,
            },
            owner_after_fresh={
                "lifetime": _OWNER[:2],
                "instance_id": _OWNER[2],
                "alive": True,
                "seen_ns": 34_000 * MS,
            },
            owner_processes=[_owner_lifetime(_OWNER_PID, _OWNER_START)],
            gate_processes=[],
            expiry_lines=0,
            cause=CAUSE_UNOBSERVED,
        )
    else:
        record["settlement"] = {
            "remaining": [],
            "unresolved": [],
            "lease": lease_probe.FREE,
            "seen_ns": 7_000 * MS,
            **({"guardian_exit": "exited"} if case.termination == ACTOR_KILLED else {}),
        }
    return record


_ALL = [
    pytest.param(row, daemon, id=f"{row}-{'daemon' if daemon else 'direct'}")
    for row in LOSS_CASES
    for daemon in (False, True)
]


@pytest.mark.parametrize(("row", "daemon"), _ALL)
def test_a_valid_losing_record_passes(row, daemon):
    assert loss_problems(_valid(row, daemon=daemon), daemon=daemon) == []


def _changed(row: str, daemon: bool, change: Callable[[dict], Any]) -> list[str]:
    record = _valid(row, daemon=daemon)
    change(record)
    return loss_problems(record, daemon=daemon)


def _gate(record: dict, **changes) -> None:
    record["gates"][0].update(changes)


def _add(record: dict, path: str, ms: int) -> None:
    record["requests"].append(_request(path, ms))


_NEXT = person_path(LOSS_USERNAME, NEXT_SECTION)
_HELD = person_path(LOSS_USERNAME, HELD_SECTION)
_ROWS = list(LOSS_CASES)
_LATE = int((RELEASE_SECONDS + RELEASE_TOLERANCE_SECONDS) * 1_000) + 500

#: (rows, modes, change, the problem it must bring, whether that problem is
#: invalid evidence). Each changes one observation of a valid record; modes
#: None is both.
_CONTROLS = [
    pytest.param(
        _ROWS,
        None,
        lambda r: _add(r, _NEXT, 21_000),
        "the read went on after the loss: the education page was requested 1",
        False,
        id="continuation-after-release",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _add(r, _HELD, 20_000),
        "the held page was asked for again after the loss",
        False,
        id="held-page-again",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["loss"].update(monotonic_ns=3_900 * MS),
        "the loss came before the held request entered the gate",
        True,
        id="loss-before-entry",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, entered_monotonic_ns=None),
        "the held section's request never entered the gate",
        True,
        id="never-entered",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, release_requested_monotonic_ns=24_500 * MS),
        "after its declared time, past the gate's deadline",
        True,
        id="release-after-the-gate-deadline",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, release_requested_monotonic_ns=(4_000 + _LATE) * MS),
        "after its declared time",
        True,
        id="release-missed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, release_requested_monotonic_ns=10_000 * MS),
        "the release was asked for before its declared time",
        True,
        id="release-early",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, terminal=DEADLINE),
        "the hold ran out its deadline before the release",
        True,
        id="gate-deadline",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, terminal=PEER_GONE, released_monotonic_ns=4_500 * MS),
        "the hold had ended before the loss",
        True,
        id="peer-gone-before-the-loss",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, released_by=RELEASED_BY_TEARDOWN),
        "the hold was released by 'teardown'",
        True,
        id="released-by-teardown",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(cleanup_began_ns=25_000 * MS),
        f"watched for less than {CONTINUATION_SECONDS}s after the release",
        True,
        id="watched-too-briefly",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["server_exit"].update(seen_ns=4_500 * MS),
        "the server's exit is not shown read after the loss",
        True,
        id="stale-exit-reading",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["loss"].update(error="not killed: server 1 was never associated"),
        "the loss failed: not killed",
        True,
        id="loss-failed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["server_exit"].update(how="still running"),
        "is not shown to exit by itself within 90.0s of the loss",
        False,
        id="server-stayed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["host"].update(killed_by_harness=True, stop_ns=130_000 * MS),
        "the harness had to end the",
        False,
        id="corrective-stop",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["fresh"]["call"].update(outcome=RAISED, is_error=None),
        "the read after the loss did not return the synthetic post",
        False,
        id="fresh-read-failed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(timing_objective="within 10.1s of the last heartbeat"),
        "claims the contract's cancellation bound",
        False,
        id="timing-claimed",
    ),
    pytest.param(
        [ROW_H_R4_TWO],
        None,
        lambda r: _add(r, person_path(SECOND_USERNAME, "main_profile"), 20_000),
        "the second read went on after the loss: 1 of its pages",
        False,
        id="second-read-after-loss",
    ),
    pytest.param(
        [ROW_H_R4_TWO],
        None,
        lambda r: _add(r, person_path(SECOND_USERNAME, "main_profile"), 4_500),
        "the second read had begun before the loss",
        True,
        id="second-read-before-loss",
    ),
    pytest.param(
        [ROW_H_R4_TWO],
        None,
        lambda r: r["calls"][2].update(ended_monotonic_ns=4_800 * MS),
        "the second read had ended before the loss",
        True,
        id="second-read-not-outstanding",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r["settlement"].update(seen_ns=121_000 * MS),
        "the settlement was read after the harness's cleanup began",
        True,
        id="corrective-cleanup-before-observation",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r["settlement"].update(remaining=[777]),
        "the profile's browser is not shown gone after the loss",
        False,
        id="direct-browser-stayed",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r["settlement"].update(lease=lease_probe.HELD),
        "the profile lease was 'held' after the loss, not free",
        False,
        id="direct-lease-held",
    ),
    pytest.param(
        [ROW_H_R5],
        False,
        lambda r: r["settlement"].update(guardian_exit="still running"),
        "the killed server's guardian is not shown to drain and exit",
        False,
        id="guardian-stayed",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["owner_after_fresh"].update(lifetime=[4243, 1700.0]),
        "the owner's lifetime changed after the fresh read",
        False,
        id="owner-lifetime-changed",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["owner_after_loss"].update(alive=False),
        "the identified owner is not shown alive after the loss",
        False,
        id="owner-not-alive",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["owner_processes"].append(_owner_lifetime(4243, 1700.0)),
        "the row started another owner, recorded apart as a successor",
        False,
        id="successor-started",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["fresh"]["call"].update(
            began_monotonic_ns=(5_100 + int(HOT_REUSE_WINDOW_SECONDS * 1_000) + 1) * MS
        ),
        f"outside the declared {HOT_REUSE_WINDOW_SECONDS}s window: hot reuse is "
        f"not shown",
        False,
        id="fresh-read-outside-the-window",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["fresh"].update(forwarded=False),
        "the fresh frontend did not forward to the shared owner",
        False,
        id="fresh-read-not-forwarded",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r.update(cause="http-disconnect"),
        "the cancellation cause is recorded as 'http-disconnect'",
        False,
        id="cause-inferred",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["owner_after_fresh"].update(seen_ns=121_000 * MS),
        "the owner reading after the fresh read was read after the harness's "
        "cleanup began",
        True,
        id="stale-owner-reading",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(username="synthetic-calibration"),
        "the record names 'synthetic-calibration' as its username",
        True,
        id="another-username",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(gates=[]),
        "the record holds no gate on the held section",
        True,
        id="no-gate",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(loss={}),
        "the record holds no loss",
        True,
        id="no-loss",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["loss"].update(kind="something-else"),
        "the loss made was 'something-else', not the row's",
        True,
        id="another-loss",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["loss"].update(monotonic_ns=None),
        "the loss has no time",
        True,
        id="untimed-loss",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, terminal=None),
        "the hold recorded no end: None",
        True,
        id="hold-without-end",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _gate(r, release_requested_monotonic_ns=None),
        "no release was asked for",
        True,
        id="never-released",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["calls"][1].update(began_monotonic_ns=4_500 * MS),
        "the held read is not shown sent before its page entered the gate",
        True,
        id="read-sent-after-entry",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: _add(r, _NEXT, 3_995),
        "the read had reached the education page before the loss",
        True,
        id="next-page-before-the-loss",
    ),
    pytest.param(
        [ROW_H_R4_TWO],
        None,
        lambda r: r["calls"][2].update(began_monotonic_ns=5_150 * MS),
        "the second read is not shown sent before the loss",
        True,
        id="second-read-sent-after-loss",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["host"].update(error="TimeoutError: initialize"),
        "the host session failed before its loss",
        False,
        id="host-failed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["host"].update(lost=None),
        "the host records the loss None",
        False,
        id="host-never-lost",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["fresh"].update(made=False, why="unsettled"),
        "no read was made after the loss: 'unsettled'",
        False,
        id="no-fresh-read",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["fresh"].update(quit_problems=["the harness had to kill"]),
        "the fresh host did not quit normally",
        False,
        id="fresh-host-killed",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r["fresh"].update(forwarded=True),
        "the fresh Direct host forwarded to a shared owner",
        False,
        id="direct-fresh-read-forwarded",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["requests"][2].update(session_valid=False),
        "these pages did not carry the staged session",
        False,
        id="unsigned-held-page",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(script_error="RuntimeError: planted"),
        "the row's script failed: RuntimeError: planted",
        False,
        id="script-failed",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r["observation_problems"].append(f"{INVALID}planted"),
        f"{INVALID}planted",
        True,
        id="observation-problem",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(idle_timeout_seconds=20.0),
        "the row ran with an idle timeout of 20.0",
        False,
        id="another-idle-timeout",
    ),
    pytest.param(
        _ROWS,
        None,
        lambda r: r.update(k2=None),
        "the record does not say why K2 is not applicable",
        False,
        id="k2-unexplained",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r.pop("settlement"),
        "the profile was not read after the loss",
        False,
        id="direct-profile-unread",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: r["settlement"].update(error="UnsettledWorker: census"),
        "reading the profile after the loss failed: UnsettledWorker",
        False,
        id="direct-profile-read-failed",
    ),
    pytest.param(
        _ROWS,
        False,
        lambda r: (
            r.update(platform="win32") or r["settlement"].update(lease=lease_probe.HELD)
        ),
        "the profile lease was 'held' after the loss",
        False,
        id="windows-lease-held",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r.update(owner_identified=None),
        "the owner was never identified before the loss",
        False,
        id="owner-never-identified",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r.pop("owner_after_loss"),
        "the owner was not read after the loss",
        False,
        id="owner-unread",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r["owner_after_fresh"].update(instance_id="instance-b"),
        "the owner's lifetime changed after the fresh read",
        False,
        id="owner-instance-changed",
    ),
    pytest.param(
        _ROWS,
        True,
        lambda r: r.pop("owner_processes"),
        "the row's owner and release gate lifetimes were not recorded",
        False,
        id="owner-starts-unread",
    ),
]


@pytest.mark.parametrize(("rows", "modes", "change", "expected", "invalid"), _CONTROLS)
def test_one_changed_observation_fails_every_row_it_applies_to(
    rows, modes, change, expected, invalid
):
    for row in rows:
        for daemon in (False, True) if modes is None else (modes,):
            problems = _changed(row, daemon, change)
            matching = [problem for problem in problems if expected in problem]
            assert matching, (row, daemon, problems)
            # Invalid evidence is named as such, and a finding never is.
            assert all(p.startswith(INVALID) is invalid for p in matching), matching


def test_windows_records_the_lease_unobserved_and_needs_no_guardian():
    record = _valid(ROW_H_R5, daemon=False)
    record["platform"] = "win32"
    record["settlement"].update(lease=call_loss.LEASE_UNOBSERVED)
    del record["settlement"]["guardian_exit"]
    assert loss_problems(record, daemon=False) == []
    # Elsewhere neither absence passes.
    record["platform"] = "darwin"
    problems = loss_problems(record, daemon=False)
    assert any("the profile lease was 'unobserved'" in p for p in problems)
    assert any("guardian is not shown to drain" in p for p in problems)


def test_windows_counts_a_venv_launcher_and_its_gate_as_the_owners_one_start():
    """The shape measured on windows-latest: a venv launcher of the release
    gate, the interpreter it started with the same command, and the owner the
    gate started. One start, so hot reuse holds; a second gate does not."""
    record = _valid(ROW_H_R4_EOF)
    record["platform"] = "win32"
    launcher = _owner_lifetime(7256, 1000.08, ppid=3628, digest="gate", last=1090.0)
    gate = _owner_lifetime(7884, 1000.09, ppid=7256, digest="gate", first=1000.2)
    record["gate_processes"] = [launcher, gate]
    # The owner's own venv launcher, and the interpreter it started, which is
    # the owner the row identified.
    owner_launcher = _owner_lifetime(
        2524, _OWNER_START - 0.01, ppid=7884, digest="owner", last=1090.0
    )
    record["owner_processes"] = [
        owner_launcher,
        _owner_lifetime(
            _OWNER_PID, _OWNER_START, ppid=2524, digest="owner", first=1000.6
        ),
    ]
    assert loss_problems(record, daemon=True) == []

    record["gate_processes"].append(
        _owner_lifetime(9000, 1500.0, ppid=3628, digest="gate-2")
    )
    problems = loss_problems(record, daemon=True)
    assert any("recorded apart as a successor" in p for p in problems), problems
    # Elsewhere nothing is collapsed: the same pairs are two starts each.
    record["gate_processes"] = [launcher, gate]
    record["platform"] = "linux"
    problems = loss_problems(record, daemon=True)
    assert any("recorded apart as a successor" in p for p in problems), problems


def test_an_identified_owner_the_row_never_launched_is_no_hot_reuse():
    record = _valid(ROW_H_R4_EOF)
    record["owner_processes"] = []
    problems = loss_problems(record, daemon=True)
    assert any("identified owner not launched by the row" in p for p in problems)


def test_invalid_evidence_is_told_apart_from_a_finding():
    record = _valid(ROW_H_R4_EOF)
    _gate(record, terminal=DEADLINE)
    _add(record, _NEXT, 21_000)
    problems = loss_problems(record, daemon=True)

    invalid = invalid_evidence(problems)
    assert invalid == [f"{INVALID}the hold ran out its deadline before the release"], (
        problems
    )
    assert [p for p in problems if p not in invalid] == [
        "the read went on after the loss: the education page was requested 1 "
        "times after it"
    ]


def test_a_missing_expiry_line_is_unobserved_and_neither_cause_excuses_anything():
    unobserved = _valid(ROW_H_R4_EOF)
    assert unobserved["cause"] == CAUSE_UNOBSERVED
    assert loss_problems(unobserved, daemon=True) == []
    expired = _valid(ROW_H_R4_EOF)
    expired.update(cause=CAUSE_EXPIRY, expiry_lines=1)
    assert loss_problems(expired, daemon=True) == []
    # An expiry line takes nothing back: the read that went on still fails.
    for cause in (CAUSE_EXPIRY, CAUSE_UNOBSERVED):
        went_on = _valid(ROW_H_R4_EOF)
        went_on["cause"] = cause
        _add(went_on, _NEXT, 21_000)
        assert any(
            "the read went on after the loss" in p
            for p in loss_problems(went_on, daemon=True)
        )
    # The record has to state it; Direct has none to state.
    del unobserved["cause"]
    assert any(
        "the cancellation cause is recorded as None" in p
        for p in loss_problems(unobserved, daemon=True)
    )
    direct = _valid(ROW_H_R4_EOF, daemon=False)
    direct["cause"] = CAUSE_EXPIRY
    assert "a Direct record names a cancellation cause" in loss_problems(
        direct, daemon=False
    )


def test_a_record_that_is_missing_or_for_another_row_fails():
    assert loss_problems(None, daemon=True) == ["the row kept no record"]
    assert loss_problems({"row": "H-CAL"}, daemon=True) == [
        "the record is for row 'H-CAL', which loses no host"
    ]
    record = _valid(ROW_H_R4_PIPE)
    record["mode"] = "direct"
    assert "the record is for mode 'direct', not daemon" in loss_problems(
        record, daemon=True
    )


def test_the_repeat_compares_classifications_and_refuses_an_invalid_record():
    reference, repeat = _valid(ROW_H_R4_TWO), _valid(ROW_H_R4_TWO)
    for name in (
        "entered_monotonic_ns",
        "release_requested_monotonic_ns",
        "released_monotonic_ns",
    ):
        repeat["gates"][0][name] += 7 * MS
    repeat["fresh"]["call"]["began_monotonic_ns"] += 900 * MS
    # The cause is a race the contract allows either way, not a classification.
    repeat["cause"] = CAUSE_EXPIRY
    assert loss_semantic_differences(reference, repeat) == []
    broken = _valid(ROW_H_R4_TWO)
    _add(broken, person_path(SECOND_USERNAME, "main_profile"), 20_000)
    [refusal] = loss_semantic_differences(reference, broken)
    assert refusal.startswith("the repeat record is not valid")
    assert loss_semantic_differences(None, repeat)[0].startswith(
        "the reference record is not valid"
    )


def test_the_candidate_is_held_to_direct_only_from_two_valid_records():
    for row in LOSS_CASES:
        assert loss_comparison(_valid(row, daemon=False), _valid(row)) == []
    went_on = _valid(ROW_H_R5)
    _add(went_on, _NEXT, 21_000)
    [refusal] = loss_comparison(_valid(ROW_H_R5, daemon=False), went_on)
    assert refusal.startswith("the daemon record is not valid")
    [refusal] = loss_comparison(None, _valid(ROW_H_R5))
    assert refusal.startswith("the Direct record is not valid")


def test_the_declared_times_hold_together():
    # The release precedes the gate's deadline even when late by the whole
    # tolerance, and comes after the contract's latest cancellation (expiry
    # 10 s and a poll after a heartbeat up to one 2 s cadence old).
    assert RELEASE_SECONDS + RELEASE_TOLERANCE_SECONDS < GATE_DEADLINE_SECONDS
    assert RELEASE_SECONDS > 2.0 + 10.0 + 0.1
    assert HOT_REUSE_WINDOW_SECONDS < LOSS_IDLE_TIMEOUT_SECONDS


@pytest.mark.parametrize("termination", sorted(call_loss.LOSS_TERMINATIONS))
@pytest.mark.parametrize("daemon", [False, True])
def test_each_loss_is_an_event_the_schema_accepts(termination, daemon):
    name, target = call_loss.loss_event(termination, daemon=daemon)
    assert name in LOSSES
    validate(
        {
            "t": 1.0,
            "run": "r",
            "experiment": "K3",
            "row": ROW_H_R4_EOF,
            "platform": "linux",
            "actor": "harness",
            "kind": "loss",
            "loss": name,
            "target": target,
            "monotonic_ns": 1,
        }
    )


# --- An expected loss in the row's judgement -----------------------------------------


def _lost_host(observed, termination: str):
    """A host the row lost: never quit, its server ended otherwise."""
    host = dataclasses.replace(
        observed.host,
        alive_before_quit=None,
        stdin_closed=None,
        exited_on_quit=None,
        exit_code=-9,
        lost=termination,
        lost_monotonic_ns=5 * S,
    )
    return dataclasses.replace(observed, host=host)


@pytest.mark.parametrize("termination", sorted(call_loss.LOSS_TERMINATIONS))
def test_an_expected_loss_does_not_read_as_a_failed_quit(profile, termination):  # noqa: F811
    lost = _lost_host(_healthy(profile, daemon=False), termination)

    vector, failures = judge_row(dataclasses.replace(lost, termination=termination))
    assert failures == [], failures
    assert vector.host_exit_clean is True
    # The same host under a normal quit's declaration fails as it always did.
    _, failures = judge_row(lost)
    assert "the host quit was not a normal one" in failures


def test_a_row_that_kills_on_its_own_trigger_claims_no_recovery(profile):  # noqa: F811
    lost = _lost_host(_healthy(profile, daemon=True), ACTOR_KILLED)
    killed = {"actor": "frontend", "exit": "killed", "pid": 4242}

    vector, failures = judge_row(
        dataclasses.replace(lost, termination=ACTOR_KILLED, killed=killed)
    )
    assert failures == [], failures
    # H-R6's second call is never made here, so nothing is read as recovery.
    assert vector.recovered is None


def test_a_loss_is_declared_only_with_its_own_script_and_alone():
    async def script(ctx) -> None:
        return None

    lifecycle = harness.RowLifecycle(termination=EOF_LOSS)
    assert harness.lifecycle_problems("H-NEW", lifecycle) == [
        "a loss with no script to make it",
        "a loss that combines with other scenarios",
    ]
    alone = dataclasses.replace(
        lifecycle, script=script, recorded=True, scenarios=False
    )
    assert harness.lifecycle_problems(ROW_H_R4_EOF, alone) == []


def test_a_declared_loss_that_never_happened_fails(profile):  # noqa: F811
    healthy = _healthy(profile, daemon=False)

    _, failures = judge_row(dataclasses.replace(healthy, termination=EOF_LOSS))
    assert (
        f"the host's declared loss, {EOF_LOSS}, was not what happened: None" in failures
    )
    assert "the host quit was not a normal one" in failures
    failed = dataclasses.replace(
        healthy.host, error="TimeoutError: initialize", lost=EOF_LOSS
    )
    _, failures = judge_row(
        dataclasses.replace(healthy, host=failed, termination=EOF_LOSS)
    )
    assert (
        "the host session failed before its loss: TimeoutError: initialize" in failures
    )


# --- The losing rows through the row entry ------------------------------------------


class _LostHost:
    """The live host a losing row is handed by the host double: losing it
    ends every call still open, and its server is then seen gone."""

    def __init__(self, scene: _LossScene) -> None:
        self.scene = scene
        self.lost: str | None = None
        self.lost_monotonic_ns: int | None = None
        self.loss_error: str | None = None
        self.killed_by_harness = False

    def mark_lost(self, termination: str) -> None:
        self.lost = termination
        self.lost_monotonic_ns = time.monotonic_ns()
        self.scene.gone.set()
        self.scene.replaced = self.scene.replace_on_loss
        self.scene.owner_left = self.scene.owner_dies_on_loss

    async def lose(self, termination: str) -> None:
        self.mark_lost(termination)

    async def server_exit(self, seconds: float) -> dict[str, Any]:
        return {"how": "exited", "code": 0, "seen_ns": time.monotonic_ns()}


class _Owner:
    """The modelled owner's process: running until the row's script is done,
    then gone. Nothing in a losing row may signal it."""

    def __init__(self, scene: _LossScene) -> None:
        self.scene = scene
        self.pid = 42

    def status(self):
        if self.scene.owner_left:
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_RUNNING

    def is_running(self) -> bool:
        return not self.scene.owner_left

    def wait(self, timeout=None):
        if not self.scene.owner_left:
            raise psutil.TimeoutExpired(timeout or 0, self.pid)

    def kill(self):
        raise AssertionError("a losing row signalled the owner")


class _LossScene(_CalibrationScene):
    """The real row entry in Direct mode, for a losing row: modelled actors, a
    real origin, and the row's times shortened so its waits are brief.

    The host double reads the profile's pages as real requests. Once the row
    loses its host, every call still open raises as a client's does, while
    the held request stays at the gate until the row releases it. With
    ``goes_on`` the lost read carries on after its held page is answered,
    and with ``second_goes_on`` the second read is served after the loss: the
    two continuations the row exists to catch.
    """

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.daemon = False
        self.replace_on_loss = False
        self.owner_dies_on_loss = False
        self.replaced = False
        self.owner_left = False
        self.goes_on = False
        self.second_goes_on = False
        self.hosts = 0
        self.background: list[asyncio.Future[Any]] = []
        self.gone = asyncio.Event()
        for name, value in (
            ("RELEASE_SECONDS", 0.5),
            ("RELEASE_TOLERANCE_SECONDS", 1.0),
            ("CONTINUATION_SECONDS", 0.5),
            ("SECOND_SEND_SECONDS", 0.1),
        ):
            monkeypatch.setattr(call_loss, name, value)
        monkeypatch.setattr(
            harness,
            "read_lock",
            lambda _path: {"now": None, "answer": {"state": lease_probe.FREE}},
        )

    async def _went_on(self, held: asyncio.Future[Any], username: str) -> None:
        await held
        await asyncio.to_thread(self._read, person_path(username, NEXT_SECTION))

    async def _call(self, session, name: str, arguments: dict) -> dict:
        if name != PERSON_TOOL:
            return await super()._call(session, name, arguments)
        record: dict[str, Any] = {
            "tool": name,
            "began": time.time(),
            "began_monotonic_ns": time.monotonic_ns(),
        }
        session.calls.append(record)
        username = arguments["linkedin_username"]
        try:
            if username == SECOND_USERNAME:
                # Behind the first, as the server's one-call-at-a-time lock
                # keeps it, until the loss.
                await self.gone.wait()
                if self.second_goes_on:
                    await asyncio.to_thread(
                        self._read, person_path(username, "main_profile")
                    )
                raise RuntimeError("Connection closed")
            await asyncio.to_thread(self._read, person_path(username, "main_profile"))
            held = asyncio.ensure_future(
                asyncio.to_thread(self._read, person_path(username, HELD_SECTION))
            )
            self.background.append(held)
            gone = asyncio.ensure_future(self.gone.wait())
            await asyncio.wait({held, gone}, return_when=asyncio.FIRST_COMPLETED)
            gone.cancel()
            if self.gone.is_set():
                if self.goes_on:
                    self.background.append(
                        asyncio.ensure_future(self._went_on(held, username))
                    )
                raise RuntimeError("Connection closed")
            await asyncio.to_thread(self._read, person_path(username, NEXT_SECTION))
        except BaseException as exc:
            record.update(
                ended=time.time(),
                ended_monotonic_ns=time.monotonic_ns(),
                outcome=RAISED,
                exception=type(exc).__name__,
            )
            raise
        record.update(
            ended=time.time(),
            ended_monotonic_ns=time.monotonic_ns(),
            outcome=RETURNED,
            is_error=False,
            read_the_post=False,
            marked_sections=list(call_loss.EXPECTED_SECTIONS),
            section_errors=[],
            text="",
        )
        return record

    async def host(
        self,
        *args,
        after_call=None,
        tool=harness.READ_TOOL,
        arguments=None,
        row_script=None,
        **kw,
    ):
        self.hosts += 1
        arguments = harness.READ_TOOL_ARGUMENTS if arguments is None else arguments
        session = harness.HostSession(
            alive_before_quit=True, stdin_closed=True, exited_on_quit=True, exit_code=0
        )
        if self.daemon:
            session.stderr.append("INFO Forwarding to the shared browser owner")
        if "started" in kw:
            kw["started"](4242)
        session.tool = await self._call(session, tool, arguments)
        if after_call is not None:
            await after_call()
        if row_script is not None:
            live = _LostHost(self)

            async def call(name, arguments):
                summary = await self._call(session, name, arguments)
                session.scripted.append(summary)
                return summary

            try:
                await row_script(call, live)
            except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                session.script_error = f"{type(exc).__name__}: {exc}"
            if live.lost is not None:
                # As ``run_host_session`` does: a lost host is never quit.
                session.alive_before_quit = session.stdin_closed = None
                session.exited_on_quit = None
                session.exit_code = None
                session.lost = live.lost
                session.lost_monotonic_ns = live.lost_monotonic_ns
            # The owner idles out once nothing needs it any longer.
            self.owner_left = True
        return session

    def as_daemon(self, monkeypatch, *, log_lines: list[str]) -> None:
        """Daemon mode: an owner the descriptor names and the watcher saw the
        row start, alive until the row's script is done, whose log holds
        *log_lines*. With ``replace_on_loss``, every reading after the loss
        names another lifetime."""
        self.daemon = True
        scene = self
        log = self.tmp_path / "daemon.log"
        log.write_text("".join(f"{line}\n" for line in log_lines))
        published = self.tmp_path / "descriptor.json"
        published.write_text("{}")
        auth_root = str(harness.claim_account(self.tmp_path).auth_root)
        monkeypatch.setattr(
            harness.daemon_descriptor, "descriptor_path", lambda _root: published
        )
        monkeypatch.setattr(
            harness.daemon_descriptor,
            "read",
            lambda _root: SimpleNamespace(
                pid=42, instance_id="first", protocol_version=2, log_path=str(log)
            ),
        )

        def identify(*args, **kwargs):
            pid, instance = (43, "second") if scene.replaced else (42, "first")
            identity = harness.OwnerIdentity(
                pid, float(pid), instance, auth_root, _Owner(scene)
            )
            return identity, None

        monkeypatch.setattr(harness, "identify_owner", identify)
        monkeypatch.setattr(
            _Watcher,
            "records",
            [
                {
                    "kind": "process.start",
                    "actor": "owner",
                    "in_row": True,
                    "pid": 42,
                    "ppid": 7,
                    "start_identity": 42.0,
                    "cmdline": ["python", "-m", harness.OWNER_MODULE],
                }
            ],
        )

    async def run(self, row: str = ROW_H_R4_EOF, **options):
        try:
            return await measure_host_quit_row(
                profile=self.tmp_path / "auth" / "profile",
                experiment="K3" if self.daemon else "K1",
                daemon=self.daemon,
                egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
                log=self.log,
                work_dir=self.tmp_path / "row",
                row=row,
                **options,
            )
        finally:
            await asyncio.gather(*self.background, return_exceptions=True)


@pytest.fixture
def losing(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _LossScene(monkeypatch, tmp_path, profile, origin, certificates)


async def test_a_lost_host_whose_read_stopped_passes_and_publishes_its_record(
    losing,
):
    result = await losing.run()

    assert result.failures == [], result.failures
    record = losing.published()["record"]
    assert record["problems"] == []
    assert loss_problems(record, daemon=False) == []
    [gate] = record["gates"]
    assert gate["released_by"] == RELEASED_BY_ROW and gate["terminal"] == SERVED
    # The settlement and the fresh read, each before the cleanup began.
    assert record["settlement"]["seen_ns"] < record["cleanup_began_ns"]
    assert record["fresh"]["made"] is True and losing.hosts == 2
    assert losing.preservation.await_count == 1
    events = losing.log.records()
    [loss] = [event for event in events if event["kind"] == "loss"]
    assert (loss["loss"], loss["target"]) == ("eof", "frontend")
    phases = [event["name"] for event in events if event["kind"] == "phase"]
    assert phases == ["armed", "entered", "lost", "released", "watched", "fresh read"]


async def test_a_read_that_went_on_after_the_loss_fails_the_row(losing):
    losing.goes_on = True
    result = await losing.run()

    assert any(
        f"{ROW_H_R4_EOF}: the read went on after the loss" in failure
        for failure in result.failures
    ), result.failures
    assert not invalid_evidence(result.record["problems"])


async def test_two_outstanding_requests_pass_only_while_the_second_never_began(
    losing,
):
    result = await losing.run(ROW_H_R4_TWO)
    assert result.failures == [], result.failures
    assert len(result.record["calls"]) == 3


async def test_a_second_request_served_after_the_loss_fails_the_row(losing):
    losing.second_goes_on = True
    result = await losing.run(ROW_H_R4_TWO)

    assert any(
        f"{ROW_H_R4_TWO}: the second read went on after the loss" in failure
        for failure in result.failures
    ), result.failures


async def test_a_read_that_never_reaches_the_held_page_loses_nothing(
    losing, monkeypatch
):
    async def stops_short(session, name, arguments):
        return await _CalibrationScene._call(losing, session, name, arguments)

    losing.pages = ["main_profile"]
    monkeypatch.setattr(losing, "_call", stops_short)
    result = await losing.run()

    assert any("nothing was lost mid-call" in failure for failure in result.failures), (
        result.failures
    )
    # Nothing was lost, so nothing the product did is judged: every problem
    # is invalid evidence, and the host was quit as usual.
    problems = result.record["problems"]
    assert problems and invalid_evidence(problems) == problems, problems
    assert result.record["host"]["lost"] is None


async def test_the_host_killed_row_runs_its_host_in_a_process_of_its_own(
    losing, monkeypatch
):
    stubbed: list[str] = []
    tied: list[int] = []

    async def stub_host(*args, **kwargs):
        stubbed.append(kwargs["tool"])
        return await losing.host(*args, **kwargs)

    def associate(pid, observed, **kw):
        tied.append(pid)
        return _Actor(pid), 1000.0

    monkeypatch.setattr(harness, "run_stub_host_session", stub_host)
    monkeypatch.setattr(harness, "associate_server", associate)
    result = await losing.run(ROW_H_R4_HOST)

    assert result.failures == [], result.failures
    # The row's host, and only it; the fresh read is an ordinary host.
    assert stubbed == [harness.READ_TOOL] and losing.hosts == 2
    # Its server tied before the loss, so its exit can be observed after.
    assert tied == [4242]
    assert result.record["prepared"] == {"server": [4242, 1000.0]}
    [loss] = [event for event in losing.log.records() if event["kind"] == "loss"]
    assert (loss["loss"], loss["target"]) == ("host-killed", "host_stub")


async def test_a_killed_server_is_tied_and_killed_through_the_r6_path(
    losing, monkeypatch
):
    victims: list[_Actor] = []

    def associate(pid, observed, **kw):
        victims.append(_Actor(pid))
        return victims[-1], 1000.0

    monkeypatch.setattr(harness, "associate_server", associate)
    monkeypatch.setattr(harness, "wait_for_guardian", lambda observed, pid: (777, 0))
    monkeypatch.setattr(
        harness, "lifetime_exit_state", lambda observed, pid, seconds: "exited"
    )
    result = await losing.run(ROW_H_R5)

    assert result.failures == [], result.failures
    # Tied before the read, killed once, never anything else.
    assert [victim.kills for victim in victims] == [1]
    record = result.record
    assert record["loss"]["killed"]["exit"] == "killed"
    assert record["settlement"]["guardian_exit"] == "exited"
    [loss] = [event for event in losing.log.records() if event["kind"] == "loss"]
    assert loss["loss"] == "server-killed"


async def test_a_server_that_was_never_tied_is_not_killed_and_the_loss_fails(
    losing, monkeypatch
):
    monkeypatch.setattr(harness, "associate_server", lambda *a, **k: (None, None))
    result = await losing.run(ROW_H_R5)

    assert result.record["prepared"]["error"].startswith("not killed")
    assert any(
        f"{ROW_H_R5}: {INVALID}the loss failed: not killed" in failure
        for failure in result.failures
    ), result.failures


async def test_only_a_row_that_kills_is_held_to_the_signal_oracle(losing, monkeypatch):
    monkeypatch.setattr(harness, "ORACLE_REQUIRED", True)
    monkeypatch.setattr(harness, "associate_server", lambda *a, **k: (_Actor(1), 1.0))
    monkeypatch.setattr(harness, "wait_for_guardian", lambda observed, pid: None)
    required = "O2: the required signal oracle's evidence is unavailable"

    killed = await losing.run(ROW_H_R5)
    assert any(f.startswith(required) for f in killed.failures), killed.failures


async def test_a_loss_without_the_signal_oracle_is_not_held_to_it(losing, monkeypatch):
    monkeypatch.setattr(harness, "ORACLE_REQUIRED", True)
    result = await losing.run(ROW_H_R4_EOF)
    assert result.failures == [], result.failures


async def test_a_direct_profile_not_shown_settled_gets_no_fresh_server(
    losing, monkeypatch
):
    monkeypatch.setattr(
        harness,
        "read_lock",
        lambda _path: {"now": None, "answer": {"state": lease_probe.HELD}},
    )
    result = await losing.run()

    # A second Direct server on a profile still held would be the harness's.
    assert losing.hosts == 1
    assert result.record["fresh"] == {
        "made": False,
        "why": "the Direct server's profile was not shown settled",
    }
    for expected in (
        "the profile lease was 'held' after the loss, not free",
        "no read was made after the loss",
    ):
        assert any(expected in failure for failure in result.failures), expected


async def test_a_lost_frontend_keeps_its_owner_and_reads_through_it_again(
    losing, monkeypatch
):
    losing.as_daemon(monkeypatch, log_lines=["INFO the owner is serving"])
    result = await losing.run(ROW_H_R4_PIPE)

    assert result.failures == [], result.failures
    record = result.record
    assert record["owner_identified"] == [42, 42.0, "first"]
    for label in ("owner_after_loss", "owner_after_fresh"):
        assert record[label]["lifetime"] == [42, 42.0] and record[label]["alive"]
    assert [p[:2] for p in record["owner_processes"]] == [[42, 42.0]]
    assert record["fresh"]["forwarded"] is True
    # No expiry line: the cause is unobserved, and that is no failure.
    assert record["cause"] == CAUSE_UNOBSERVED


async def test_the_owners_expiry_line_is_recorded_as_the_cause(losing, monkeypatch):
    losing.as_daemon(
        monkeypatch,
        log_lines=["INFO Nobody has waited for call 7f in 10s; stopping it"],
    )
    result = await losing.run(ROW_H_R4_EOF)

    assert result.failures == [], result.failures
    assert (result.record["cause"], result.record["expiry_lines"]) == (
        CAUSE_EXPIRY,
        1,
    )


async def test_an_owner_gone_after_the_loss_fails_hot_reuse(losing, monkeypatch):
    # The descriptor still names it, so only its own handle can tell.
    losing.as_daemon(monkeypatch, log_lines=[])
    losing.owner_dies_on_loss = True
    result = await losing.run(ROW_H_R4_EOF)

    assert result.record["owner_after_loss"]["alive"] is False
    assert any(
        "the identified owner is not shown alive after the loss" in failure
        for failure in result.failures
    ), result.failures


async def test_an_owner_replaced_after_the_loss_fails_hot_reuse(losing, monkeypatch):
    losing.as_daemon(monkeypatch, log_lines=[])
    losing.replace_on_loss = True
    result = await losing.run(ROW_H_R4_EOF)

    assert any(
        "the owner's lifetime changed after the loss" in failure
        for failure in result.failures
    ), result.failures
