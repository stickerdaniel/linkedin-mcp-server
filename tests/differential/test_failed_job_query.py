"""H-R11's shim, its source model, its member and its evidence, without Windows.

The native row is ``test_failed_job_query_row.py`` and runs on Windows CI only;
its continuation is judged through the real row in ``test_preservation_gate``.
Here, on every platform:

* the shim's scope: it fails ``IsProcessInJob`` for the one caller it names,
  passes every other call through, and records each failure it plants with the
  lifetime that made the call;
* the **source model**: the baseline's and this checkout's own routine,
  compiled from their source text, run against Win32 doubles
  (``job_query_model``): the baseline's prohibited branch, the candidate's
  abstention, and every positive control around them, and a spliced revision
  that must fail the calibration;
* which records witness the intended entry, and which lifetimes the row must
  account for; the installer fates and inventory behind settlement; the
  successor behind recovery; the owner's log behind the consumer;
* the shim venv, built for real: same product code, the shim ran, the hash;
* the row-private cache: a held-back link is only ever a link;
* the stall host: it holds a request without answering.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from differential.baseline import baseline_file
from differential.harness import (
    BELOW_INSTALLER,
    FROM_HARNESS,
    INSTALLER,
    NEGATIVE_CONSUMPTION_LINE,
    UNRESOLVED,
    Lineage,
    WallClockMarker,
    auth_files,
    fault_witnesses,
    installer_family,
    installer_inventory,
    is_installer,
    job_query_problems,
    log_size,
    logged_since,
    protected_changes,
    restoration_changes,
    successor_problems,
    successor_verdict,
    unaccounted_members,
)
from differential.job_query import (
    SHIM_SHA256,
    SHIM_SOURCE,
    Fate,
    Fates,
    PrivateCache,
    ShimVenv,
    StallHost,
    code_difference,
    filetime_to_unix,
    make_shim_venv,
    private_install,
    reached,
    record_install,
    shim_namespace,
)
from differential.job_query_model import (
    BASELINE,
    CANDIDATE,
    CASES,
    SOURCE_MODEL,
    ModelRefused,
    Routine,
    calibrate,
    source_sha256,
)
from differential.session import (
    LAST_VERSION_FILE,
    snapshot,
    write_synthetic_cookie_file,
)
from linkedin_mcp_server import process_tree
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
    write_source_state,
)

ROW = "H-R11"
_PROCESS_TREE = "linkedin_mcp_server/process_tree.py"


class _Error(Exception):
    """Stands in for ``pywintypes.error``."""


def _identity(handle: Any) -> tuple[int, float]:
    """What the shim reads of a handle: here the handle is the pid, created at 9."""
    return handle, 9.0


def _planted(record: Path, answer: bool = True, created: float | None = 5.0):
    """A ``win32job`` double with the shim installed over it."""
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: answer)
    shim_namespace()["install"](job, _Error, _identity, record, created=created)
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
    # The lifetime that made the call, and the one it asked about.
    assert (line["pid"], line["pid_created"]) == (os.getpid(), 5.0)
    assert (line["member"], line["created"], line["job"]) == (700, 9.0, 55)


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


def test_a_record_that_cannot_be_written_still_plants_the_failure(tmp_path):
    # The fault is the fault; only its witness is missing, and nothing reads
    # the missing witness as anything.
    unwritable = tmp_path / "a-directory"
    unwritable.mkdir()
    job = _planted(unwritable)
    with pytest.raises(_Error):
        _caller("_in_another_owned_job", "linkedin_mcp_server.process_tree")(
            job, 700, 55
        )
    assert reached(unwritable) == []


def test_an_unreadable_line_witnesses_nothing_and_hides_nothing(tmp_path):
    record = tmp_path / "reached.jsonl"
    job = _planted(record)
    with record.open("a") as stream:
        stream.write('{"kind": "query", "pid": 1\n')
    with pytest.raises(_Error):
        _caller("_in_another_owned_job", "linkedin_mcp_server.process_tree")(
            job, 700, 55
        )
    assert [line["member"] for line in reached(record)] == [700]


def test_the_shim_is_one_text_with_one_hash():
    import hashlib

    from differential import job_query

    assert hashlib.sha256(job_query.SHIM_SOURCE.encode()).hexdigest() == SHIM_SHA256


# --- The source model: each revision's own routine, Win32 doubles --------------


def _sources() -> dict[str, str]:
    return {
        BASELINE: baseline_file(_PROCESS_TREE),
        CANDIDATE: Path(process_tree.__file__).read_text(encoding="utf-8"),
    }


@pytest.fixture(scope="module")
def routines() -> dict[str, Routine]:
    return {
        revision: Routine.load(revision, source)
        for revision, source in _sources().items()
    }


def _experiment(case_name: str, revision: str) -> str:
    """Which K cell a model case stands for: K1 has no adopted Job."""
    if case_name == "no-adopted-job":
        return "K1"
    return "K2" if revision == BASELINE else "K3"


@pytest.mark.parametrize(
    ("case", "revision"),
    [
        pytest.param(
            case,
            revision,
            id=f"{case.name}-{revision}",
            marks=pytest.mark.differential_row(
                row=ROW, experiment=_experiment(case.name, revision), column="unit"
            ),
        )
        for case in CASES
        for revision in (BASELINE, CANDIDATE)
    ],
)
def test_each_revision_selects_its_own_branch_in_every_modelled_state(
    routines, case, revision
):
    outcome = case.run(routines[revision])
    assert case.check(revision, outcome) == [], (case.claim, outcome)


def test_the_unanswered_member_is_where_the_revisions_part(routines):
    # The conditional branch itself, stated plainly: the same modelled state,
    # TerminateProcess selected by one routine and not by the other.
    (unknown,) = [case for case in CASES if case.name == "unknown"]
    baseline = unknown.run(routines[BASELINE])
    candidate = unknown.run(routines[CANDIDATE])
    assert dict(baseline.selected) == {700: 1} and baseline.returned is True
    assert dict(candidate.selected) == {} and candidate.returned is False
    # The candidate kept counting it, iteration after iteration, to the deadline.
    assert candidate.iterations > 2


def test_the_calibration_names_the_sources_it_ran():
    sources = _sources()
    model = calibrate(sources)
    assert model.passed, model.problems
    assert model.evidence == SOURCE_MODEL
    assert model.sha256 == {
        revision: source_sha256(text) for revision, text in sources.items()
    }


def _spliced(target: str, donor: str, name: str) -> str:
    """*target*'s source with the definition of *name* taken from *donor*."""
    donor_def = next(
        node
        for node in ast.parse(donor).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    tree = ast.parse(target)
    tree.body = [
        donor_def if isinstance(node, ast.FunctionDef) and node.name == name else node
        for node in tree.body
    ]
    return ast.unparse(tree)


@pytest.mark.parametrize(
    ("revision", "name", "donor"),
    [
        # A candidate whose helper reads an unanswered Job as "no" again.
        pytest.param(CANDIDATE, "_in_another_owned_job", BASELINE, id="helper"),
        # The candidate's helper under the baseline's routine: None is falsy,
        # so the routine ends the member it could not place.
        pytest.param(BASELINE, "_in_another_owned_job", CANDIDATE, id="routine"),
    ],
)
def test_a_revision_that_ends_an_unplaced_member_fails_the_calibration(
    revision, name, donor
):
    sources = _sources()
    sources[CANDIDATE] = _spliced(sources[revision], sources[donor], name)
    model = calibrate(sources)
    assert any(
        problem.startswith(("unknown, candidate:", "planted, candidate:"))
        for problem in model.problems
    ), model.problems


def test_a_source_without_the_routine_is_refused():
    tree = ast.parse(Path(process_tree.__file__).read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.FunctionDef) and node.name == "_in_another_owned_job"
        )
    ]
    without = ast.unparse(tree)
    with pytest.raises(ModelRefused, match="_in_another_owned_job is defined 0"):
        Routine.load(CANDIDATE, without)
    model = calibrate({BASELINE: baseline_file(_PROCESS_TREE), CANDIDATE: without})
    assert not model.passed and CANDIDATE not in model.sha256


