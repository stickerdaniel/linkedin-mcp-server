"""Profile commands beside a live owner: the driver, the verdicts, the rows'
wiring and the model mapping.

No browser. The **driver** runs a stand-in CLI as a real child on a real
pseudo-terminal or on real pipes. The **synthetic browser** is found and
ranked by the product's own discovery. The **verdicts** start from an
explicit valid record of each row and change one observation at a time,
the plan's controls among them. The **wiring** runs the real row entry on
modelled actors with a host double whose reads are real requests to a real
origin, held at its real gates, while the row's own script drives the
stand-in CLI through the real seams. The **model mapping** names tests that
exist.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import copy
import dataclasses
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from differential import accounting, harness, model_coverage, profile_commands
from differential.call_loss import EXPECTED_SECTIONS, INVALID, PERSON_TOOL
from differential.harness import (
    MUST_REMAIN_CLEARED,
    ORDINARY,
    RETURNED,
    RowLifecycle,
    lifecycle_problems,
    measure_host_quit_row,
    run_host_session,
)
from differential.lease_probe import FREE, HELD
from differential.profile_commands import (
    BANNER,
    BUSY_COMMANDS,
    BUSY_LINE,
    BUSY_USERNAME,
    CASES,
    CLEARED_LINE,
    COMMAND_IDLE_TIMEOUT_SECONDS,
    DELETE_PROMPT,
    FROZEN_CONTENTION,
    IMPORT_FOUND,
    IMPORT_LEASE_REFUSAL,
    K2_NOT_APPLICABLE,
    KEYCHAIN_NOTICE,
    LEASE_REFUSAL,
    LOGIN_BANNER,
    LOGIN_OPENED,
    MODEL_COVERAGE,
    NEEDS_TERMINAL_LINE,
    RETIRE_PROMPT,
    RETIRING_LINE,
    ROW_BUSY,
    ROW_DECLINE,
    ROW_LOGOUT,
    ROW_NO_TERMINAL,
    ROW_STATUS,
    STATUS_HELD,
    STATUS_SHARED,
    TerminalCommand,
    comparison_refusals,
    h_r10a_problems,
    h_r10b_problems,
    h_r15_problems,
    invalid_evidence,
    problems_for,
    semantic_differences,
    semantics,
    status_category,
    synthetic_browser,
)
from differential.session import CLEARED_BY_USER, LOGOUT, RETAINED
from differential.synthetic_origin import RELEASED_BY_ROW, SERVED, person_path
from differential.test_call_loss import (  # noqa: F401 - fixtures
    _CalibrationScene,
    certificates,
    origin,
    owned,
)
from differential.test_host_stub import _STAND_IN_SERVER
from differential.test_preservation_gate import profile  # noqa: F401 - fixture
from differential.unconfirmed_close import LOCK_FILE

MS = 1_000_000

posix = pytest.mark.skipif(os.name == "nt", reason="a pseudo-terminal is POSIX's")
#: The row's Ctrl-C reaches a command only where this process was not
#: started with SIGINT ignored, as a shell's background job is.
interruptible = pytest.mark.skipif(
    os.name != "nt" and signal.getsignal(signal.SIGINT) is signal.SIG_IGN,
    reason="started with SIGINT ignored, which every child inherits",
)


# --- The driver, on a real terminal and real pipes ----------------------------------

#: A stand-in for the CLI: says whether its stdin and stdout are terminals,
#: asks one question and answers by what it was told, or waits to be
#: interrupted.
_ASKS = r"""
import sys, time
print(f"tty {sys.stdin.isatty()} {sys.stdout.isatty()}", flush=True)
mode = sys.argv[1]
if mode == "ask":
    answer = input("Go on? (y/N): ").strip()
    print(f"answered {answer}", flush=True)
    sys.exit(0 if answer == "y" else 3)
if mode == "wait":
    print("waiting", flush=True)
    time.sleep(600)
if mode == "silent":
    sys.exit(4)
if mode == "helper-now":
    # The helper started as the command exits at once: no poll sees it as
    # a child before it is reparented.
    import subprocess
    subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    sys.exit(0)
if mode == "daemonize":
    # A helper that detaches by double fork and a new session, while the
    # command itself stays a while: never its descendant by ancestry.
    import os
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            time.sleep(60)
            os._exit(0)
        os._exit(0)
    time.sleep(0.5)
    print("detached a helper", flush=True)
    sys.exit(0)
if mode == "helper":
    # A helper with its own output, which outlives the command: a browser
    # the command launched is one. Bounded by its own deadline.
    import subprocess
    subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    print("started a helper", flush=True)
    sys.exit(0)
"""


def _driver(tmp_path: Path, mode: str, *, terminal: bool) -> TerminalCommand:
    script = tmp_path / "asks.py"
    script.write_text(_ASKS)
    command = TerminalCommand(
        [sys.executable, str(script), mode],
        args=[mode],
        env={**os.environ, **profile_commands.COMMAND_ENV},
        cwd=tmp_path,
        terminal=terminal,
        label=mode,
    )
    command.start()
    return command


@posix
async def test_a_command_on_the_terminal_sees_a_terminal_and_is_answered_after_asking(
    tmp_path,
):
    command = _driver(tmp_path, "ask", terminal=True)
    try:
        asked = await command.expect("Go on? (y/N): ", 30)
        assert asked is not None
        answer = command.answer("y")
        assert await command.wait(30) is not None
        assert command.settled(10)
    finally:
        command.end()
    record = command.record()
    text = [line for _, line in record["lines"]]
    assert "tty True True" in text
    assert any(line.startswith("Go on? (y/N): ") for line in text)
    assert "answered y" in text
    assert record["returncode"] == 0 and record["output_ended"] is True
    assert record["ended_by_harness"] is False
    assert asked <= answer["answered_ns"] <= record["exited_ns"]
    # Every line stamped, in the order it came.
    stamps = [at for at, _ in record["lines"]]
    assert stamps == sorted(stamps) and record["started_ns"] <= stamps[0]
    assert record["expected"] == [{"text": "Go on? (y/N): ", "seen_ns": asked}]


async def test_a_command_on_pipes_sees_no_terminal_and_is_answered_all_the_same(
    tmp_path,
):
    command = _driver(tmp_path, "ask", terminal=False)
    try:
        assert await command.expect("Go on? (y/N): ", 30) is not None
        command.answer("n")
        await command.wait(30)
        assert command.settled(10)
    finally:
        command.end()
    record = command.record()
    assert "tty False False" in [line for _, line in record["lines"]]
    assert record["returncode"] == 3 and record["terminal"] is False


@posix
@interruptible
async def test_an_interrupt_ends_a_waiting_command_and_is_the_rows_own(tmp_path):
    command = _driver(tmp_path, "wait", terminal=True)
    try:
        assert await command.expect("waiting", 30) is not None
        sent = command.interrupt()
        await command.wait(30)
    finally:
        command.end()
    record = command.record()
    assert sent["error"] is None and record["interrupted_ns"] == sent["interrupted_ns"]
    assert record["returncode"] not in (None, 0)
    assert record["ended_by_harness"] is False


async def test_a_command_left_running_is_ended_by_the_teardown_and_says_so(
    tmp_path,
):
    command = _driver(tmp_path, "wait", terminal=False)
    assert await command.expect("waiting", 30) is not None
    assert command.settled(0.1) is False
    command.end()
    assert command.settled(10) is True
    record = command.record()
    assert record["ended_by_harness"] is True and record["returncode"] is not None


async def test_a_command_is_not_settled_while_a_helper_it_started_runs(tmp_path):
    """The command exits and its output ends, but the helper it started with
    output of its own still runs: not settled, and the teardown ends that
    helper too, and only it."""
    command = _driver(tmp_path, "helper", terminal=False)
    try:
        assert await command.expect("started a helper", 30) is not None
        await command.wait(30)
        assert command.returncode == 0
        assert command.settled(1.0) is False
        record = command.record()
        assert record["output_ended"] is True
        assert record["descendants_alive"] and record["descendants"]
    finally:
        command.end()
    assert command.settled(10) is True
    record = command.record()
    assert record["ended_by_harness"] is True and record["descendants_alive"] == []


@pytest.mark.parametrize(
    "mode",
    [
        "helper-now",
        pytest.param("daemonize", marks=posix),
    ],
)
async def test_a_helper_no_ancestry_names_is_still_the_commands_to_settle(
    tmp_path, mode
):
    """One started as the command exits, or detached by double fork into a
    session of its own: no poll sees it as a child, but it carries the
    command's marker, so the command is not settled and the teardown ends it."""
    command = _driver(tmp_path, mode, terminal=False)
    try:
        await command.wait(30)
        assert command.returncode == 0
        assert command.settled(1.0) is False
        assert command.record()["descendants_alive"]
    finally:
        command.end()
    assert command.settled(10) is True
    assert command.record()["descendants_alive"] == []


