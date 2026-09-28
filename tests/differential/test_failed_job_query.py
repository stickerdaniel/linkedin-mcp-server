"""H-R11's shim, its placement, its member, and its judgement, without Windows.

The native row is ``test_failed_job_query_row.py`` and runs on Windows CI only.
Here, on every platform:

* the shim's scope: it fails ``IsProcessInJob`` for the one caller it names
  and passes every other call through, recording each failure it plants;
* the observer: it records the two drains' ``TerminateProcess`` calls by
  caller and outcome, and changes nothing about any call;
* the drain's reading, from those records only, and unknown on incomplete
  evidence; the installer fates behind it; the successor behind recovery;
* the **model control**: the baseline's own ``process_tree`` at the pin and
  this checkout's, each loaded as that module, drained against Win32 doubles
  through the shim. The baseline terminates the member of the other Job (the
  known ``!``), the candidate does not and leaves the drain unproved;
* the shim venv, built for real: same product code, the shim ran, the hash;
* the row-private cache: a held-back link is only ever a link;
* the stall host: it holds a request without answering;
* the judgement, through ``judge_row``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from differential.baseline import baseline_file
from differential.harness import (
    RowResult,
    RowVector,
    WallClockMarker,
    close_left_unconfirmed,
    installer_inventory,
    is_installer,
    job_query_observations,
    job_query_problems,
    known_non_installer,
    successor_problems,
    judge_row,
    r11_reading,
    r11_verdict,
    successor_verdict,
)
from differential.job_query import (
    LOST_MARKER,
    ROUTINE_DRAIN,
    SHIM_SHA256,
    SHIM_SOURCE,
    Fate,
    Fates,
    PrivateCache,
    ShimVenv,
    StallHost,
    code_difference,
    drain_reading,
    make_shim_venv,
    private_install,
    reached,
    record_install,
    shim_log,
    shim_namespace,
    filetime_to_unix,
    terminations,
)
from differential.session import LAST_VERSION_FILE, write_synthetic_cookie_file
from differential.test_row_judgement import _healthy
from linkedin_mcp_server import process_tree
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

ROW = "H-R11"


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    return directory, staged


class _Error(Exception):
    """Stands in for ``pywintypes.error``."""


def _identity(handle: Any) -> tuple[int, float]:
    """What the shim reads of a handle: here the handle is the pid, created at 9."""
    return handle, 9.0


def _planted(record: Path, answer: bool = True) -> SimpleNamespace:
    """A ``win32job`` double with the shim installed over it."""
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: answer)
    shim_namespace()["install"](job, _Error, _identity, record)
    return job


def _caller(name: str, module: str):
    """A function called *name* in a module called *module* that asks *job*."""
    source = f"def {name}(job, process, handle):\n    return job.IsProcessInJob(process, handle)\n"
    namespace: dict[str, Any] = {"__name__": module}
    exec(source, namespace)
    return namespace[name]


# --- The shim's scope ---------------------------------------------------------


def test_the_shim_fails_only_the_drains_membership_query(tmp_path):
    record = tmp_path / "reached.jsonl"
    job = _planted(record)
    query = _caller("_in_another_owned_job", "linkedin_mcp_server.process_tree")
    with pytest.raises(_Error):
        query(job, 700, 55)
    (line,) = reached(record)
    assert line["member"] == 700 and line["job"] == 55 and line["pid"] == os.getpid()
    assert line["created"] == 9.0
    assert terminations(record) == []


# --- The observer: the drains' TerminateProcess, recorded and unchanged -------------

_PROCESS_TREE = "linkedin_mcp_server.process_tree"


class _Terminator:
    """A ``win32api`` double: records every real call, answers or raises."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.error = error

    def TerminateProcess(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return "the real answer"


def _observed(record: Path, error: BaseException | None = None) -> _Terminator:
    api = _Terminator(error)
    shim_namespace()["observe"](api, _identity, record)
    return api


def _terminating(name: str, module: str = _PROCESS_TREE):
    """A function called *name* in *module* that terminates through *api*."""
    source = (
        f"def {name}(api, handle, status):\n"
        f"    return api.TerminateProcess(handle, status)\n"
    )
    namespace: dict[str, Any] = {"__name__": module}
    exec(source, namespace)
    return namespace[name]


@pytest.mark.parametrize(
    "caller", ["_drain_adopted_windows_job_members", "_drain_adopted_windows_job"]
)
def test_the_observer_records_a_drains_termination_and_changes_nothing(
    tmp_path, caller
):
    record = tmp_path / "reached.jsonl"
    api = _observed(record)
    assert _terminating(caller)(api, 700, 1) == "the real answer"
    # The real API, once, with exactly the arguments the drain gave.
    assert api.calls == [((700, 1), {})]
    (line,) = terminations(record)
    assert (line["caller"], line["member"], line["created"]) == (caller, 700, 9.0)
    assert line["succeeded"] is True and line["pid"] == os.getpid()
    assert line["began"] <= line["ended"]
    assert reached(record) == []


def test_a_failed_termination_is_raised_unchanged_and_recorded_as_failed(tmp_path):
    record = tmp_path / "reached.jsonl"
    refused = _Error(5, "TerminateProcess", "Access is denied.")
    api = _observed(record, error=refused)
    with pytest.raises(_Error) as raised:
        _terminating("_drain_adopted_windows_job_members")(api, 700, 1)
    assert raised.value is refused
    assert api.calls == [((700, 1), {})]
    (line,) = terminations(record)
    assert line["succeeded"] is False and "Access is denied" in line["error"]


@pytest.mark.parametrize(
    ("name", "module"),
    [
        pytest.param("terminate", _PROCESS_TREE, id="another-function"),
        pytest.param("_drain_adopted_windows_job_members", "elsewhere", id="elsewhere"),
    ],
)
def test_every_other_termination_passes_through_unrecorded(tmp_path, name, module):
    record = tmp_path / "reached.jsonl"
    api = _observed(record)
    assert _terminating(name, module)(api, 700, 1) == "the real answer"
    assert api.calls == [((700, 1), {})]
    assert terminations(record) == []


@pytest.mark.parametrize(
    ("name", "module"),
    [
        # The installer's assignment check asks the same Job the same question.
        pytest.param("_assign_handle", "linkedin_mcp_server.process_tree", id="assign"),
        pytest.param("_in_another_owned_job", "elsewhere", id="same-name-elsewhere"),
    ],
)
def test_every_other_call_gets_the_real_answer(tmp_path, name, module):
    record = tmp_path / "reached.jsonl"
    job = _planted(record, answer=True)
    assert _caller(name, module)(job, 700, 55) is True
    assert reached(record) == []


def test_the_shim_is_one_text_with_one_hash():
    import hashlib

    from differential import job_query

    assert hashlib.sha256(job_query.SHIM_SOURCE.encode()).hexdigest() == SHIM_SHA256


# --- The model control: the baseline's drain and this checkout's --------------


def _module(source: str) -> types.ModuleType:
    """*source* loaded as ``linkedin_mcp_server.process_tree``, apart from the
    imported one, so the shim's frame check sees the name it names."""
    module = types.ModuleType("linkedin_mcp_server.process_tree")
    exec(compile(source, "process_tree.py", "exec"), module.__dict__)
    return module


def _drain(module: types.ModuleType, record: Path, *, adopted: int | None = 123):
    """The routine drain with one member, 700, in the adopted Job and in the
    installer's; the Win32 API answers truthfully except where the shim fails
    it. Returns what was terminated and whether the drain was proved."""
    current = os.getpid()
    terminated: list[int] = []
    clock = SimpleNamespace(now=0.0)

    class ProcessHandle:
        def __init__(self, process: int) -> None:
            self.process = process

        def Close(self) -> None:
            pass

    class Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, process: int) -> ProcessHandle:
            return ProcessHandle(process)

        @staticmethod
        def TerminateProcess(handle: ProcessHandle, status: int) -> None:
            terminated.append(handle.process)

    class Con:
        PROCESS_TERMINATE = 1
        PROCESS_QUERY_LIMITED_INFORMATION = 2

    job = SimpleNamespace(
        JobObjectBasicProcessIdList=3,
        QueryInformationJobObject=lambda handle, info: (
            (current,) if terminated else (current, 700)
        ),
        # Truthful: 700 is in both Jobs.
        IsProcessInJob=lambda process, handle: True,
    )
    shim = shim_namespace()
    shim["install"](job, _Error, lambda handle: (handle.process, 9.0), record)
    api = Api()
    shim["observe"](api, lambda handle: (handle.process, 9.0), record)

    def sleep(seconds: float) -> None:
        clock.now += seconds

    module.__dict__.update(
        _IS_WINDOWS=True,
        _adopted_windows_job=adopted,
        _adopted_windows_gate=None,
        _live_windows_jobs=[SimpleNamespace(job_handle=55)],
        _windows_modules=lambda: (api, Con(), job, object()),
        time=SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    proved = module.drain_browser_process_marker(
        "browser",
        timeout=1.0,
        containment=SimpleNamespace(closed=True, drained=True),
    )
    return terminated, proved


@pytest.mark.differential_row(row=ROW, experiment="K2", column="unit")
def test_the_baseline_terminates_the_member_it_could_not_place(tmp_path):
    record = tmp_path / "reached.jsonl"
    source = baseline_file("linkedin_mcp_server/process_tree.py")
    terminated, proved = _drain(_module(source), record)
    # The known '!': the failed query read as "in no other Job".
    assert terminated == [700]
    assert proved is True
    assert [line["member"] for line in reached(record)] == [700]
    # And the observer names the routine drain as the one that terminated it.
    (line,) = terminations(record)
    assert (line["caller"], line["member"], line["succeeded"]) == (
        ROUTINE_DRAIN,
        700,
        True,
    )


@pytest.mark.differential_row(row=ROW, experiment="K3", column="unit")
def test_the_candidate_neither_terminates_nor_proves_the_drain(tmp_path):
    record = tmp_path / "reached.jsonl"
    source = Path(process_tree.__file__).read_text(encoding="utf-8")
    terminated, proved = _drain(_module(source), record)
    assert terminated == []
    assert proved is False
    assert reached(record) and {line["member"] for line in reached(record)} == {700}
    assert terminations(record) == []


@pytest.mark.differential_row(row=ROW, experiment="K1", column="unit")
def test_without_an_adopted_job_the_query_is_never_reached(tmp_path):
    # Direct: no adopted Job, so the drain has no member to ask about.
    record = tmp_path / "reached.jsonl"
    source = baseline_file("linkedin_mcp_server/process_tree.py")
    terminated, proved = _drain(_module(source), record, adopted=None)
    assert (terminated, proved) == ([], True)
    assert reached(record) == []


# --- The shim venv, built -------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="win32job runs only on Windows")
def test_model_shim_does_not_patch_the_real_job_api():
    win32job = pytest.importorskip("win32job")
    original = win32job.IsProcessInJob
    shim_namespace()
    assert win32job.IsProcessInJob is original


def test_model_shim_does_not_patch_a_windows_job_api(monkeypatch):
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    api = SimpleNamespace(TerminateProcess=lambda handle, status: None)
    original, terminate = job.IsProcessInJob, api.TerminateProcess
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "win32job", job)
        patch.setitem(sys.modules, "win32api", api)
        patch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=_Error))
        patch.setitem(
            sys.modules,
            "win32process",
            SimpleNamespace(GetProcessId=lambda handle: handle),
        )
        shim_namespace()
    assert job.IsProcessInJob is original
    assert api.TerminateProcess is terminate