# --- The shim venv, built -------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="win32job runs only on Windows")
def test_model_shim_does_not_patch_the_real_job_api():
    win32job = pytest.importorskip("win32job")
    original = win32job.IsProcessInJob
    shim_namespace()
    assert win32job.IsProcessInJob is original


def test_model_shim_does_not_patch_a_windows_job_api(monkeypatch):
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    original = job.IsProcessInJob
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "win32job", job)
        patch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=_Error))
        shim_namespace()
    assert job.IsProcessInJob is original


def test_an_actors_startup_plants_the_fault_even_without_its_own_lifetime(
    monkeypatch, tmp_path
):
    # Run as an actor's ``sitecustomize`` over Windows doubles. Its creation
    # time is unreadable here, so its records carry none and witness nothing,
    # but the fault is still planted.
    job = SimpleNamespace(IsProcessInJob=lambda process, handle: True)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        patch.setitem(sys.modules, "win32job", job)
        patch.setitem(sys.modules, "win32api", None)
        patch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=_Error))
        namespace: dict[str, Any] = {
            "__name__": "sitecustomize",
            "__file__": str(tmp_path / "sitecustomize.py"),
        }
        exec(compile(SHIM_SOURCE, "sitecustomize.py", "exec"), namespace)
    record = tmp_path / "h-r11-reached.jsonl"
    # Nothing is written at startup.
    assert not record.exists()
    with pytest.raises(_Error):
        _caller("_in_another_owned_job", "linkedin_mcp_server.process_tree")(
            job, 700, 55
        )
    (line,) = reached(record)
    assert line["pid_created"] is None and line["pid"] == os.getpid()


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