async def test_a_prompt_that_never_comes_is_given_up_once_the_command_is_gone(
    tmp_path,
):
    command = _driver(tmp_path, "silent", terminal=False)
    began = time.monotonic()
    try:
        assert await command.expect("Go on?", 60) is None
    finally:
        command.end()
    # Not the whole bound: an exited command whose output ended asks nothing.
    assert time.monotonic() - began < 30
    assert command.record()["expected"] == [{"text": "Go on?", "seen_ns": None}]


@pytest.mark.skipif(os.name != "nt", reason="the refusal is Windows'")
def test_windows_refuses_a_terminal_command(tmp_path):  # pragma: no cover - Windows
    with pytest.raises(ValueError, match="pseudo-terminal"):
        TerminalCommand(["x"], args=[], env={}, cwd=tmp_path, terminal=True, label="x")


# --- The import's synthetic browser -----------------------------------------------


def test_the_import_discovers_only_the_synthetic_browser_and_ranks_it_live(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.browser_import.discovery import discover_profiles
    from linkedin_mcp_server.browser_import.orchestrate import rank_live_profiles

    environment = synthetic_browser(tmp_path)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    profiles = discover_profiles(profile_commands.IMPORT_BROWSER)
    live, _ = rank_live_profiles(profiles)

    assert [p.profile_dir_name for p in profiles] == ["Default"]
    assert all(tmp_path in p.cookies_db.parents for p in profiles)
    assert len(live) == 1
    # Nothing the record keeps names more than the variables set.
    assert set(environment) <= {"HOME", "XDG_CONFIG_HOME", "LOCALAPPDATA", "APPDATA"}


# --- Verdicts: records ----------------------------------------------------------------

_A = [4242, 999.5, "owner-a"]


def _lifetime(pid: Any, start: Any) -> list:
    return [pid, start, 1, "owner", None, start + 0.1, start + 100.0]


def _host() -> dict:
    return {
        "error": None,
        "alive_before_quit": True,
        "stdin_closed": True,
        "exited_on_quit": True,
        "exit_code": 0,
        "killed_by_harness": False,
        "eof_ns": 1_000 * MS,
        "exit_seen_ns": 1_500 * MS,
    }


def _call(tool: str, began: int, ended: int, **fields) -> dict:
    return {
        "tool": tool,
        "began": 1000.0 + began / 1000,
        "ended": 1000.0 + ended / 1000,
        "began_monotonic_ns": began * MS,
        "ended_monotonic_ns": ended * MS,
        "outcome": RETURNED,
        **fields,
    }


def _feed(began: int, ended: int) -> dict:
    return _call(harness.READ_TOOL, began, ended, is_error=False, read_the_post=True)


def _request(path: str, ms: int) -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": True,
        "t": 1000.0 + ms / 1000,
        "monotonic_ns": ms * MS,
    }


def _command(
    label: str,
    args: list[str],
    lines: list[tuple[int, str]],
    *,
    terminal: bool = True,
    started: int,
    exited: int,
    code: int | None = 0,
    expected: Sequence[tuple[str, int]] = (),
    answers: Sequence[tuple[str, int]] = (),
    interrupted: int | None = None,
) -> dict:
    """A command record, times in ms; on a terminal its banner first."""
    banner = [(started + 50, f"{BANNER}0.0.0 🔗")] if terminal else []
    return {
        "label": label,
        "args": args,
        "terminal": terminal,
        "overridden": sorted(profile_commands.COMMAND_ENV),
        "started_ns": started * MS,
        "exited_ns": exited * MS,
        "returncode": code,
        "interrupted_ns": interrupted * MS if interrupted is not None else None,
        "ended_by_harness": False,
        "output_ended": True,
        "error": None,
        "expected": [{"text": t, "seen_ns": at * MS} for t, at in expected],
        "answers": [
            {"text": t, "answered_ns": at * MS, "error": None} for t, at in answers
        ],
        "lines": [[at * MS, line] for at, line in [*banner, *lines]],
    }


def _point(began: int, ended: int, *, alive: bool = True, lock: str = HELD) -> dict:
    return {
        "began_ns": began * MS,
        "ended_ns": ended * MS,
        "actor_alive": [alive, alive],
        "lock": {"answer": {"state": lock}},
    }


def _owner(ms: int, *, alive: bool = True, lifetime: list | None = None) -> dict:
    return {
        "lifetime": lifetime or list(_A[:2]),
        "instance_id": _A[2],
        "alive": alive,
        "seen_ns": ms * MS,
    }


def _base(row: str, *, daemon: bool) -> dict:
    record: dict[str, Any] = {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": COMMAND_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "host": _host(),
        "egress": {"forwarded": ["www.linkedin.com"], "refused": []},
        "calls": [_feed(100, 900)],
        "requests": [_request("/feed/", 500)],
        "owner_processes": [_lifetime(*_A[:2])] if daemon else [],
        "gate_processes": [],
    }
    if daemon:
        record["owner_identified"] = list(_A)
    return record


def _logout(row: str = ROW_LOGOUT, *, daemon: bool = True) -> dict:
    """H-R10a. The host quits at 1 s; the logout starts at 2 s and asks to
    delete at 3 s, answered at 3.1 s. Daemon: asked to retire at 4 s, a
    checkpoint from 4.1 to 4.3 s, answered at 4.4 s; the owner seen gone at
    6 s, the logout retiring at 4.6 s, cleared at 7 s, exit at 7.1 s."""
    record = _base(row, daemon=daemon)
    record.update(command=["--logout"], terminal=row != ROW_NO_TERMINAL)
    record["host_quit_ns"] = 1_000 * MS
    record["authorized"] = LOGOUT
    deleted = [(3_000, DELETE_PROMPT + "y")]
    if not daemon:
        record["settlement"] = {
            "remaining": [],
            "unresolved": [],
            "lease": FREE,
            "seen_ns": 1_800 * MS,
        }
        record["logout"] = _command(
            "logout",
            ["--logout"],
            [*deleted, (5_000, f"✅ {CLEARED_LINE}!")],
            started=2_000,
            exited=5_100,
            expected=[(DELETE_PROMPT, 3_000)],
            answers=[("y", 3_100)],
        )
        return record
    record["owner_after_quit"] = _owner(1_500)
    if row == ROW_NO_TERMINAL:
        record["before_command"] = _point(1_600, 1_900)
        record["logout"] = _command(
            "logout",
            ["--logout"],
            [
                (3_000, DELETE_PROMPT),
                (3_200, NEEDS_TERMINAL_LINE),
                (3_300, f"RuntimeError: {LEASE_REFUSAL}. Stop the running server"),
            ],
            terminal=False,
            started=2_000,
            exited=3_400,
            code=1,
            expected=[(DELETE_PROMPT, 3_000)],
            answers=[("y", 3_100)],
        )
        record["after_command"] = _point(8_500, 8_800)
        record["owner_after_command"] = _owner(9_000)
        record["owner_lines"] = {"standing_down": 0, "idle_exit": 0}
        return record
    record["before_answer"] = _point(4_100, 4_300)
    if row == ROW_DECLINE:
        record["logout"] = _command(
            "logout",
            ["--logout"],
            [
                *deleted,
                (4_000, RETIRE_PROMPT + "n"),
                (4_600, f"❌ {'Operation cancelled'}"),
            ],
            started=2_000,
            exited=4_700,
            expected=[(DELETE_PROMPT, 3_000), (RETIRE_PROMPT, 4_000)],
            answers=[("y", 3_100), ("n", 4_400)],
        )
        record["after_command"] = _point(9_800, 9_900)
        record["owner_after_command"] = _owner(10_000)
        record["owner_lines"] = {"standing_down": 0, "idle_exit": 0}
        return record
    record["logout"] = _command(
        "logout",
        ["--logout"],
        [
            *deleted,
            (4_000, RETIRE_PROMPT + "y"),
            (4_600, f"ℹ️  {RETIRING_LINE}"),
            (7_000, f"✅ {CLEARED_LINE}!"),
        ],
        started=2_000,
        exited=7_100,
        expected=[(DELETE_PROMPT, 3_000), (RETIRE_PROMPT, 4_000)],
        answers=[("y", 3_100), ("y", 4_400)],
    )
    record["owner_exit"] = {"how": "exited", "seen_ns": 6_000 * MS}
    record["owner_after_command"] = _owner(7_500, alive=False)
    record["owner_lines"] = {"standing_down": 1, "idle_exit": 0}
    return record


