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
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from differential.baseline import baseline_file
from differential.harness import (
    RowResult,
    RowVector,
    close_left_unconfirmed,
    is_installer,
    job_query_observations,
    job_query_problems,
    judge_row,
    r11_reading,
    r11_verdict,
    successor_verdict,
)
from differential.job_query import (
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


def test_the_actors_startup_installs_the_fault_and_the_observer(monkeypatch):
    # What an actor's interpreter runs: the shim as ``sitecustomize`` on Windows.
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    api = SimpleNamespace(TerminateProcess=lambda handle, status: None)
    original, terminate = job.IsProcessInJob, api.TerminateProcess
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "win32job", job)
        patch.setitem(sys.modules, "win32api", api)
        patch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=_Error))
        namespace: dict[str, Any] = {"__name__": "sitecustomize", "__file__": "shim"}
        exec(compile(SHIM_SOURCE, "sitecustomize.py", "exec"), namespace)
    assert job.IsProcessInJob is not original
    assert api.TerminateProcess is not terminate


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
    # Nothing but the shim and the path line: every other import is the source's.
    added = {p.name for p in Path(venv.site_packages).iterdir()} - {"__pycache__"}
    assert added == {"sitecustomize.py", "_h_r11_code.pth"}


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
    succeeded: bool = True,
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


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(_terminate("_drain_adopted_windows_job"), id="hard-exit-drain"),
        pytest.param(_terminate(succeeded=False), id="failed-call"),
    ],
)
def test_neither_the_hard_exit_drain_nor_a_failed_call_is_the_routine_bang(line):
    # The baseline's hard exit terminates every member after the close; a
    # termination that failed terminated nothing. Neither is the witness.
    reading = drain_reading([_ended()], [_query()], [line])
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
            _ended(), [_query(created=5.0)], [], "did not watch", id="another-lifetime"
        ),
        pytest.param(
            _ended(),
            [_query(), _query(member=701)],
            [],
            "did not watch",
            id="an-unwatched-member",
        ),
        pytest.param(
            _ended(),
            [_query()],
            [_terminate(member=701)],
            "did not watch",
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


def _watched(native: _Native, *, required: bool = True) -> Fates:
    fates = Fates(native)
    fates.watch(700, 9.0, required=required)
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
def test_a_late_look_keeps_only_what_it_can_watch(native):
    assert _watched(native, required=False).fates == {}


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