# --- Installer fates ------------------------------------------------------------


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
    # Neither alive nor ended: unknown, and the inventory still wants its end.
    assert fates.alive() == [] and fates.unsettled() == [fate]
    records = [_seen(700, 880, 9.0, "installer")]
    assert installer_inventory(records, fates)


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


def _ended(pid: int = 700, start: float = 9.0) -> Fate:
    return Fate(pid, start, exit_code=1, kernel_exit=11.0, exited_at=11.1)


def test_the_problems_that_keep_the_row_from_observing(tmp_path):
    assert (
        job_query_problems(
            _shim(tmp_path),
            fates=_fates(_ended()),
            window={"script_ended": True, "clock_held": True},
            script_error=None,
        )
        == []
    )
    problems = job_query_problems(
        _shim(tmp_path),
        fates=_fates(),
        window={
            "clock_held": False,
            "family_before_recovery": ["pid 700 (installer) was neither seen"],
            "restoration_changes": ["the auth root's cookies.json changed"],
        },
        script_error="TimeoutError: probe",
    )
    assert any("script failed" in p for p in problems)
    assert any("no installer ran" in p for p in problems)
    assert any("did not run to its end" in p for p in problems)
    assert any(p.startswith("before recovery: pid 700") for p in problems)
    assert any(p.startswith("restoration: the auth root's cookies") for p in problems)
    assert any("wall clock moved" in p for p in problems)


def test_the_installer_is_every_process_of_it():
    assert is_installer({"actor": "installer", "cmdline": ["x"]})
    assert is_installer(
        {"cmdline": ["python", "-P", "-m", "patchright", "install", "chromium"]}
    )
    assert is_installer({"cmdline": ["node", "lib/entry/oopBrowserDownload.js"]})
    assert not is_installer({"cmdline": ["python", "-m", "linkedin_mcp_server"]})