def _gate(name: str, entered: int, released: int) -> dict:
    sections = {command: section for command, _, section in BUSY_COMMANDS}
    return {
        "path": person_path(BUSY_USERNAME, sections[name]),
        "ordinal": 1,
        "entered_monotonic_ns": entered * MS,
        "release_requested_monotonic_ns": released * MS,
        "released_monotonic_ns": released * MS + 1,
        "released_by": RELEASED_BY_ROW,
        "terminal": SERVED,
    }


def _busy(*, daemon: bool = True) -> dict:
    """H-R10b. The read sent at 2 s; each section held 10 s apart, from 3, 13
    and 23 s; each command answered 0.5 s into its hold, exited at 2 s and
    released at 5 s; the read returned at 30 s."""
    record = _base(ROW_BUSY, daemon=daemon)
    record.update(username=BUSY_USERNAME, terminal=True, authorized=LOGOUT)
    record["held"] = {
        name: {"path": person_path(BUSY_USERNAME, section), "ordinal": 1}
        for name, _, section in BUSY_COMMANDS
    }
    record["gates"] = []
    for index, (name, args, _section) in enumerate(BUSY_COMMANDS):
        entered = 3_000 + 10_000 * index
        released = entered + 5_000
        record["gates"].append(_gate(name, entered, released))
        if daemon:
            prompts: list[tuple[int, str]] = []
            expected = []
            answers = []
            if name == "logout":
                prompts.append((entered - 1_000, DELETE_PROMPT + "y"))
                expected.append((DELETE_PROMPT, entered - 1_000))
                answers.append(("y", entered + 400))
            prompts.append((entered + 450, RETIRE_PROMPT + "y"))
            expected.append(
                (RETIRE_PROMPT, entered + 450 if name == "logout" else entered - 900)
            )
            answers.append(("y", entered + 500))
            lines = [*prompts, (entered + 1_500, BUSY_LINE)]
            code = 1
        elif name == "logout":
            expected = [(DELETE_PROMPT, entered - 1_000)]
            answers = [("y", entered + 500)]
            lines = [
                (entered - 1_000, DELETE_PROMPT + "y"),
                (entered + 1_500, f"RuntimeError: {LEASE_REFUSAL}."),
            ]
            code = 1
        elif name == "login":
            expected, answers = [(LOGIN_BANNER, entered + 600)], []
            lines = [
                (entered + 600, LOGIN_BANNER),
                (entered + 1_800, "KeyboardInterrupt"),
            ]
            code = -2
        else:
            expected, answers = [], []
            lines = [
                (
                    entered + 700,
                    f"INFO Found 1 {IMPORT_FOUND}; trying most recently used",
                ),
                (entered + 1_500, f"BrowserBusyError: ... {IMPORT_LEASE_REFUSAL}."),
            ]
            code = 1
        started = entered - 2_000 if (daemon or name == "logout") else entered + 100
        command = _command(
            name,
            list(args),
            lines,
            started=started,
            exited=entered + 2_000,
            code=code,
            expected=expected,
            answers=answers,
            interrupted=entered + 1_700 if (not daemon and name == "login") else None,
        )
        command["read_open"] = True
        command["release_requested_ns"] = released * MS
        record[name] = command
    record["calls"].append(
        _call(
            PERSON_TOOL,
            2_000,
            30_000,
            is_error=False,
            marked_sections=list(EXPECTED_SECTIONS),
            section_errors=[],
        )
    )
    record["requests"] += [
        _request(person_path(BUSY_USERNAME, section), 3_000 + 10_000 * index)
        for index, (_, _, section) in enumerate(BUSY_COMMANDS)
    ]
    record["read_open"] = False
    if daemon:
        record["owner_after_read"] = _owner(31_000)
        record["owner_lines"] = {"standing_down": 0, "idle_exit": 0}
    return record


def _status(*, daemon: bool = True) -> dict:
    """H-R15. A checkpoint 1 to 1.2 s, the status 2 to 5 s, a checkpoint 6
    to 6.2 s, the second read 7 to 8 s."""
    record = _base(ROW_STATUS, daemon=daemon)
    record.update(command=["--status"], terminal=False)
    record["before_status"] = _point(1_000, 1_200, alive=daemon)
    record["after_status"] = _point(6_000, 6_200, alive=daemon)
    record["roots_before"] = {"roots": [[7777, 1000.0]], "seen_ns": 1_300 * MS}
    record["roots_after"] = {"roots": [[7777, 1000.0]], "seen_ns": 6_300 * MS}
    lines = (
        [(4_000, STATUS_HELD), (4_100, STATUS_SHARED)]
        if daemon
        else [
            (
                4_000,
                f"❌ Could not validate session: Another client {FROZEN_CONTENTION}.",
            )
        ]
    )
    record["status"] = _command(
        "status",
        ["--status"],
        lines,
        terminal=False,
        started=2_000,
        exited=5_000,
        code=1,
    )
    record["calls"].append(_feed(7_000, 8_000))
    record["requests"].append(_request("/feed/", 7_500))
    if daemon:
        record["owner_after_read"] = _owner(9_000)
    return record


def _add_line(record: dict, name: str, ms: int, line: str) -> None:
    record[name]["lines"].append([ms * MS, line])


# --- Verdicts: H-R10a ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "daemon"),
    [
        (ROW_LOGOUT, True),
        (ROW_LOGOUT, False),
        (ROW_DECLINE, True),
        (ROW_NO_TERMINAL, True),
        (ROW_BUSY, True),
        (ROW_BUSY, False),
        (ROW_STATUS, True),
        (ROW_STATUS, False),
    ],
)
def test_each_valid_record_has_no_problem(row, daemon):
    build: dict[str, Callable[..., dict]] = {
        ROW_LOGOUT: lambda **k: _logout(**k),
        ROW_DECLINE: lambda **k: _logout(ROW_DECLINE, **k),
        ROW_NO_TERMINAL: lambda **k: _logout(ROW_NO_TERMINAL, **k),
        ROW_BUSY: _busy,
        ROW_STATUS: _status,
    }
    record = build[row](daemon=daemon)
    assert problems_for(record, daemon=daemon) == []
    # Every record survives the packet's JSON as it is.
    assert problems_for(json.loads(json.dumps(record)), daemon=daemon) == []


