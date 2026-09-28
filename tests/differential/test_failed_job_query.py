"""H-R11's shim, its placement, its member, and its judgement, without Windows.

The native row is ``test_failed_job_query_row.py`` and runs on Windows CI only.
Here, on every platform:

* the shim's scope: it fails ``IsProcessInJob`` for the one caller it names
  and passes every other call through, recording each failure it plants;
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
    is_installer,
    job_query_observations,
    judge_row,
    r11_reading,
    r11_verdict,
)
from differential.job_query import (
    SHIM_SHA256,
    SHIM_SOURCE,
    Fate,
    Fates,
    PrivateCache,
    ShimVenv,
    StallHost,
    make_shim_venv,
    private_install,
    reached,
    record_install,
    shim_namespace,
    filetime_to_unix,
    terminated_by_drain,
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


def _planted(record: Path, answer: bool = True) -> SimpleNamespace:
    """A ``win32job`` double with the shim installed over it."""
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: answer)
    shim_namespace()["install"](job, _Error, lambda process: process, record)
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
    shim_namespace()["install"](job, _Error, lambda handle: handle.process, record)

    def sleep(seconds: float) -> None:
        clock.now += seconds

    module.__dict__.update(
        _IS_WINDOWS=True,
        _adopted_windows_job=adopted,
        _adopted_windows_gate=None,
        _live_windows_jobs=[SimpleNamespace(job_handle=55)],
        _windows_modules=lambda: (Api(), Con(), job, object()),
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


@pytest.mark.differential_row(row=ROW, experiment="K3", column="unit")
def test_the_candidate_neither_terminates_nor_proves_the_drain(tmp_path):
    record = tmp_path / "reached.jsonl"
    source = Path(process_tree.__file__).read_text(encoding="utf-8")
    terminated, proved = _drain(_module(source), record)
    assert terminated == []
    assert proved is False
    assert reached(record) and {line["member"] for line in reached(record)} == {700}


@pytest.mark.differential_row(row=ROW, experiment="K1", column="unit")
def test_without_an_adopted_job_the_query_is_never_reached(tmp_path):
    # Direct: no adopted Job, so the drain has no member to ask about.
    record = tmp_path / "reached.jsonl"
    source = baseline_file("linkedin_mcp_server/process_tree.py")
    terminated, proved = _drain(_module(source), record, adopted=None)
    assert (terminated, proved) == ([], True)
    assert reached(record) == []


# --- The shim venv, built -------------------------------------------------------


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


def _reach(tmp_path: Path, pid: int) -> None:
    with (tmp_path / "h-r11-reached.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"pid": pid, "t": 5.0, "member": 700, "job": 55}) + "\n"
        )


def _fates(*fates: Fate) -> Fates:
    tracked = Fates()
    for fate in fates:
        tracked.fates[(fate.pid, fate.start)] = fate
    return tracked


_WINDOW = {"began": 10.0, "ended": 12.0, "probe_ended": 20.0}
_OWNERS = [
    {"kind": "process.start", "t": 1.0, "pid": 42, "in_row": True, "actor": "owner"},
    {"kind": "process.start", "t": 15.0, "pid": 43, "in_row": True, "actor": "owner"},
]


def test_a_terminated_member_inside_the_close_reads_bang(tmp_path):
    _reach(tmp_path, 42)
    observed = job_query_observations(
        _shim(tmp_path),
        fates=_fates(Fate(700, 9.0, exited_at=11.0, exit_code=1)),
        window=_WINDOW,
        owner_pid=42,
        observed=_OWNERS[:1],
        daemon=True,
    )
    assert observed["job_query_reached"] and observed["job_member_terminated"]
    assert observed["successor_before_quit"] is False


# The three shapes run 36381621588 measured on Windows, seconds past 1790573600.
#: K2: the baseline drain asked about 7596 and ended it 6 ms later, mid-close.
_K2 = dict(window=(72.848, 73.805), queried=[73.749], exited=73.755)
#: K3: the candidate drain asked about the member for its whole 10 s deadline;
#: the owner's shutdown ended it 211 ms after the close had returned.
_K3 = dict(window=(130.632, 140.910), queried=[130.901, 140.904], exited=141.121)


def _drained(shape: dict, member: int = 700, **fate: Any) -> list[Fate]:
    queried = [{"member": member, "t": t} for t in shape["queried"]]
    ended = Fate(700, 1.0, exit_code=1, kernel_exit=shape["exited"])
    return terminated_by_drain(
        [dataclasses.replace(ended, **fate)], queried, shape["window"]
    )


def test_the_drain_ending_a_member_it_just_failed_to_place_reads_bang():
    assert [fate.pid for fate in _drained(_K2)] == [700]


def test_the_shared_shutdown_after_the_close_is_not_the_drains():
    # The K3 reading the 1 s grace turned into '!' on Windows.
    assert _drained(_K3) == []


@pytest.mark.parametrize(
    ("shape", "fate", "member"),
    [
        # K1: no adopted Job, so nothing ever asked about the member.
        pytest.param(_K2, {}, 701, id="never-asked"),
        pytest.param(
            dict(_K2, queried=[73.760]), {}, 700, id="ended-before-it-was-asked"
        ),
        pytest.param(_K2, {"exit_code": 0}, 700, id="another-code"),
        pytest.param(_K2, {"kernel_exit": None}, 700, id="still-running"),
    ],
)
def test_any_other_end_of_the_member_is_not_the_drains(shape, fate, member):
    assert _drained(shape, member=member, **fate) == []


def test_the_kernels_exit_time_is_judged_not_the_waiters():
    # The waiter woke after the close returned; the kernel ended it inside.
    late = _drained(_K2, exited_at=73.9, kernel_exit=73.804)
    assert [fate.pid for fate in late] == [700]


def test_a_filetime_reads_on_the_unix_clock():
    assert filetime_to_unix(116_444_736_000_000_000) == 0.0
    assert filetime_to_unix(116_444_736_000_000_000 + 15_000_000) == 1.5


def test_the_first_owners_gate_and_launcher_are_no_successor(tmp_path):
    # Windows records the owner's gate and venv launcher as owner processes,
    # started with it, long before the close.
    observed = job_query_observations(
        _shim(tmp_path),
        fates=_fates(),
        window=_WINDOW,
        owner_pid=42,
        observed=[
            {
                "kind": "process.start",
                "t": 1.0,
                "pid": p,
                "in_row": True,
                "actor": "owner",
            }
            for p in (40, 41, 42)
        ],
        daemon=True,
    )
    assert observed["successor_before_quit"] is False


def test_a_planted_failure_in_another_process_is_not_the_owners(tmp_path):
    _reach(tmp_path, 99)
    observed = job_query_observations(
        _shim(tmp_path),
        fates=_fates(),
        window=_WINDOW,
        owner_pid=42,
        observed=_OWNERS,
        daemon=True,
    )
    assert observed["job_query_reached"] is False
    assert observed["successor_before_quit"] is True


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


def _result(vector: RowVector) -> RowResult:
    return RowResult(experiment="X", mode=vector.mode, vector=vector)


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


def test_k3_must_read_equal_and_elect_a_successor(profile):
    good = _vector(profile, daemon=True)
    assert r11_verdict(_result(good), experiment="K3", non_windows=False) == []
    bang = _vector(profile, daemon=True, job_member_terminated=True)
    assert any(
        "terminated" in p
        for p in r11_verdict(_result(bang), experiment="K3", non_windows=False)
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
        cache = private_install(sys.executable, [browser], env, host, parent=tmp_path)
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(cache.directory)
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