# --- Placing lifetimes: the family, the row's own, or unaccounted -----------------


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


def _lineage(records, pid, created):
    lineage = Lineage(records)
    life = lineage.lifetime(pid, created)
    return None if life is None else lineage.of(life)


@pytest.mark.parametrize(
    ("member", "created", "kind"),
    [
        pytest.param(3572, 678.378, FROM_HARNESS, id="owner-gate"),
        pytest.param(7708, 678.383, FROM_HARNESS, id="owner-child"),
        pytest.param(1276, 678.386, FROM_HARNESS, id="console-host"),
        pytest.param(880, 686.896, INSTALLER, id="the-installer"),
        pytest.param(4040, 687.182, INSTALLER, id="an-installer-by-its-command"),
        pytest.param(4100, 687.300, BELOW_INSTALLER, id="an-installers-descendant"),
        pytest.param(3572, 600.0, None, id="another-lifetime-at-the-pid"),
        pytest.param(9999, 678.0, None, id="never-recorded"),
    ],
)
def test_each_recorded_lifetime_leads_where_its_ancestry_does(member, created, kind):
    assert _lineage(_K2_ROW, member, created) == kind


@pytest.mark.parametrize(
    "missing", [6076, 6928, 3572], ids=["launcher", "frontend", "gate"]
)
def test_a_parent_nobody_recorded_leaves_the_ancestry_unresolved(missing):
    # E1EU-03: with any link of the chain unrecorded, the console host could
    # sit below an installer, so it has to be seen to end like one.
    records = [r for r in _K2_ROW if r["pid"] != missing]
    assert _lineage(records, 1276, 678.386) == UNRESOLVED
    problems = installer_inventory(records, _fates(_ended(880, 686.896)))
    assert any(p.startswith("pid 1276 (unresolved)") for p in problems)


def test_a_link_outside_the_row_leaves_the_ancestry_unresolved():
    records = [dict(r, in_row=False) if r["pid"] == 6928 else r for r in _K2_ROW]
    assert _lineage(records, 1276, 678.386) == UNRESOLVED


def test_only_the_installer_family_is_the_family():
    member = installer_family(_K2_ROW, _fates(_ended(5000, 688.0)))
    assert member(880, 686.896) and member(4040, 687.182) and member(4100, 687.3)
    # Watched as an installer, though the watcher never recorded it.
    assert member(5000, 688.0)
    assert not member(3572, 678.378) and not member(1276, 678.386)
    assert not member(9999, 678.0)


def test_every_lifetime_the_drain_asked_about_must_be_accounted_for():
    # Run 36410976409, K2: the owner's gate, its child and its console host
    # trace to the harness; the installer and its worker were recorded.
    asked = [
        {"member": m, "created": c}
        for m, c in [
            (3572, 678.378),
            (7708, 678.383),
            (1276, 678.386),
            (880, 686.896),
            (4040, 687.182),
        ]
    ]
    assert unaccounted_members(asked, _K2_ROW, _fates()) == []
    stranger = [*asked, {"member": 4200, "created": 687.9}]
    (problem,) = unaccounted_members(stranger, _K2_ROW, _fates())
    assert "pid 4200" in problem
    # Watched by the row, though never recorded, it is accounted for.
    assert unaccounted_members(stranger, _K2_ROW, _fates(_ended(4200, 687.9))) == []


# --- Which records witness the intended entry ------------------------------------

#: The owner that closed, (pid, its own creation time), and its close call.
_OWNER = (42, 1.0)
_CLOSE = (100.0, 110.0)


def _record(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "query",
        "t": 105.0,
        "pid": 42,
        "pid_created": 1.0,
        "member": 880,
        "created": 686.896,
        "job": 55,
        **fields,
    }