def _findings(problems: list[str]) -> list[str]:
    return [p for p in problems if not p.startswith(INVALID)]


def test_a_retirement_request_before_the_confirmation_fails_the_logout():
    record = _logout()
    # The owner was gone at the checkpoint before the answer, and its log
    # says a profile command asked.
    record["before_answer"] = _point(4_100, 4_300, alive=False, lock=FREE)
    problems = h_r10a_problems(record, daemon=True)
    assert any("retired before the user confirmed" in p for p in _findings(problems))


def test_a_retirement_reported_before_the_confirmation_fails_the_logout():
    record = _logout()
    record["logout"]["lines"].insert(2, [4_200 * MS, RETIRING_LINE])
    problems = h_r10a_problems(record, daemon=True)
    assert any("before the user confirmed it" in p for p in _findings(problems))


def test_a_retirement_answer_typed_before_its_prompt_is_invalid_evidence():
    record = _logout()
    record["logout"]["answers"][1]["answered_ns"] = 3_900 * MS
    problems = h_r10a_problems(record, daemon=True)
    assert any(
        "not confirmed after its prompt" in p for p in invalid_evidence(problems)
    )


def test_a_logout_that_never_asks_to_retire_the_recorded_owner_fails():
    record = _logout()
    record["logout"]["expected"].pop()
    record["logout"]["answers"].pop()
    problems = h_r10a_problems(record, daemon=True)
    assert any("never asked to retire" in p for p in _findings(problems))


@pytest.mark.parametrize(
    ("change", "expected", "finding"),
    [
        (
            lambda r: r.update(owner_lines={"standing_down": 0, "idle_exit": 1}),
            "idled out",
            False,
        ),
        (
            lambda r: r["owner_exit"].update(how="still running"),
            "not shown to exit",
            True,
        ),
        (lambda r: r["owner_lines"].update(standing_down=0), "does not say once", True),
        (lambda r: r["logout"]["lines"].pop(), "did not clear", True),
        (
            lambda r: r.update(before_answer=_point(4_100, 4_300, lock=FREE)),
            "holding the profile",
            False,
        ),
        (
            lambda r: r.update(before_answer=_point(100, 200)),
            "between the retirement prompt",
            False,
        ),
        (
            lambda r: r["owner_processes"].append(_lifetime(4545, 1003.0)),
            "another owner",
            True,
        ),
        (lambda r: r.update(authorized=None), "confirmation was not recorded", False),
        (
            lambda r: r["logout"].update(ended_by_harness=True),
            "ended by the harness",
            False,
        ),
        (lambda r: r["logout"]["lines"].pop(0), "did not see a terminal", False),
        (lambda r: r.update(left_running=["logout"]), "harness failure", False),
    ],
)
def test_the_confirmed_logout_holds_each_of_its_observations(change, expected, finding):
    record = _logout()
    change(record)
    problems = h_r10a_problems(record, daemon=True)
    chosen = _findings(problems) if finding else invalid_evidence(problems)
    assert any(expected in p for p in chosen), problems


def test_a_direct_logout_on_a_profile_not_shown_settled_is_invalid():
    record = _logout(daemon=False)
    record["settlement"]["remaining"] = [31337]
    problems = h_r10a_problems(record, daemon=False)
    assert any("not shown settled" in p for p in invalid_evidence(problems))


def test_a_direct_logout_that_asks_to_retire_fails():
    record = _logout(daemon=False)
    _add_line(record, "logout", 4_000, RETIRE_PROMPT)
    assert any("asked to retire" in p for p in h_r10a_problems(record, daemon=False))


def test_a_direct_logout_that_does_not_clear_fails():
    record = _logout(daemon=False)
    record["logout"]["lines"].pop()
    problems = h_r10a_problems(record, daemon=False)
    assert any("the logout did not clear" in p for p in _findings(problems))


@pytest.mark.parametrize("daemon", [True, False])
def test_a_deletion_typed_before_its_prompt_is_invalid_evidence(daemon):
    record = _logout(daemon=daemon)
    record["logout"]["answers"][0]["answered_ns"] = 2_900 * MS
    problems = h_r10a_problems(record, daemon=daemon)
    assert any(
        "deletion was not confirmed after its prompt" in p
        for p in invalid_evidence(problems)
    )


def test_a_checkpoint_long_before_the_retirement_answer_is_invalid_evidence():
    record = _logout()
    # The answer came 11 s after the checkpoint ended, past FRESH_SECONDS.
    record["logout"]["answers"][1]["answered_ns"] = 15_300 * MS
    problems = h_r10a_problems(record, daemon=True)
    assert any("is not fresh" in p for p in invalid_evidence(problems))


def test_declining_that_still_sends_a_retirement_fails():
    record = _logout(ROW_DECLINE)
    record["owner_lines"] = {"standing_down": 1, "idle_exit": 0}
    problems = h_r10a_problems(record, daemon=True)
    assert any("a retirement request reached it" in p for p in _findings(problems))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda r: _add_line(r, "logout", 4_650, CLEARED_LINE),
            "still reported a clear",
        ),
        (lambda r: r["logout"].update(returncode=1), "nothing done and exit 0"),
        (
            lambda r: r.update(owner_after_command=_owner(10_000, alive=False)),
            "not shown kept",
        ),
        (
            lambda r: r.update(after_command=_point(9_800, 9_900, lock=FREE)),
            "no longer held",
        ),
    ],
)
def test_declining_holds_each_of_its_observations(change, expected):
    record = _logout(ROW_DECLINE)
    change(record)
    assert any(expected in p for p in h_r10a_problems(record, daemon=True))


def test_a_logout_without_a_terminal_that_clears_a_held_profile_fails():
    record = _logout(ROW_NO_TERMINAL)
    record["logout"]["lines"][-1] = [3_300 * MS, f"✅ {CLEARED_LINE}!"]
    record["logout"]["returncode"] = 0
    problems = h_r10a_problems(record, daemon=True)
    assert any("went on beside a profile shown held" in p for p in _findings(problems))


def test_without_a_held_profile_the_ordinary_checks_are_only_recorded():
    record = _logout(ROW_NO_TERMINAL)
    record["before_command"] = _point(1_600, 1_900, lock=FREE)
    record["logout"]["lines"][-1] = [3_300 * MS, f"✅ {CLEARED_LINE}!"]
    record["logout"]["returncode"] = 0
    problems = h_r10a_problems(record, daemon=True)
    assert problems and all(p.startswith(INVALID) for p in problems), problems
    # Windows: the contender cannot answer, and nothing is asserted or invalid.
    record["platform"] = "win32"
    for label in ("before_command", "after_command"):
        record[label]["lock"] = {}
    assert h_r10a_problems(record, daemon=True) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda r: _add_line(r, "logout", 3_250, RETIRE_PROMPT),
            "asked to retire without",
        ),
        (lambda r: r["logout"]["lines"].pop(1), "needs a terminal"),
        (
            lambda r: r.update(owner_lines={"standing_down": 1, "idle_exit": 0}),
            "reached it",
        ),
        (lambda r: _add_line(r, "logout", 3_250, f"{BANNER}0.0.0"), "saw a terminal"),
    ],
)
def test_the_logout_without_a_terminal_holds_each_of_its_observations(change, expected):
    record = _logout(ROW_NO_TERMINAL)
    change(record)
    assert any(expected in p for p in h_r10a_problems(record, daemon=True))


# --- Verdicts: H-R10b -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["logout", "login", "import"])
def test_a_mutation_on_the_busy_path_fails(name):
    record = _busy()
    _add_line(
        record,
        name,
        4_000 + 10_000 * [n for n, _, _ in BUSY_COMMANDS].index(name),
        f"✅ {CLEARED_LINE}!",
    )
    problems = h_r10b_problems(record, daemon=True)
    assert any(
        "changed the profile beside a busy owner" in p for p in _findings(problems)
    )