def _startup(monkeypatch, tmp_path: Path, *, observer: bool = True) -> Path:
    """Run the shim as an actor's ``sitecustomize`` over Windows doubles."""
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    api = SimpleNamespace(TerminateProcess=lambda handle, status: None)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "win32job", job)
        patch.setitem(sys.modules, "win32api", api if observer else None)
        patch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=_Error))
        namespace: dict[str, Any] = {
            "__name__": "sitecustomize",
            "__file__": str(tmp_path / "sitecustomize.py"),
        }
        exec(compile(SHIM_SOURCE, "sitecustomize.py", "exec"), namespace)
    return tmp_path / "h-r11-reached.jsonl"


def test_the_actors_startup_installs_the_fault_and_the_observer(monkeypatch, tmp_path):
    record = _startup(monkeypatch, tmp_path)
    (ready,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert ready["kind"] == "ready" and ready["seq"] == 1
    assert ready["fault"] is True and ready["observer"] is True


def test_an_actor_whose_observer_is_missing_says_so(monkeypatch, tmp_path):
    record = _startup(monkeypatch, tmp_path, observer=False)
    (ready,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert ready["fault"] is True and ready["observer"] is False


# --- The records: complete, or no reading ---------------------------------------


def _actor(record: Path, **ready: Any) -> dict[str, Any]:
    """One actor's shim, over doubles, that has written its ready record."""
    shim = shim_namespace()
    shim["ready"](record, **{"created": 5.0, "fault": True, "observer": True, **ready})
    return shim


def _drain_once(shim: dict[str, Any], record: Path, api_record: Path | None = None):
    """The routine drain asks about 700 and terminates it, through the shim."""
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    shim["install"](job, _Error, _identity, record)
    api = _Terminator()
    shim["observe"](api, _identity, api_record or record)
    with pytest.raises(_Error):
        _caller("_in_another_owned_job", _PROCESS_TREE)(job, 700, 55)
    answer = _terminating("_drain_adopted_windows_job_members")(api, 700, 1)
    return job, api, answer


def _log(record: Path, **kwargs: Any):
    return shim_log(record, pid=os.getpid(), created=5.0, **kwargs)


def test_complete_records_are_read_as_they_were_written(tmp_path):
    record = tmp_path / "reached.jsonl"
    _drain_once(_actor(record), record)
    log = _log(record)
    assert log.problems == []
    assert [line["member"] for line in log.queries] == [700]
    (call,) = log.terminations
    assert (call["caller"], call["member"], call["succeeded"]) == (
        ROUTINE_DRAIN,
        700,
        True,
    )


def test_a_termination_record_that_could_not_be_written_leaves_no_reading(
    tmp_path, capsys
):
    # E1EP-02: the query is written, the termination's records are not. The
    # real API still ran once and answered; the lost records show.
    record, unwritable = tmp_path / "reached.jsonl", tmp_path / "a-directory"
    unwritable.mkdir()
    shim = _actor(record)
    _job, api, answer = _drain_once(shim, record, api_record=unwritable)
    assert answer == "the real answer" and api.calls == [((700, 1), {})]
    lost = capsys.readouterr().err.splitlines()
    assert len([line for line in lost if LOST_MARKER in line]) == 2
    # Announced on stderr, the owner's log, even with nothing written after.
    assert _log(record, lost=lost).problems
    # And a later record shows the numbers it skipped, without the log.
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    shim["install"](job, _Error, _identity, record)
    with pytest.raises(_Error):
        _caller("_in_another_owned_job", _PROCESS_TREE)(job, 700, 55)
    log = _log(record)
    assert any("without a gap" in problem for problem in log.problems)
    reading = drain_reading(
        [Fate(700, 9.0, exit_code=1, kernel_exit=11.0)],
        log.queries,
        log.terminations,
        health=log.problems,
    )
    assert reading.value is None


@pytest.mark.parametrize(
    ("ready", "why"),
    [
        pytest.param({"observer": False}, "not all in place", id="no-observer"),
        pytest.param({"fault": False}, "not all in place", id="no-fault"),
        pytest.param({"created": 4.0}, "0 ready records", id="another-lifetime"),
    ],
)
def test_an_actor_not_shown_fully_shimmed_leaves_no_reading(tmp_path, ready, why):
    record = tmp_path / "reached.jsonl"
    _drain_once(_actor(record, **ready), record)
    assert any(why in problem for problem in _log(record).problems)


def test_no_ready_record_at_all_leaves_no_reading(tmp_path):
    record = tmp_path / "reached.jsonl"
    _drain_once(shim_namespace(), record)
    assert any("0 ready records" in p for p in _log(record).problems)


def test_an_unreadable_line_leaves_no_reading(tmp_path):
    record = tmp_path / "reached.jsonl"
    _drain_once(_actor(record), record)
    with record.open("a") as stream:
        stream.write('{"kind": "terminate", "phase": "beg\n')
    assert any("unreadable" in p for p in _log(record).problems)


def test_an_end_with_no_recorded_start_leaves_no_reading(tmp_path):
    record = tmp_path / "reached.jsonl"
    shim = _actor(record)
    shim["_write"](record, {"kind": "terminate", "phase": "end", "call": 9})
    assert any("no record of its start" in p for p in _log(record).problems)


def test_a_start_with_no_recorded_end_is_an_attempt(tmp_path):
    # The call began and never came back: it may have terminated the member.
    record = tmp_path / "reached.jsonl"
    shim = _actor(record)
    shim["_write"](
        record,
        {
            "kind": "terminate",
            "phase": "begin",
            "call": 1,
            "caller": ROUTINE_DRAIN,
            "member": 700,
            "created": 9.0,
            "t": 1.0,
        },
    )
    log = _log(record)
    assert log.problems == []
    (call,) = log.terminations
    assert call["succeeded"] is None


def test_an_unrelated_sitecustomize_does_not_prove_the_shim_ran(tmp_path):
    source = {"module": "source", "direct_url": {}, "version": "1"}
    shimmed = {**source, "sitecustomize": str(tmp_path / "other.py")}
    assert code_difference(source, shimmed, tmp_path / "sitecustomize.py")


def test_the_shim_venv_probes_code_without_importing_the_working_directory(
    tmp_path, monkeypatch
):
    import linkedin_mcp_server

    shadow = tmp_path / "shadow" / "linkedin_mcp_server"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("shadow = True\n")
    monkeypatch.chdir(shadow.parent)
    venv = make_shim_venv(sys.executable, tmp_path / "venv")
    assert (
        Path(venv.code["module"]).resolve()
        == Path(linkedin_mcp_server.__file__).resolve()
    )


def test_the_shim_venv_runs_the_source_code_and_the_shim(tmp_path):
    venv = make_shim_venv(sys.executable, tmp_path / "venv")
    assert venv.code["module"] == venv.source_code["module"]
    assert venv.code["direct_url"] == venv.source_code["direct_url"]
    assert Path(venv.code["sitecustomize"]).parent == Path(venv.site_packages)
    assert venv.shim_sha256 == SHIM_SHA256
    installed = Path(venv.site_packages, "sitecustomize.py").read_text(encoding="utf-8")
    assert installed == SHIM_SOURCE
    # Windows startup also writes the declared observer-ready record.
    added = {p.name for p in Path(venv.site_packages).iterdir()} - {"__pycache__"}
    expected = {"sitecustomize.py", "_h_r11_code.pth"}
    if sys.platform == "win32":
        expected.add("h-r11-reached.jsonl")
        ready = [
            json.loads(line) for line in venv.reached_file.read_text().splitlines()
        ]
        assert ready and all(
            line["kind"] == "ready" and line["fault"] and line["observer"]
            for line in ready
        )
    assert added == expected


# --- The row-private cache ------------------------------------------------------


def test_the_private_cache_only_ever_removes_links(tmp_path):
    store = tmp_path / "store"
    sources = [store / "chromium-1", store / "ffmpeg-2"]
    for source in sources:
        source.mkdir(parents=True)
        (source / "INSTALLATION_COMPLETE").write_text("")
    cache = PrivateCache.build(tmp_path / "private", sources)
    held = cache.hold_back()
    assert held.name == "ffmpeg-2" and not held.exists()
    # A download patchright left in the held-back place is removed on restore.
    held.mkdir()
    (held / "partial").write_text("x")
    cache.restore()
    assert cache.removed == [str(held)]
    (cache.directory / ".links").mkdir()
    cache.dismantle()
    assert not cache.directory.exists()
    assert all((source / "INSTALLATION_COMPLETE").is_file() for source in sources)


def test_a_partial_private_cache_build_leaves_its_sources_untouched(tmp_path):
    source = tmp_path / "store" / "chromium-1"
    source.mkdir(parents=True)
    (source / "INSTALLATION_COMPLETE").write_text("")
    private = tmp_path / "private"
    with pytest.raises(RuntimeError, match="not installed"):
        PrivateCache.build(private, [source, tmp_path / "missing"])
    assert not private.exists()
    assert (source / "INSTALLATION_COMPLETE").is_file()


# --- The stall host ----------------------------------------------------------------


def test_the_stall_host_holds_a_request_unanswered():
    host = StallHost().start()
    try:
        port = int(host.url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"GET /builds/x.zip HTTP/1.1\r\nHost: x\r\n\r\n")
            client.settimeout(0.5)
            with pytest.raises(TimeoutError):
                client.recv(1)
    finally:
        host.stop()
    assert host.connections == 1


# --- Observations and judgement ------------------------------------------------------


def _shim(tmp_path: Path) -> ShimVenv:
    return ShimVenv(
        directory=tmp_path,
        python=sys.executable,
        source_python=sys.executable,
        site_packages=str(tmp_path),
        shim_sha256=SHIM_SHA256,
        pth_sha256="",
        source_code={},
        code={},
    )


# The shapes run 36381621588 measured on Windows, seconds past 1790573600.
#: K2: the baseline drain asked about 7596 and ended it 6 ms later, mid-close.
_K2_QUERY, _K2_TERMINATE, _K2_EXIT = 73.749, 73.750, 73.755
#: K3: the candidate drain asked about the member for its whole 10 s deadline;
#: the owner's shutdown ended it with the same code 211 ms after the close.
_K3_QUERIES, _K3_EXIT = (130.901, 140.904), 141.121


def _ended(**fields: Any) -> Fate:
    """Installer 700, created at 9, observed to end with code 1."""
    base: dict[str, Any] = dict(exit_code=1, kernel_exit=_K2_EXIT, exited_at=73.9)
    base.update(fields)
    return Fate(700, 9.0, **base)


def _query(t: float = _K2_QUERY, member: int = 700, created: float = 9.0) -> dict:
    return {"kind": "query", "pid": 42, "t": t, "member": member, "created": created}


def _terminate(
    caller: str = ROUTINE_DRAIN,
    *,
    succeeded: bool | None = True,
    began: float = _K2_TERMINATE,
    member: int = 700,
    created: float = 9.0,
) -> dict:
    return {
        "kind": "terminate",
        "pid": 42,
        "caller": caller,
        "member": member,
        "created": created,
        "began": began,
        "ended": began + 0.001,
        "succeeded": succeeded,
    }


def test_the_routine_drain_terminating_a_member_it_could_not_place_reads_bang():
    reading = drain_reading([_ended()], [_query()], [_terminate()])
    assert reading.value is True
    assert [fate.pid for fate in reading.terminated] == [700]


def test_the_same_code_at_shared_shutdown_is_not_the_drains():
    # K3: the member ends with code 1 while the candidate's owner shuts its
    # setup down, and no routine drain terminated it: '='.
    reading = drain_reading(
        [_ended(kernel_exit=_K3_EXIT)], [_query(t) for t in _K3_QUERIES], []
    )
    assert reading.value is False and reading.unknown == []


def test_the_hard_exit_drain_is_not_the_routine_bang():
    # The baseline's hard exit terminates every member after the close.
    line = _terminate("_drain_adopted_windows_job")
    assert drain_reading([_ended()], [_query()], [line]).value is False


def test_a_failed_termination_is_an_attempt_never_a_termination():
    # E1EU-02: a forbidden act, so never '=', and no calibration either.
    reading = drain_reading([_ended()], [_query()], [_terminate(succeeded=False)])
    assert reading.value is None and reading.terminated == []
    assert [line["member"] for line in reading.attempted] == [700]
    assert reading.unknown == []


def test_a_success_on_an_installer_whose_end_was_not_observed_confirms_nothing():
    reading = drain_reading(
        [_ended(exit_code=None, kernel_exit=None, exited_at=None)],
        [_query()],
        [_terminate()],
    )
    assert reading.value is None and reading.terminated == []


def test_a_termination_with_no_recorded_end_is_unknown():
    # E1EU-02: the begin is written before the call, which may never have run.
    reading = drain_reading([_ended()], [_query()], [_terminate(succeeded=None)])
    assert reading.value is None and reading.attempted == []
    assert any("no end was recorded" in unknown for unknown in reading.unknown)


def test_a_confirmed_termination_beside_an_attempt_is_the_witness():
    # Run 36410976409, K2: some calls failed, others terminated installers.
    other = Fate(701, 9.5, exit_code=1, kernel_exit=_K2_EXIT)
    reading = drain_reading(
        [_ended(), other],
        [_query(), _query(member=701, created=9.5)],
        [_terminate(), _terminate(member=701, created=9.5, succeeded=False)],
    )
    assert reading.value is True
    assert [f.pid for f in reading.terminated] == [700]


def test_a_known_row_process_the_drain_terminated_is_an_act_not_a_witness():
    # Run 36410976409, K2: the drain also asked about, and terminated, the
    # owner's gate and console host, row processes that are no installer.
    gate = dict(_query(member=3572, created=1.0))
    reading = drain_reading(
        [_ended()],
        [_query(), gate],
        [_terminate(member=3572, created=1.0)],
        known_other=lambda member, created: (member, created) == (3572, 1.0),
    )
    assert reading.unknown == [] and reading.value is None
    assert [line["member"] for line in reading.others] == [3572]
    assert reading.acts() == ["terminated row process 3572"]
    # Asked about and left alone, it changes nothing.
    left = drain_reading(
        [_ended()],
        [_query(), gate],
        [],
        known_other=lambda member, created: (member, created) == (3572, 1.0),
    )
    assert left.unknown == [] and left.value is False


def test_the_records_own_gaps_leave_no_reading():
    reading = drain_reading([_ended()], [_query()], [], health=["a record was lost"])
    assert reading.value is None and reading.unknown == ["a record was lost"]


def test_an_unsettled_installer_the_drain_never_touched_is_the_inventorys():
    # Its end matters for the barrier (installer_inventory), not the reading.
    reading = drain_reading([_ended(kernel_exit=None, exit_code=None)], [], [])
    assert reading.value is False


@pytest.mark.parametrize("received", [140.910, 141.200, 300.0])
def test_a_delayed_reply_moves_no_reading(tmp_path, received):
    # E1EN-02's control: the host's receipt of the close reply moved past the
    # shared shutdown's termination turned '=' into '!' when it bounded the
    # drain. Now the reading has no host time in it at all.
    reading = drain_reading(
        [_ended(kernel_exit=_K3_EXIT)], [_query(t) for t in _K3_QUERIES], []
    )
    observed = job_query_observations(
        _shim(tmp_path),
        reading=reading,
        window={"began": 130.632, "ended": received},
        owner_pid=42,
        daemon=True,
    )
    assert observed["job_member_terminated"] is False


@pytest.mark.parametrize(
    ("fate", "queried", "terminated", "why"),
    [
        pytest.param(
            _ended(exit_code=None, kernel_exit=None, exited_at=None),
            [_query()],
            [],
            "no exit was observed",
            id="still-running",
        ),
        pytest.param(
            _ended(
                exit_code=None,
                kernel_exit=None,
                exited_at=None,
                problem="its wait failed: PermissionError()",
            ),
            [_query()],
            [],
            "its wait failed",
            id="wait-failed",
        ),
        pytest.param(
            _ended(kernel_exit=None),
            [_query()],
            [],
            "no exit was observed",
            id="no-kernel-exit",
        ),
        pytest.param(
            _ended(),
            [_query(created=5.0)],
            [],
            "neither watched nor knows",
            id="another-lifetime",
        ),
        pytest.param(
            _ended(),
            [_query(), _query(member=701)],
            [],
            "neither watched nor knows",
            id="an-unwatched-member",
        ),
        pytest.param(
            _ended(),
            [_query()],
            [_terminate(member=701)],
            "cannot place",
            id="terminated-unwatched",
        ),
        pytest.param(
            _ended(),
            [_query(t=_K2_TERMINATE + 1)],
            [_terminate()],
            "without the planted query",
            id="terminated-before-asked",
        ),
        pytest.param(
            _ended(exit_code=0),
            [_query()],
            [_terminate()],
            "ended with 0",
            id="terminated-but-ended-otherwise",
        ),
    ],
)
def test_incomplete_evidence_is_no_reading(fate, queried, terminated, why):
    reading = drain_reading([fate], queried, terminated)
    assert reading.value is None
    assert any(why in unknown for unknown in reading.unknown), reading.unknown


class _Native:
    """``NativeProcess`` for one pid, with the answers a test chooses."""

    def __init__(
        self,
        *,
        created: float | None = 9.0,
        wait: BaseException | None = None,
        code: int = 1,
        exited: float | None = 11.0,
        opens: bool = True,
    ) -> None:
        self._created, self._wait, self._code = created, wait, code
        self._exited, self._opens = exited, opens
        self.closed = 0

    def open(self, pid):
        return object() if self._opens else None

    def created(self, handle):
        return self._created

    def wait(self, handle):
        if self._wait is not None:
            raise self._wait

    def exit_code(self, handle):
        return self._code

    def exited(self, handle):
        return self._exited

    def close(self, handle):
        self.closed += 1


def _watched(native: _Native) -> Fates:
    fates = Fates(native)
    fates.watch(700, 9.0)
    fates.settle(5.0)
    return fates


def test_an_exit_is_read_from_the_handle_the_lifetime_was_bound_to():
    fates = _watched(_Native())
    (fate,) = fates.fates.values()
    assert fate.settled and (fate.exit_code, fate.kernel_exit) == (1, 11.0)
    assert fates.alive() == [] and fates.unsettled() == []


@pytest.mark.parametrize(
    ("native", "why"),
    [
        pytest.param(_Native(wait=PermissionError()), "wait failed", id="wait"),
        pytest.param(_Native(exited=None), "no exit time", id="no-exit-time"),
        pytest.param(_Native(created=5.0), "created at 5.0", id="another-lifetime"),
        pytest.param(_Native(opens=False), "could not be opened", id="no-handle"),
    ],
)
def test_an_unobserved_end_is_unknown_never_an_exit_or_a_survivor(native, why):
    fates = _watched(native)
    (fate,) = fates.fates.values()
    assert not fate.settled and why in (fate.problem or "")
    assert fate.exited_at is None and fate.exit_code is None
    # Neither alive nor ended: unknown, and so no reading.
    assert fates.alive() == [] and fates.unsettled() == [fate]
    assert drain_reading(fates.fates.values(), [_query()], []).value is None


@pytest.mark.parametrize(
    "native",
    [_Native(opens=False), _Native(created=5.0)],
    ids=["gone", "another-lifetime"],
)
def test_a_process_that_could_not_be_watched_is_kept_as_unknown(native):
    # E1EP-03: dropped, it would leave the inventory with nothing to account
    # for; kept, it is unknown until something shows it ended.
    (fate,) = _watched(native).fates.values()
    assert fate.problem is not None and not fate.settled


def _reach(tmp_path: Path, pid: int) -> None:
    with (tmp_path / "h-r11-reached.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(_query(), pid=pid)) + "\n")


def test_the_reading_is_what_the_row_feeds_its_judgement(tmp_path):
    _reach(tmp_path, 42)
    reading = drain_reading([_ended()], [_query()], [_terminate()])
    observed = job_query_observations(
        _shim(tmp_path),
        reading=reading,
        window={"successor_verified": True},
        owner_pid=42,
        daemon=True,
    )
    assert observed["job_query_reached"] and observed["job_member_terminated"]
    assert observed["successor_before_quit"] is True
    unknown = drain_reading([_ended(kernel_exit=None)], [_query()], [])
    observed = job_query_observations(
        _shim(tmp_path), reading=unknown, window={}, owner_pid=42, daemon=True
    )
    assert observed["job_member_terminated"] is None
    assert observed["successor_before_quit"] is False


def test_a_planted_failure_in_another_process_is_not_the_owners(tmp_path):
    _reach(tmp_path, 99)
    observed = job_query_observations(
        _shim(tmp_path), reading=None, window={}, owner_pid=42, daemon=True
    )
    assert observed["job_query_reached"] is False


def test_a_filetime_reads_on_the_unix_clock():
    assert filetime_to_unix(116_444_736_000_000_000) == 0.0
    assert filetime_to_unix(116_444_736_000_000_000 + 15_000_000) == 1.5


# --- The successor: served, after the closing owner left, before host quit -------

_SERVED = {"is_error": False, "read_the_post": True}


@pytest.mark.parametrize(
    ("probe", "left", "why"),
    [
        pytest.param(None, True, "no probe", id="no-probe"),
        pytest.param(
            {"is_error": True, "read_the_post": False}, True, "did not read", id="error"
        ),
        pytest.param(
            {"is_error": False, "read_the_post": False},
            True,
            "did not read",
            id="no-post",
        ),
        pytest.param(_SERVED, False, "not confirmed gone", id="closer-stayed"),
        pytest.param(_SERVED, None, "not confirmed gone", id="closer-unasked"),
    ],
)
def test_recovery_needs_a_served_probe_after_the_closer_left(probe, left, why):
    problems = successor_verdict(probe=probe, left=left, problems=[])
    assert any(why in problem for problem in problems)


def test_a_served_probe_after_the_closer_left_still_needs_the_new_owner():
    assert successor_verdict(probe=_SERVED, left=True, problems=[]) == []
    assert successor_verdict(probe=_SERVED, left=True, problems=None) == [
        "the successor was never looked for"
    ]
    assert successor_verdict(
        probe=_SERVED, left=True, problems=["no browser descends from 43"]
    ) == ["no browser descends from 43"]


def _fates(*fates: Fate) -> Fates:
    tracked = Fates(_Native())
    for fate in fates:
        tracked.fates[(fate.pid, fate.start)] = fate
    return tracked


def test_the_problems_that_keep_the_row_from_observing(tmp_path):
    complete = drain_reading([_ended()], [_query()], [])
    assert (
        job_query_problems(
            _shim(tmp_path),
            fates=_fates(_ended()),
            reading=complete,
            window={"script_ended": True},
            script_error=None,
        )
        == []
    )
    unknown = drain_reading([_ended(kernel_exit=None)], [_query()], [])
    problems = job_query_problems(
        _shim(tmp_path),
        fates=_fates(),
        reading=unknown,
        window={},
        script_error="TimeoutError: probe",
    )
    assert any("script failed" in p for p in problems)
    assert any("no installer ran" in p for p in problems)
    assert any("no exit was observed" in p for p in problems)
    assert any("did not run to its end" in p for p in problems)


def test_the_installer_is_every_process_of_it():
    assert is_installer({"actor": "installer", "cmdline": ["x"]})
    assert is_installer(
        {"cmdline": ["python", "-P", "-m", "patchright", "install", "chromium"]}
    )
    assert is_installer({"cmdline": ["node", "lib/entry/oopBrowserDownload.js"]})
    assert not is_installer({"cmdline": ["python", "-m", "linkedin_mcp_server"]})


def _vector(profile, *, daemon: bool, **changes) -> RowVector:
    base = dict(
        failed_job_query=True,
        job_query_reached=True,
        job_member_terminated=False,
        successor_before_quit=True,
    )
    base.update(changes)
    observed = dataclasses.replace(_healthy(profile, daemon=daemon), **base)
    vector, _ = judge_row(observed)
    return vector


def _result(vector: RowVector, observation: tuple[str, ...] = ()) -> RowResult:
    return RowResult(
        experiment="X",
        mode=vector.mode,
        vector=vector,
        observation_failures=list(observation),
    )


def test_a_daemon_row_that_never_reached_the_query_fails(profile):
    observed = dataclasses.replace(
        _healthy(profile, daemon=True),
        failed_job_query=True,
        job_query_reached=False,
        job_member_terminated=False,
    )
    _, failures = judge_row(observed)
    assert any("never reached" in failure for failure in failures)


def test_k2_must_read_bang(profile):
    bang = _vector(profile, daemon=True, job_member_terminated=True)
    assert r11_reading(_result(bang)) == "!"
    assert r11_verdict(_result(bang), experiment="K2", non_windows=False) == []
    equal = _vector(profile, daemon=True, job_member_terminated=False)
    problems = r11_verdict(_result(equal), experiment="K2", non_windows=False)
    assert any("harness defect" in problem for problem in problems)
    unknown = _vector(profile, daemon=True, job_member_terminated=None)
    problems = r11_verdict(_result(unknown), experiment="K2", non_windows=False)
    assert any("harness defect" in problem for problem in problems)


@pytest.mark.parametrize("experiment", ["K1", "K2", "K3"])
def test_a_failure_to_observe_fails_every_experiment(profile, experiment):
    # K2 keeps its known behavioural '!', never evidence the harness could not
    # complete or clean up after.
    vector = _vector(
        profile,
        daemon=experiment != "K1",
        job_member_terminated=experiment == "K2",
        job_query_reached=experiment != "K1",
    )
    assert r11_verdict(_result(vector), experiment=experiment, non_windows=False) == []
    failed = _result(vector, ("teardown: the private browser cache stayed",))
    assert "teardown: the private browser cache stayed" in r11_verdict(
        failed, experiment=experiment, non_windows=False
    )


def test_k3_must_read_equal_and_elect_a_successor(profile):
    good = _vector(profile, daemon=True)
    assert r11_verdict(_result(good), experiment="K3", non_windows=False) == []
    bang = _vector(profile, daemon=True, job_member_terminated=True)
    assert any(
        "terminated" in p
        for p in r11_verdict(_result(bang), experiment="K3", non_windows=False)
    )
    unknown = _vector(profile, daemon=True, job_member_terminated=None)
    assert any(
        "no complete reading" in p
        for p in r11_verdict(_result(unknown), experiment="K3", non_windows=False)
    )
    alone = _vector(profile, daemon=True, successor_before_quit=False)
    assert any(
        "no successor" in p
        for p in r11_verdict(_result(alone), experiment="K3", non_windows=False)
    )


def test_k1_must_not_reach_the_query(profile):
    good = _vector(
        profile, daemon=False, job_query_reached=False, successor_before_quit=None
    )
    assert r11_verdict(_result(good), experiment="K1", non_windows=False) == []
    reached_it = _vector(profile, daemon=False, job_query_reached=True)
    assert any(
        "no adopted Job" in p
        for p in r11_verdict(_result(reached_it), experiment="K1", non_windows=False)
    )


# --- Placing the drain's members: installers, known row processes, or unknown ------


def _seen(pid, ppid, start, actor, cmdline=("python",), *, t=None, in_row=True):
    return {
        "kind": "process.start",
        "t": start + 0.02 if t is None else t,
        "pid": pid,
        "ppid": ppid,
        "pgid": pid,
        "start_identity": start,
        "in_row": in_row,
        "actor": actor,
        "cmdline": list(cmdline),
    }


def _gone(pid, start, t):
    return {"kind": "process.exit", "t": t, "pid": pid, "start_identity": start}


#: Run 36410976409, K2, as the watcher recorded it: the frontend's launcher
#: (6076, a child of the harness, which also started the row's canaries) and
#: the frontend (6928), the owner's release gate (3572), its Python child
#: (7708) and that child's console host (1276), eight seconds before the
#: installer's supervisor (880) and a worker below it. Here the harness is
#: this process.
_K2_ROW = [
    _seen(6076, os.getpid(), 678.2, "frontend", ("venv\\python.exe", "-m", "x")),
    _seen(6928, 6076, 678.25, "frontend", ("venv\\python.exe", "-m", "x")),
    _seen(3572, 6928, 678.378, "owner", ("venv\\python.exe", "-I", "-S", "-u")),
    _seen(7708, 3572, 678.383, "owner", ("venv\\python.exe", "-I", "-S", "-u")),
    _seen(1276, 7708, 678.386, "other", ("conhost.exe", "0x4")),
    _seen(880, 3860, 686.896, "installer"),
    _seen(4040, 880, 687.182, "other", ("python", "-m", "patchright", "install")),
    # A child of the installer that is no installer by its own record.
    _seen(4100, 880, 687.300, "other", ("conhost.exe", "0x4")),
]


@pytest.mark.parametrize(
    ("member", "created", "known"),
    [
        pytest.param(3572, 678.378, True, id="owner-gate"),
        pytest.param(7708, 678.383, True, id="owner-child"),
        pytest.param(1276, 678.386, True, id="console-host"),
        pytest.param(880, 686.896, False, id="the-installer"),
        pytest.param(4040, 687.182, False, id="an-installer-by-its-command"),
        pytest.param(4100, 687.300, False, id="an-installers-descendant"),
        pytest.param(3572, 600.0, False, id="another-lifetime-at-the-pid"),
        pytest.param(9999, 678.0, False, id="never-recorded"),
    ],
)
def test_only_a_recorded_row_process_outside_every_installer_is_placed(
    member, created, known
):
    assert known_non_installer(_K2_ROW)(member, created) is known


@pytest.mark.parametrize(
    "missing", [6076, 6928, 3572], ids=["launcher", "frontend", "gate"]
)
def test_a_parent_nobody_recorded_places_nothing(missing):
    # E1EU-03: with any link of the chain unrecorded, the console host could
    # sit below an installer; that is unknown, never a known row process.
    records = [r for r in _K2_ROW if r["pid"] != missing]
    assert known_non_installer(records)(1276, 678.386) is False


def test_a_link_outside_the_row_places_nothing():
    # The frontend recorded, but not as one of the row's own processes: the
    # chain does not reach the harness through the row.
    records = [dict(r, in_row=False) if r["pid"] == 6928 else r for r in _K2_ROW]
    assert known_non_installer(records)(1276, 678.386) is False


def test_the_measured_k2_members_read_bang_once_placed():
    # The three the run could not place are known row processes; the reading
    # is then complete, and the routine drain's terminations of the installer
    # make it '!'.
    fates = [
        Fate(880, 686.896, exit_code=1, kernel_exit=690.937),
        Fate(4040, 687.182, exit_code=1, kernel_exit=690.949),
    ]
    queries = [
        {"t": 690.9, "member": m, "created": c}
        for m, c in [(3572, 678.378), (7708, 678.383), (1276, 678.386)]
        + [(880, 686.896), (4040, 687.182)]
    ]
    stopped = [
        dict(_terminate(member=3572, created=678.378, began=690.92)),
        dict(_terminate(member=880, created=686.896, began=690.93)),
        dict(_terminate(member=4040, created=687.182, began=690.94)),
    ]
    reading = drain_reading(
        fates, queries, stopped, known_other=known_non_installer(_K2_ROW)
    )
    assert reading.unknown == [] and reading.value is True
    assert sorted(f.pid for f in reading.terminated) == [880, 4040]
    # Unplaced, the same member leaves the reading unknown, not '='.
    stranger = [*queries, {"t": 690.9, "member": 4100, "created": 687.3}]
    assert (
        drain_reading(
            fates, stranger, stopped, known_other=known_non_installer(_K2_ROW)
        ).value
        is None
    )


# --- The installer inventory, before any session after the row ---------------------


def _unended(problems: list[str]) -> list[int]:
    return sorted(int(p.split()[1]) for p in problems)


def test_every_installer_lifetime_must_be_shown_ended():
    fates = Fates(_Native())
    fates.fates[(880, 686.896)] = Fate(880, 686.896, exit_code=1, kernel_exit=690.9)
    fates.fates[(4040, 687.182)] = Fate(4040, 687.182, problem="PermissionError()")
    records = [
        *_K2_ROW,
        _seen(5000, 880, 688.0, "installer"),
        # Started once setup had, below a parent nobody recorded.
        _seen(5100, 5099, 689.0, "other"),
        # Started before setup, with a parent nobody recorded: not setup's.
        _seen(5200, 5199, 600.0, "other"),
    ]
    problems = installer_inventory(records, fates)
    # 880 settled through its handle. 4040's handle failed; 4100 is a console
    # host below the installer, 5000 an installer nobody watched, 5100 of
    # unresolved lineage since setup began: none was seen to leave. The
    # owner's gate, its child and its console host trace to the harness.
    assert _unended(problems) == [4040, 4100, 5000, 5100]
    assert any("PermissionError" in p for p in problems)
    assert any("4100 (below an installer)" in p for p in problems)
    # The watcher seeing a lifetime leave the table is an observed end.
    gone = [
        *records,
        _gone(4040, 687.182, 690.95),
        _gone(4100, 687.300, 690.95),
        _gone(5000, 688.0, 690.96),
        _gone(5100, 689.0, 690.97),
    ]
    assert installer_inventory(gone, fates) == []


def test_a_late_installer_whose_handle_failed_blocks_the_session(tmp_path):
    # E1EP-03: the late look could not open it, the drain never asked about
    # it, the other installer settled: it is still not shown ended.
    fates = Fates(_Native(opens=False))
    fates.watch(5000, 688.0)
    records = [_seen(5000, 880, 688.0, "installer")]
    problems = job_query_problems(
        _shim(tmp_path),
        fates=fates,
        reading=drain_reading(fates.fates.values(), [], []),
        window={"script_ended": True},
        script_error=None,
        observed=records,
    )
    assert any("installer inventory" in p and "5000" in p for p in problems)


def test_what_the_watcher_and_cleanup_could_not_do_is_an_observation_failure(
    tmp_path,
):
    # E1EP-04: K2 keeps its known '!', never a watcher that could not see or a
    # cleanup that could not finish.
    fates = Fates(_Native())
    fates.fates[(880, 686.896)] = Fate(880, 686.896, exit_code=1, kernel_exit=690.9)
    problems = job_query_problems(
        _shim(tmp_path),
        fates=fates,
        reading=drain_reading(fates.fates.values(), [], []),
        window={"script_ended": True},
        script_error=None,
        observed=[_seen(880, 3860, 686.896, "installer")],
        host=["the host session failed: RuntimeError: boom"],
        watcher=["the watcher wrote no summary"],
        cleanup=["the row's daemon directory survived removal"],
    )
    assert "watcher: the watcher wrote no summary" in problems
    assert "cleanup: the row's daemon directory survived removal" in problems
    assert "the host session failed: RuntimeError: boom" in problems


# --- The successor, on Windows' clock --------------------------------------------


def test_a_wall_clock_marker_orders_only_well_apart_and_while_the_clock_held():
    marker = WallClockMarker()
    assert marker.after(1, 10.0) is None  # never marked
    marker.created = 100.0
    assert marker.after(1, 101.0) is True
    assert marker.after(1, 99.0) is False
    assert marker.after(1, 100.1) is None  # too close to order
    # The wall clock jumped since the row began: nothing is ordered.
    wall, mono = marker.began
    marker.began = (wall - 5.0, mono)
    assert marker.after(1, 101.0) is None


def test_a_wall_clock_marker_reads_a_real_process():
    marker = WallClockMarker()
    marker.mark()
    assert marker.created is not None
    assert abs(marker.created - time.time()) < 30


#: The close began at 10 (marker created then); the probe ran 12.5 to 14.
_PROBE = (12.5, 14.0)


def _after_close(pid, start):
    return start > 10.25 if abs(start - 10.0) > 0.25 else None


def _owner_identity(pid, start, instance):
    from differential.harness import OwnerIdentity

    return OwnerIdentity(pid, start, instance, "/auth", None)


def _served(*extra, owner_start=12.0, browser_t=13.0):
    return [
        _seen(20, 10, 2.0, "owner"),
        _seen(22, 20, 3.0, "driver"),
        _seen(30, 22, 4.0, "browser"),
        _gone(30, 4.0, 9.0),
        _seen(40, 10, owner_start, "owner", t=owner_start + 0.05),
        _seen(42, 40, browser_t, "driver", t=browser_t),
        _seen(43, 42, browser_t + 0.1, "browser", t=browser_t + 0.1),
        *extra,
    ]


def _successor(records, *, owner_start=12.0, probe=_PROBE, requests=1):
    return successor_problems(
        records,
        _owner_identity(20, 2.0, "first"),
        _owner_identity(40, owner_start, "second"),
        probe=probe,
        probe_requests=requests,
        after_close=_after_close,
    )


def test_a_new_owner_whose_browser_ran_while_the_probe_was_served_succeeds():
    assert _successor(_served()) == []


def test_an_owner_born_after_the_probe_did_not_serve_it():
    # E1EP-05: born at 20, browser at 22, the probe returned at 14.
    problems = _successor(_served(owner_start=20.0, browser_t=22.0), owner_start=20.0)
    assert any("no browser of pid 40" in p for p in problems)


def test_an_unknown_browser_beside_the_successors_fails_closed():
    stranger = _seen(50, 49, 13.2, "browser", t=13.2)
    problems = _successor(_served(stranger))
    assert any("browser 50 ran while" in p for p in problems)


# --- The first read finds the private cache installed ---------------------------

_READY = """
from linkedin_mcp_server import bootstrap
bootstrap.configure_browser_environment()
print(bootstrap.browser_ready())
"""


def _ready(env: dict[str, str]) -> bool:
    """What this checkout's own readiness check says, in a fresh interpreter."""
    result = subprocess.run(
        [sys.executable, "-I", "-c", _READY],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        env=env,
    )
    return result.stdout.strip().splitlines()[-1] == "True"


def test_the_private_cache_reads_as_installed_before_the_first_read(
    tmp_path, isolate_profile_dir
):
    # The Windows CI cause, off Windows: staging recorded the real cache, and
    # the readiness check refuses a record for any other browsers path, so
    # with the private cache configured the first read only said "setup in
    # progress". Symlinks stand in for the junctions.
    from linkedin_mcp_server import bootstrap

    targets = bootstrap._patchright_install_targets()
    assert targets is not None
    revision = targets[bootstrap._FULL_DIR_PREFIX]
    real = tmp_path / "real"
    browser = real / f"{bootstrap._FULL_DIR_PREFIX}{revision}"
    browser.mkdir(parents=True)
    (browser / "INSTALLATION_COMPLETE").write_text("")
    ffmpeg = real / "ffmpeg-1"
    ffmpeg.mkdir()
    (ffmpeg / "INSTALLATION_COMPLETE").write_text("")
    directory = isolate_profile_dir
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LINKEDIN", "PLAYWRIGHT", "USER_DATA_DIR"))
    }
    base["USER_DATA_DIR"] = str(directory)
    staged = {**base, "PLAYWRIGHT_BROWSERS_PATH": str(real)}
    record_install(sys.executable, staged)
    private = {**base, "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "private" / "b")}
    assert not _ready(private)  # the cause: a record for the real cache only
    host = StallHost().start()
    try:
        env = dict(base)
        cache = private_install(
            sys.executable, [browser, ffmpeg], env, host, parent=tmp_path
        )
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(cache.directory)
        assert _ready(env)
        cache.hold_back()
        (directory.parent / "browser-install.json").unlink(missing_ok=True)
        cache.restore()
        assert not _ready(env)
        cache.hold_back()
        cache.restore_installed(sys.executable, env)
        assert _ready(env)
        cache.dismantle()
    finally:
        host.stop()
    assert (browser / "INSTALLATION_COMPLETE").is_file()


def test_recording_an_install_that_is_not_there_is_refused(
    tmp_path, isolate_profile_dir
):
    # A row whose first read would only say "setup in progress" stops here,
    # before an actor starts, with the path it could not read as ready.
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LINKEDIN", "PLAYWRIGHT", "USER_DATA_DIR"))
    }
    env.update(
        USER_DATA_DIR=str(isolate_profile_dir), PLAYWRIGHT_BROWSERS_PATH=str(empty)
    )
    with pytest.raises(RuntimeError, match="does not read as ready"):
        record_install(sys.executable, env)


def test_an_owner_log_says_whether_its_close_stayed_unconfirmed(tmp_path):
    # The line the owner logs when its drain could not prove the browser gone,
    # as in run 36384952466, K3.
    log = tmp_path / "daemon.log"
    log.write_text(
        '{"level": "ERROR", "message": "Browser processes from this launch are '
        'still running after close, so the shutdown stays unconfirmed."}\n'
    )
    assert close_left_unconfirmed(str(log), seconds=0.1)
    # K2: the baseline drain terminated what it could not place and confirmed.
    log.write_text('{"level": "INFO", "message": "Browser closed"}\n')
    assert not close_left_unconfirmed(str(log), seconds=0.1)
    assert not close_left_unconfirmed(None, seconds=0.1)