@pytest.mark.parametrize(
    ("changes", "witness"),
    [
        pytest.param({}, True, id="the-owner-about-the-installer-in-its-close"),
        pytest.param({"member": 4100, "created": 687.3}, True, id="below-it"),
        pytest.param({"pid_created": 3.0}, False, id="another-lifetime-at-the-pid"),
        pytest.param({"pid_created": None}, False, id="its-lifetime-unread"),
        pytest.param({"job": None}, False, id="no-held-job-named"),
        pytest.param({"pid": 43, "pid_created": 120.0}, False, id="the-successor"),
        pytest.param({"t": 99.9}, False, id="before-the-close"),
        pytest.param({"t": 110.1}, False, id="after-the-close"),
        pytest.param({"member": 3572, "created": 678.378}, False, id="the-gate"),
        pytest.param({"member": 9999, "created": 1.0}, False, id="an-unrecorded-one"),
    ],
)
def test_a_witness_is_the_owner_that_closed_asking_about_the_family_then(
    changes, witness
):
    family = installer_family(_K2_ROW, _fates())
    found = fault_witnesses(
        [_record(**changes)], owner=_OWNER, family=family, interval=_CLOSE
    )
    assert bool(found) is witness


def test_no_owner_or_no_close_leaves_no_witness():
    family = installer_family(_K2_ROW, _fates())
    assert (
        fault_witnesses([_record()], owner=None, family=family, interval=_CLOSE) == []
    )
    assert (
        fault_witnesses([_record()], owner=_OWNER, family=family, interval=None) == []
    )


# --- The installer inventory, before recovery and before any later session --------


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
        # Earlier observation without ancestry still cannot prove non-setup.
        _seen(5200, 5199, 600.0, "other"),
    ]
    problems = installer_inventory(records, fates)
    # 880 settled through its handle. 4040's handle failed; 4100 is a console
    # host below the installer, 5000 an installer nobody watched, and 5100
    # and 5200 have unresolved lineage: none was seen to leave. The owner's
    # gate, its child and its console host trace to the harness.
    assert _unended(problems) == [4040, 4100, 5000, 5100, 5200]
    assert any("PermissionError" in p for p in problems)
    assert any("4100 (below an installer)" in p for p in problems)
    # The watcher seeing a lifetime leave the table is an observed end.
    gone = [
        *records,
        _gone(4040, 687.182, 690.95),
        _gone(4100, 687.300, 690.95),
        _gone(5000, 688.0, 690.96),
        _gone(5100, 689.0, 690.97),
        _gone(5200, 600.0, 690.97),
    ]
    assert installer_inventory(gone, fates) == []


def test_a_late_installer_whose_handle_failed_blocks_the_session(tmp_path):
    # E1EP-03: the late look could not open it, the other installer settled:
    # it is still not shown ended.
    fates = Fates(_Native(opens=False))
    fates.watch(5000, 688.0)
    records = [_seen(5000, 880, 688.0, "installer")]
    problems = job_query_problems(
        _shim(tmp_path),
        fates=fates,
        window={"script_ended": True},
        script_error=None,
        observed=records,
    )
    assert any("installer inventory" in p and "5000" in p for p in problems)