def test_an_accepted_reply_from_a_busy_owner_that_proceeds_fails():
    record = _busy()
    _add_line(record, "logout", 4_000, RETIRING_LINE)
    problems = h_r10b_problems(record, daemon=True)
    assert any("as an accepted retirement" in p for p in _findings(problems))


@pytest.mark.parametrize(
    ("change", "expected", "finding"),
    [
        (
            lambda r: _add_line(r, "import", 23_800, KEYCHAIN_NOTICE),
            "keychain notice",
            True,
        ),
        (
            lambda r: _add_line(r, "import", 23_800, f"Found 1 {IMPORT_FOUND}"),
            "discovered browser",
            True,
        ),
        (
            lambda r: _add_line(r, "login", 13_800, "owner 4242 is busy"),
            "names the owner",
            True,
        ),
        (lambda r: r["logout"].update(returncode=0), "exit 1", True),
        (lambda r: r["login"]["lines"].pop(), "not refused as busy", True),
        (
            lambda r: r["gates"][1].update(terminal="deadline"),
            "ran out its deadline",
            False,
        ),
        (
            lambda r: r["import"].update(exited_ns=29_000 * MS),
            "while its section was held",
            False,
        ),
        (
            lambda r: r["login"]["answers"][0].update(answered_ns=12_000 * MS),
            "before its section was held",
            False,
        ),
        (lambda r: r["import"].update(read_open=False), "read had ended", False),
        (lambda r: r.update(read_open=True), "did not complete normally", True),
        (
            lambda r: r.update(owner_lines={"standing_down": 1, "idle_exit": 0}),
            "reached it",
            True,
        ),
        (
            lambda r: r.update(
                owner_after_read=_owner(31_000, lifetime=[4545, 1003.0])
            ),
            "not shown kept",
            True,
        ),
    ],
)
def test_the_busy_commands_hold_each_of_their_observations(change, expected, finding):
    record = _busy()
    change(record)
    problems = h_r10b_problems(record, daemon=True)
    chosen = _findings(problems) if finding else invalid_evidence(problems)
    assert any(expected in p for p in chosen), problems


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda r: _add_line(r, "login", 14_000, LOGIN_OPENED), "took the profile"),
        (lambda r: r["import"]["lines"].pop(), "not refused at the lease"),
        (
            lambda r: r["login"].update(interrupted_ns=None),
            "neither waited nor refused",
        ),
        (lambda r: r["logout"]["lines"].pop(), "not refused at the lease"),
    ],
)
def test_the_frozen_commands_follow_their_lease(change, expected):
    record = _busy(daemon=False)
    change(record)
    assert any(expected in p for p in h_r10b_problems(record, daemon=False))


# --- Verdicts: H-R15 ---------------------------------------------------------------------


def test_a_status_that_launches_a_browser_fails():
    record = _status()
    record["roots_after"]["roots"].append([8888, 1010.0])
    problems = h_r15_problems(record, daemon=True)
    assert any("launched by the status" in p for p in _findings(problems))


def test_a_status_that_succeeds_under_proved_contention_fails():
    record = _status()
    record["status"]["lines"] = [[4_000 * MS, "✅ Session is valid (profile: x)"]]
    record["status"]["returncode"] = 0
    problems = h_r15_problems(record, daemon=True)
    assert any("while the profile was shown held" in p for p in _findings(problems))


def test_a_status_that_succeeds_once_the_profile_came_free_is_a_timing_branch():
    record = _status()
    record["status"]["lines"] = [[4_000 * MS, "✅ Session is valid (profile: x)"]]
    record["status"]["returncode"] = 0
    record["after_status"] = _point(6_000, 6_200, alive=False, lock=FREE)
    problems = h_r15_problems(record, daemon=True)
    assert problems and _findings(problems) == [], problems
    assert any("timing branch" in p for p in problems)


@pytest.mark.parametrize(
    ("change", "expected", "finding"),
    [
        (
            lambda r: r["status"]["lines"].pop(),
            "name the recorded shared browser",
            True,
        ),
        (lambda r: r["status"].update(returncode=0), "not 1", True),
        (
            lambda r: r["requests"].append(_request("/feed/", 3_000)),
            "during the status",
            True,
        ),
        (lambda r: r["calls"].pop(), "feed reads", False),
        (
            lambda r: r["calls"][1].update(read_the_post=False),
            "did not return the post",
            True,
        ),
        (
            lambda r: r["status"].update(
                lines=[[3_000 * MS, "No valid source session found at x"]]
            ),
            "staging",
            False,
        ),
        (
            lambda r: r["status"].update(lines=[[3_000 * MS, "Traceback"]]),
            "did not end in contention",
            True,
        ),
        (
            lambda r: r.update(after_status=_point(4_000, 4_100)),
            "do not bracket",
            False,
        ),
        (
            lambda r: r.update(owner_after_read=_owner(9_000, alive=False)),
            "not shown kept",
            True,
        ),
    ],
)
def test_the_status_holds_each_of_its_observations(change, expected, finding):
    record = _status()
    change(record)
    problems = h_r15_problems(record, daemon=True)
    chosen = _findings(problems) if finding else invalid_evidence(problems)
    assert any(expected in p for p in chosen), problems


def test_the_frozen_status_is_judged_by_its_own_wording_and_category():
    record = _status(daemon=False)
    assert status_category(record["status"]) == "contention"
    record["status"]["lines"] = [[4_000 * MS, "Something else entirely"]]
    assert any(
        "did not end in contention" in p for p in h_r15_problems(record, daemon=False)
    )


def test_on_windows_the_status_rests_on_the_browser_root_both_times():
    record = _status()
    record["platform"] = "win32"
    for label in ("before_status", "after_status"):
        record[label]["lock"] = {}
    assert h_r15_problems(record, daemon=True) == []
    record["roots_before"]["roots"] = []
    record["roots_after"]["roots"] = []
    assert any("not shown held" in p for p in h_r15_problems(record, daemon=True))


# --- Comparisons --------------------------------------------------------------------------


def test_a_repeat_reads_as_its_reference_and_an_invalid_one_is_refused():
    reference = _logout()
    assert semantic_differences(reference, copy.deepcopy(reference)) == []
    declined = _logout(ROW_DECLINE)
    declined["row"] = ROW_LOGOUT
    assert semantic_differences(reference, declined) != []
    broken = copy.deepcopy(reference)
    broken["logout"]["returncode"] = None
    [refusal] = semantic_differences(reference, broken)
    assert "repeat record is not valid" in refusal
    assert semantics(_busy())["logout"] == "busy-refused"
    assert semantics(_busy(daemon=False))["login"] == "waited"


def test_k3_is_held_to_k1_only_from_two_valid_records():
    assert comparison_refusals(_logout(daemon=False), _logout()) == []
    assert comparison_refusals(None, _logout())
    invalid = _status()
    invalid["k2"] = None
    assert comparison_refusals(_status(daemon=False), invalid)


# --- Declarations -------------------------------------------------------------------------


def test_every_row_here_is_declared_with_its_session_and_policy():
    for row, case in CASES.items():
        lifecycle = harness.ROWS[row]
        assert lifecycle.commands and lifecycle.recorded and not lifecycle.scenarios
        assert lifecycle.idle_timeout == COMMAND_IDLE_TIMEOUT_SECONDS
        assert lifecycle.expect_session == case.expect_session
        assert lifecycle.k2 == K2_NOT_APPLICABLE
        assert harness.ROW_VERDICTS[row] is problems_for
        assert lifecycle.preservation == (
            MUST_REMAIN_CLEARED if row == ROW_LOGOUT else ORDINARY
        )
    assert CASES[ROW_LOGOUT].expect_session == CLEARED_BY_USER
    assert {r for r, c in CASES.items() if c.expect_session == RETAINED} == {
        ROW_DECLINE,
        ROW_NO_TERMINAL,
        ROW_BUSY,
        ROW_STATUS,
    }


def test_a_cleared_session_may_not_meet_a_repairing_preservation():
    declared = harness.ROWS[ROW_LOGOUT]
    assert lifecycle_problems(ROW_LOGOUT, declared) == []
    assert lifecycle_problems(
        ROW_LOGOUT, dataclasses.replace(declared, preservation=ORDINARY)
    ) == ["a cleared session left to a preservation session that can repair it"]
    assert lifecycle_problems(
        ROW_LOGOUT, dataclasses.replace(declared, expect_session="lost-silent")
    ) == ["an expected session of 'lost-silent'"]
    assert "profile commands with no script to run them" in lifecycle_problems(
        ROW_LOGOUT, dataclasses.replace(declared, script=None)
    )
    assert "profile commands that combine with other scenarios" in lifecycle_problems(
        ROW_LOGOUT, dataclasses.replace(declared, scenarios=True)
    )
    assert lifecycle_problems("H-R1", RowLifecycle()) == []


# --- What stays with the models --------------------------------------------------------------


def _functions(path: Path) -> set[str]:
    """Every ``Class::test`` and module-level test the file defines."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(f"{node.name}::{item.name}")
    return found


@pytest.mark.parametrize("branch", list(MODEL_COVERAGE))
def test_each_branch_left_to_the_models_names_tests_that_exist(branch):
    """A check of the mapping only: never counted as any branch's coverage,
    which comes from the mapped tests' own runs."""
    _row, nodes = MODEL_COVERAGE[branch]
    root = Path(__file__).resolve().parents[2]
    assert nodes
    for node in nodes:
        path, _, name = node.partition("::")
        assert name in _functions(root / path), node


def test_each_mapped_test_models_one_row():
    assert all(len(rows) == 1 for rows in model_coverage.model_rows().values())


def test_the_mapped_tests_count_as_model_coverage_and_nothing_else_does():
    """The accounting marks a mapped test, parametrized or not, as its row's
    model coverage; a test it does not map, or one already counted as a row
    of its own, is left as it was."""

    class _Item:
        def __init__(self, nodeid: str, marked: bool = False) -> None:
            self.nodeid = nodeid
            self.markers = (
                [pytest.mark.differential_row(row="X").mark] if marked else []
            )

        def get_closest_marker(self, name: str):
            found = [m for m in self.markers if m.name == name]
            return found[-1] if found else None

        def add_marker(self, marker) -> None:
            self.markers.append(marker.mark)

    node, row = next(iter(model_coverage.model_rows().items()))
    items = [
        _Item(node),
        _Item(f"{node}[case]"),
        _Item("tests/test_cli_main.py::test_not_mapped"),
        _Item(node, marked=True),
    ]
    accounting.pytest_collection_modifyitems(cast(Any, None), cast(Any, items))
    for item in items[:2]:
        marker = item.get_closest_marker("differential_row")
        assert marker is not None and marker.kwargs == {
            "row": row[0],
            "experiment": "K3",
            "column": "unit",
        }
    assert items[2].get_closest_marker("differential_row") is None
    assert items[3].get_closest_marker("differential_row").kwargs == {"row": "X"}


# --- The session quits a host its script quit only once ----------------------------------


async def test_a_host_the_script_quit_is_not_quit_again(tmp_path):
    server = tmp_path / "stand_in_server.py"
    server.write_text(_STAND_IN_SERVER)
    exits: list[int] = []

    async def after_exit() -> None:
        exits.append(time.monotonic_ns())

    async def script(call, transport) -> None:
        await transport.host_quit()

    session = await run_host_session(
        [sys.executable, str(server), "marker", "normal", str(tmp_path / "die")],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lambda _line: None,
        after_exit=after_exit,
        row_script=script,
    )

    assert session.script_error is None
    assert len(exits) == 1
    assert session.alive_before_quit is True and session.exited_on_quit is True
    assert harness.host_failures(session) == []


# --- The wiring: the real row entry, a stand-in CLI on the real seams ---------------------

#: A stand-in for the CLI, by its arguments: the deletion prompt and a real
#: clear (``clear``), a claimed clear that changes nothing (``keep``), a
#: refusal at the lease; a login that waits for the lease until interrupted,
#: or (``proceed``) says it opened a browser; an import refused at its lease;
#: a frozen status in contention or (``valid``) a checked session.
_FAKE_CLI = r"""
import os, sys, time
from pathlib import Path

args = sys.argv[1:]
mode = Path(__file__).with_name("mode").read_text().strip()
if sys.stdin.isatty() and sys.stdout.isatty():
    print("%(banner)s0.0.0 🔗", flush=True)
profile = Path(os.environ["USER_DATA_DIR"])
if mode == "linger":
    # A child that keeps the command's output open past its exit, for 30s.
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path(__file__).with_name("linger.pid").write_text(str(child.pid))
    sys.exit(0)
if args == ["--logout"]:
    answer = input("%(delete)s").strip().lower()
    if answer not in ("y", "yes"):
        print("❌ Operation cancelled")
        sys.exit(0)
    if mode == "clear":
        from linkedin_mcp_server.profile_claim import ensure_profile_claim
        from linkedin_mcp_server.session_state import clear_auth_state

        ensure_profile_claim(profile, claim_anyway=True)
        assert clear_auth_state(profile)
    if mode in ("clear", "keep"):
        print("✅ %(cleared)s!")
        sys.exit(0)
    raise RuntimeError("%(lease)s. Stop the running server")
if args == ["--login"]:
    print("%(login)s", flush=True)
    if mode == "proceed":
        print("%(opened)s...", flush=True)
    time.sleep(600)
if args[:1] == ["--import-from-browser"]:
    raise RuntimeError("Another client is using the browser, %(imported)s.")
if args == ["--status"]:
    if mode == "valid":
        print("✅ Session is valid (profile: x)")
        sys.exit(0)
    print("❌ Could not validate session: Another client %(contention)s.")
    sys.exit(1)
""" % {
    "banner": BANNER,
    "delete": DELETE_PROMPT,
    "cleared": CLEARED_LINE,
    "lease": LEASE_REFUSAL,
    "login": LOGIN_BANNER,
    "opened": LOGIN_OPENED,
    "imported": IMPORT_LEASE_REFUSAL,
    "contention": FROZEN_CONTENTION,
}


class _QuitOnly:
    """The live host as the row's script sees it: one quit, recorded."""

    def __init__(self) -> None:
        self.quit_done = False

    async def host_quit(self) -> None:
        self.quit_done = True