def test_what_the_watcher_and_cleanup_could_not_do_is_an_observation_failure(
    tmp_path,
):
    # E1EP-04: every experiment, K2 included, is held to these.
    fates = Fates(_Native())
    fates.fates[(880, 686.896)] = Fate(880, 686.896, exit_code=1, kernel_exit=690.9)
    problems = job_query_problems(
        _shim(tmp_path),
        fates=fates,
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


# --- The harness's restoration, and the session at the recovery boundary ----------


@pytest.fixture
def auth(tmp_path):
    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    (directory / "Default").mkdir()
    (directory / "Default" / "Preferences").write_text("{}")
    return directory, staged


def test_the_restoration_may_write_only_its_own_install_record(auth):
    profile, _ = auth
    root = profile.parent
    before = auth_files(root)
    (root / "browser-install.json").write_text('{"restored": true}')
    assert restoration_changes(before, auth_files(root)) == []
    # Anything else written meanwhile is not the harness's to hide.
    portable_cookie_path(profile).write_text("[]")
    (profile / "Default" / "Preferences").unlink()
    changed = restoration_changes(before, auth_files(root))
    assert len(changed) == 2 and all("changed while" in p for p in changed)


def test_a_legitimate_export_is_no_protected_change(auth):
    profile, staged = auth
    before = snapshot(profile, expected_digest=staged.li_at_digest)
    # The close's export rewrites the cookie file, the session unchanged.
    cookies = portable_cookie_path(profile)
    entries = json.loads(cookies.read_text())
    cookies.write_text(json.dumps([*entries, {"name": "lang", "value": "en"}]))
    at = snapshot(profile, expected_digest=staged.li_at_digest)
    assert at.cookies_sha256 != before.cookies_sha256
    assert protected_changes(before, at) == []


def test_a_new_login_generation_by_the_boundary_is_a_protected_change(auth):
    profile, staged = auth
    before = snapshot(profile, expected_digest=staged.li_at_digest)
    state = source_state_path(profile)
    state.write_text(
        json.dumps({**json.loads(state.read_text()), "login_generation": "x"})
    )
    at = snapshot(profile, expected_digest=staged.li_at_digest)
    assert any("login generation" in p for p in protected_changes(before, at))


def test_a_lost_session_by_the_boundary_is_a_protected_change(auth):
    profile, staged = auth
    before = snapshot(profile, expected_digest=staged.li_at_digest)
    portable_cookie_path(profile).write_text("[]")
    at = snapshot(profile, expected_digest=staged.li_at_digest)
    assert any("no longer usable" in p for p in protected_changes(before, at))


# --- The owner's own log: the consumer and the stand-down -------------------------


def test_only_what_the_owner_wrote_after_the_offset_counts(tmp_path):
    log = tmp_path / "daemon.log"
    # An earlier owner of the same auth root logged the same line.
    log.write_text(json.dumps({"message": NEGATIVE_CONSUMPTION_LINE}) + "\n")
    offset = log_size(str(log))
    assert (
        logged_since(str(log), offset, NEGATIVE_CONSUMPTION_LINE, seconds=0.1) is False
    )
    with log.open("a") as stream:
        stream.write(json.dumps({"message": NEGATIVE_CONSUMPTION_LINE}) + "\n")
    end = log_size(str(log))
    assert logged_since(str(log), offset, NEGATIVE_CONSUMPTION_LINE, seconds=0.1)
    # And only before a later offset, when one is given.
    with log.open("a") as stream:
        stream.write(json.dumps({"message": "Standing down: later"}) + "\n")
    assert logged_since(str(log), offset, "Standing down", until=end) is False
    assert logged_since(str(log), offset, "Standing down") is True


def test_an_unreadable_log_is_no_witness_either_way(tmp_path):
    assert log_size(None) is None
    assert log_size(str(tmp_path / "missing.log")) is None
    assert logged_since(None, 0, NEGATIVE_CONSUMPTION_LINE) is None
    assert logged_since(str(tmp_path / "missing.log"), 0, "x", seconds=0.1) is None
    assert logged_since(str(tmp_path / "missing.log"), None, "x") is None


# --- The successor, on Windows' clock --------------------------------------------


def test_a_wall_clock_marker_orders_only_well_apart_and_while_the_clock_held():
    marker = WallClockMarker()
    assert marker.after(1, 10.0) is None  # never marked
    marker.created = 100.0
    assert marker.held()
    assert marker.after(1, 101.0) is True
    assert marker.after(1, 99.0) is False
    assert marker.after(1, 100.1) is None  # too close to order
    # The wall clock jumped since the row began: nothing is ordered.
    wall, mono = marker.began
    marker.began = (wall - 5.0, mono)
    assert not marker.held()
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