class _CommandScene(_CalibrationScene):
    """The calibration's scene, with a host the script can quit and the
    stand-in CLI as the row's command line."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cli = self.tmp_path / "cli" / "fake_cli.py"
        self.cli.parent.mkdir()
        self.cli.write_text(_FAKE_CLI)
        self.mode("clear")
        # The profile lease's lock file, as the row's server leaves it.
        (self.tmp_path / "auth" / LOCK_FILE).touch()

    def mode(self, mode: str) -> None:
        (self.cli.parent / "mode").write_text(mode)

    async def host(
        self, *args, after_call=None, tool, arguments, row_script=None, **kw
    ):
        session = harness.HostSession(
            alive_before_quit=True, stdin_closed=True, exited_on_quit=True, exit_code=0
        )
        kw["started"](4242)
        session.tool = await self._call(session, tool, arguments)
        if after_call is not None:
            await after_call()
        if row_script is not None:

            async def call(name, arguments):
                summary = await self._call(session, name, arguments)
                session.scripted.append(summary)
                return summary

            try:
                await row_script(call, _QuitOnly())
            except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                session.script_error = f"{type(exc).__name__}: {exc}"
        session.eof_monotonic_ns = time.monotonic_ns()
        session.exit_seen_monotonic_ns = time.monotonic_ns()
        return session

    async def run_row(self, row: str):
        return await measure_host_quit_row(
            profile=self.tmp_path / "auth" / "profile",
            experiment="K1",
            daemon=False,
            egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
            log=self.log,
            work_dir=self.tmp_path / "row",
            row=row,
            command=[sys.executable, str(self.cli)],
        )


@pytest.fixture
def scene(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _CommandScene(monkeypatch, tmp_path, profile, origin, certificates)


@posix
async def test_a_confirmed_logout_clears_and_nothing_repairs_it(scene):
    result = await scene.run_row(ROW_LOGOUT)

    assert result.failures == [], result.report()
    assert result.vector is not None and result.vector.o4_session == CLEARED_BY_USER
    assert set(result.vector.o3_authorized) and result.vector.o3_protected == ()
    scene.preservation.assert_not_awaited()
    assert result.post_quit is not None
    assert result.post_quit.withheld == MUST_REMAIN_CLEARED
    assert result.record is not None and result.record["authorized"] == LOGOUT
    # Every line the command printed went to the row's log as the CLI's.
    events = [
        json.loads(line)
        for line in (scene.log.directory / "events.jsonl").read_text().splitlines()
    ]
    printed = [e for e in events if e["actor"] == "cli" and e["kind"] == "user.output"]
    assert any(CLEARED_LINE in e["line"] for e in printed)


@posix
async def test_a_confirmed_logout_that_clears_nothing_is_not_cleared_by_the_user(
    scene,
):
    scene.mode("keep")
    result = await scene.run_row(ROW_LOGOUT)

    # The confirmation was recorded and the command said it cleared; the
    # artefacts say otherwise, and only they count.
    assert result.record is not None and result.record["authorized"] == LOGOUT
    assert CLEARED_LINE in json.dumps(result.record["logout"]["lines"])
    assert result.vector is not None and result.vector.o4_session != CLEARED_BY_USER
    assert result.failures == ["O4: the session was uncertain, not cleared-by-user"]
    scene.preservation.assert_not_awaited()


@posix
async def test_a_command_the_script_leaves_running_is_ended_and_fails_the_row(
    scene, monkeypatch
):
    async def leaves_it(ctx) -> None:
        assert ctx.commands is not None
        await ctx.commands.start(["--login"], terminal=True, label="login")

    monkeypatch.setitem(
        harness.ROWS,
        ROW_LOGOUT,
        dataclasses.replace(harness.ROWS[ROW_LOGOUT], script=leaves_it),
    )
    result = await scene.run_row(ROW_LOGOUT)

    assert result.record is not None and result.record["left_running"] == ["login"]
    assert any("left the profile command login running" in f for f in result.failures)
    assert any("harness failure" in f for f in result.failures)


@posix
async def test_a_command_whose_output_outlives_it_is_held_and_refuses_the_next_step(
    scene, monkeypatch
):
    async def finishes_it(ctx) -> None:
        assert ctx.commands is not None
        command = await ctx.commands.start(["--logout"], terminal=True, label="logout")
        ctx.record["logout"] = await ctx.commands.finish(command, 30)

    monkeypatch.setitem(
        harness.ROWS,
        ROW_LOGOUT,
        dataclasses.replace(harness.ROWS[ROW_LOGOUT], script=finishes_it),
    )
    scene.mode("linger")
    try:
        result = await scene.run_row(ROW_LOGOUT)
    finally:
        pid = int((scene.cli.parent / "linger.pid").read_text())
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(pid).kill()

    record = result.record
    assert record is not None and record["logout"]["output_ended"] is False
    # Exited, but not settled: held, and nothing measured after it.
    assert any(
        "the profile command logout is not shown settled" in f for f in result.failures
    ), result.failures


@posix
async def test_the_row_measures_beside_a_command_waiting_at_its_prompt(
    scene, monkeypatch
):
    async def checks_beside_it(ctx) -> None:
        assert ctx.commands is not None
        command = await ctx.commands.start(["--logout"], terminal=True, label="logout")
        assert await command.expect(DELETE_PROMPT, 30) is not None
        ctx.record["beside"] = await ctx.checkpoint("beside the prompt")
        command.answer("n")
        ctx.record["logout"] = await ctx.commands.finish(command, 30)

    monkeypatch.setitem(
        harness.ROWS,
        ROW_LOGOUT,
        dataclasses.replace(harness.ROWS[ROW_LOGOUT], script=checks_beside_it),
    )
    result = await scene.run_row(ROW_LOGOUT)

    record = result.record
    assert record is not None and record["script_error"] is None
    # A running command is no unsettled worker: the checkpoint was read.
    assert "error" not in record["beside"], record["beside"]
    assert record["beside"]["lock"]["now"] is not None
    assert record["logout"]["returncode"] == 0 and "left_running" not in record


@posix
@interruptible
async def test_the_frozen_commands_run_inside_their_held_sections(scene, monkeypatch):
    monkeypatch.setattr(profile_commands, "LOGIN_WAIT_SECONDS", 0.5)
    scene.mode("held")
    result = await scene.run_row(ROW_BUSY)

    assert result.failures == [], result.report()
    record = result.record
    assert record is not None
    assert [record[name]["label"] for name, _, _ in BUSY_COMMANDS] == [
        "logout",
        "login",
        "import",
    ]
    assert record["login"]["interrupted_ns"] is not None
    assert [g["released_by"] for g in record["gates"]] == [RELEASED_BY_ROW] * 3
    # The import's discovery was sent to the row's own synthetic browser.
    assert record["import_discovery"]
    scene.preservation.assert_awaited_once()


@posix
@interruptible
async def test_a_frozen_login_that_takes_the_profile_fails_the_row(scene, monkeypatch):
    monkeypatch.setattr(profile_commands, "LOGIN_WAIT_SECONDS", 0.5)
    scene.mode("proceed")
    result = await scene.run_row(ROW_BUSY)

    assert any("took the profile" in f for f in result.failures), result.failures


def _hold_the_lease(scene: _CommandScene) -> Callable[[], None]:
    import fcntl

    path = scene.tmp_path / "auth" / LOCK_FILE
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return lambda: os.close(fd)


@posix
async def test_a_status_in_contention_is_bracketed_and_read_again(scene):
    release = _hold_the_lease(scene)
    try:
        result = await scene.run_row(ROW_STATUS)
    finally:
        release()

    assert result.failures == [], result.report()
    record = result.record
    assert record is not None
    assert [
        record[p]["lock"]["answer"]["state"] for p in ("before_status", "after_status")
    ] == [HELD, HELD]
    assert record["status"]["terminal"] is False


@posix
async def test_a_status_that_checks_a_held_session_fails(scene):
    scene.mode("valid")
    release = _hold_the_lease(scene)
    try:
        result = await scene.run_row(ROW_STATUS)
    finally:
        release()

    assert any("shown held around it" in f for f in result.failures), result.failures


# --- The scripts in daemon mode, on a modelled owner -------------------------------------

#: A stand-in for the candidate's CLI beside a recorded owner, by the mode
#: file next to it: the owner idle (``idle``), retiring on a confirmed
#: request by writing its stand-down line and going; busy (``busy``),
#: answering every confirmed request with the busy refusal; and the status
#: in contention. Without a terminal it says retiring needs one, and the
#: ordinary checks refuse the held profile.
_CANDIDATE_CLI = r"""
import sys
from pathlib import Path

here = Path(__file__).parent
mode = (here / "mode").read_text().strip()
args = sys.argv[1:]
interactive = sys.stdin.isatty() and sys.stdout.isatty()
if interactive:
    print("%(banner)s0.0.0 🔗", flush=True)


def retire():
    if not interactive:
        print(%(terminal)r, flush=True)
        return False
    if input(%(prompt)r).strip().lower() not in ("y", "yes"):
        print("❌ Operation cancelled")
        sys.exit(0)
    if mode == "busy":
        print(%(busy)r)
        sys.exit(1)
    with open(here / "owner.log", "a") as log:
        log.write(%(standing)r + "\n")
    (here / "retired").touch()
    print("ℹ️  %(retiring)s", flush=True)
    return True


if args == ["--logout"]:
    if input(%(delete)r).strip().lower() not in ("y", "yes"):
        print("❌ Operation cancelled")
        sys.exit(0)
    if retire():
        print("✅ %(cleared)s!")
        sys.exit(0)
    raise RuntimeError("%(lease)s. Stop the running server")
if args == ["--login"] or args[:1] == ["--import-from-browser"]:
    retire()
    sys.exit(0)
if args == ["--status"]:
    print(%(held)r)
    print(%(shared)r)
    sys.exit(1)
""" % {
    "banner": BANNER,
    "terminal": NEEDS_TERMINAL_LINE,
    "prompt": "A shared browser may be running. " + RETIRE_PROMPT,
    "busy": BUSY_LINE,
    "standing": profile_commands.STANDING_DOWN_LINE,
    "retiring": RETIRING_LINE,
    "delete": DELETE_PROMPT,
    "cleared": CLEARED_LINE,
    "lease": LEASE_REFUSAL,
    "held": STATUS_HELD,
    "shared": STATUS_SHARED,
}


class _ModelledOwner:
    """The owner the row identified, as files the stand-in CLI writes: its
    log, and the mark it leaves once it retired."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.log = directory / "owner.log"
        self.log.write_text("")

    @property
    def alive(self) -> bool:
        return not (self.directory / "retired").exists()


class _DaemonContext:
    """What a script may touch (``harness.RowContext``), on a modelled owner,
    the stand-in CLI and, for H-R10b and H-R15, a real origin's gates and a
    real host read through it."""

    def __init__(self, row: str, tmp_path: Path, scene: _CalibrationScene | None):
        self.row = row
        self.daemon = True
        self.directory = tmp_path / "candidate-cli"
        self.directory.mkdir()
        (self.directory / "fake_cli.py").write_text(_CANDIDATE_CLI)
        self.owner_model = _ModelledOwner(self.directory)
        self.scene = scene
        self.session = harness.HostSession()
        self.record: dict[str, Any] = _base(row, daemon=True)
        for name in ("owner_identified", "calls", "requests"):
            self.record.pop(name)
        self.gates: list[Any] = []
        self.started: list[TerminalCommand] = []
        self.transport = _QuitOnly()
        scratch = tmp_path / "candidate-scratch"
        scratch.mkdir()
        self.commands = SimpleNamespace(
            start=self._start,
            finish=self._finish,
            settlement=None,
            owner_reading=self._owner_reading,
            owner_log=lambda: self.owner_model.log.read_text().splitlines(),
            owner_exit=self._owner_exit,
            roots=self._roots,
            scratch=scratch,
        )

    def mode(self, mode: str) -> None:
        (self.directory / "mode").write_text(mode)

    def owner(self) -> Any:
        return SimpleNamespace(
            pid=_A[0], create_time=_A[1], instance_id=_A[2], process=None
        )

    def emit(self, *args, **kwargs) -> None:
        pass

    def hold(self, path: str, *, ordinal: int = 1) -> Any:
        assert self.scene is not None
        gate = self.scene.origin.hold(path, ordinal=ordinal)
        self.gates.append(gate)
        return gate

    async def call(self, name: str, arguments: dict) -> dict:
        assert self.scene is not None
        return await self.scene._call(self.session, name, arguments)

    async def checkpoint(self, label: str, *, actor: Any = None) -> dict:
        began = time.monotonic_ns()
        alive = self.owner_model.alive
        return {
            "label": label,
            "began_ns": began,
            "ended_ns": time.monotonic_ns(),
            "actor_alive": [alive, alive],
            "lock": {"answer": {"state": HELD if alive else FREE}},
        }

    async def _start(self, args, *, terminal, label, overrides=None):
        command = TerminalCommand(
            [sys.executable, str(self.directory / "fake_cli.py"), *args],
            args=args,
            env={**os.environ, **profile_commands.COMMAND_ENV, **(overrides or {})},
            cwd=self.directory,
            terminal=terminal,
            label=label,
        )
        command.start()
        self.started.append(command)
        return command

    async def _finish(self, command: TerminalCommand, seconds: float) -> dict:
        await command.wait(seconds)
        command.settled(10)
        return command.record()

    async def _owner_reading(self, label: str) -> dict:
        return {
            **_owner(0, alive=self.owner_model.alive),
            "seen_ns": time.monotonic_ns(),
        }

    async def _owner_exit(self, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        while self.owner_model.alive and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        how = "still running" if self.owner_model.alive else "exited"
        return {"how": how, "seen_ns": time.monotonic_ns(), "seen": time.time()}

    async def _roots(self, label: str) -> dict:
        return {
            "label": label,
            "roots": [[7777, 1000.0]],
            "seen_ns": time.monotonic_ns(),
        }

    def finish_record(self) -> dict:
        """What the row entry adds once the script is done."""
        for command in self.started:
            command.end()
        record = self.record
        record["calls"] = [_feed(1, 2), *self.session.calls]
        record["gates"] = [gate.as_record() for gate in self.gates]
        requests = self.scene.origin.requests if self.scene is not None else []
        record["requests"] = [
            _request("/feed/", 1),
            *(
                {
                    "host": r.host,
                    "path": r.path,
                    "session_valid": r.session_valid,
                    "t": r.t,
                    "monotonic_ns": r.monotonic_ns,
                }
                for r in requests
            ),
        ]
        return json.loads(json.dumps(record))


async def _daemon_row(row: str, tmp_path: Path, *, mode: str, scene=None) -> dict:
    context = _DaemonContext(row, tmp_path, scene)
    context.mode(mode)
    await CASES[row].script(cast(Any, context))
    return context.finish_record()


@posix
async def test_the_confirmed_logout_retires_the_idle_owner_and_clears(tmp_path):
    record = await _daemon_row(ROW_LOGOUT, tmp_path, mode="idle")

    assert h_r10a_problems(record, daemon=True) == []
    assert record["authorized"] == LOGOUT
    assert record["owner_exit"]["how"] == "exited"
    assert semantics(record)["logout"] == "cleared"


@posix
async def test_declining_the_retirement_leaves_the_idle_owner_alone(tmp_path):
    record = await _daemon_row(ROW_DECLINE, tmp_path, mode="idle")

    assert [a["text"] for a in record["logout"]["answers"]] == ["y", "n"]
    assert h_r10a_problems(record, daemon=True) == []


async def test_without_a_terminal_nothing_is_asked_and_the_held_profile_refuses(
    tmp_path,
):
    record = await _daemon_row(ROW_NO_TERMINAL, tmp_path, mode="idle")

    assert h_r10a_problems(record, daemon=True) == []
    assert semantics(record)["logout"] == "lease-refused"


async def test_the_candidate_status_in_contention_names_the_shared_browser(
    tmp_path, scene
):
    record = await _daemon_row(ROW_STATUS, tmp_path, mode="idle", scene=scene)

    assert h_r15_problems(record, daemon=True) == []


@posix
async def test_every_command_beside_a_busy_owner_is_refused_inside_its_hold(
    tmp_path, scene
):
    record = await _daemon_row(ROW_BUSY, tmp_path, mode="busy", scene=scene)

    assert h_r10b_problems(record, daemon=True) == []
    assert {semantics(record)[name] for name, _, _ in BUSY_COMMANDS} == {"busy-refused"}


@posix
async def test_an_owner_that_retires_while_busy_fails_the_busy_row(tmp_path, scene):
    record = await _daemon_row(ROW_BUSY, tmp_path, mode="idle", scene=scene)

    problems = h_r10b_problems(record, daemon=True)
    assert any("as an accepted retirement" in p for p in _findings(problems))
