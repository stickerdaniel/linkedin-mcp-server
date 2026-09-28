"""The post-quit session starts only when every actor of the row is settled.

Driven through the real row entry, ``measure_host_quit_row``, with everything
that would launch, signal or touch an owner replaced: staging, the watcher
process, the host session, owner discovery, cleanup and the post-quit session
itself. What runs for real is the profile census over a modelled process
table, the gate, and the row's own continuation. Only the census and the
watcher's summary differ between cases, and the question is whether the
post-quit session was started.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import psutil
import pytest

import linkedin_mcp_server
from differential import harness
from differential.baseline import baseline_file
from differential.events import EventLog
from differential.harness import DaemonCleanup, PostQuit, measure_host_quit_row
from differential import job_query
from differential.job_query import SHIM_SHA256, ShimVenv, StallHost
from differential.job_query_model import BASELINE, CANDIDATE, calibrate
from differential.session import LAST_VERSION_FILE, write_synthetic_cookie_file
from differential.signals import (
    COMPLETE,
    UNAVAILABLE,
    OracleOutcome,
    parse_strace,
)
from differential.test_failed_job_query import _Native
from differential.test_row_judgement import _healthy
from linkedin_mcp_server import process_tree
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

ME = "harness-user"
_PROCESS_TREE = "linkedin_mcp_server/process_tree.py"


class _Watcher:
    summary: dict = {}
    records: list = []

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def observed(self):
        return list(self.records)

    def stop(self):
        return self.summary


class _Actor:
    """A modelled owner or server: the handle the row took when it tied it.

    Killed, it stays a zombie, as a killed owner does until its parent reaps
    it; otherwise it has already left.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.kills = 0

    def kill(self):
        self.kills += 1

    def status(self):
        if self.kills:
            return psutil.STATUS_ZOMBIE
        raise psutil.NoSuchProcess(self.pid)

    def num_threads(self):
        # SIGKILL ended every thread: the dead leader is all that is left.
        return 1

    def wait(self, timeout=None):
        return None


class _Oracle:
    """No strace: the unit rows never attach anything to a modelled pid.

    ``stop`` returns ``outcome``, unavailable unless a test sets another.
    """

    available = False
    unavailable = "modelled"
    scope = None
    outcome = OracleOutcome(status=UNAVAILABLE, reasons=["modelled"])

    def __init__(self, directory, *, required=False):
        self.required = required

    def start(self, pids):
        raise AssertionError("the oracle is not available")

    def stop(self, *, confirmed_dead=()):
        return dataclasses.replace(self.outcome, required=self.required)


class _Row:
    """The row entry, and what the modelled row handed to its owner cleanup.

    ``hooks`` maps a tool name to what the modelled actors do while that call
    runs in a row's scripted phase: an owner's log line, a shim record.
    """

    def __init__(self, run, owner, cleaned: list, log: EventLog, hooks: dict):
        self._run = run
        self.owner = owner
        self.cleaned = cleaned
        self.log = log
        self.hooks = hooks

    async def __call__(self, **row):
        return await self._run(**row)


def _process(pid: int, *, cmdline, exe=None, user=ME, status="running"):
    return SimpleNamespace(
        pid=pid, info={"cmdline": cmdline, "exe": exe, "status": status}, user=user
    )


@pytest.fixture
def row(tmp_path, monkeypatch, profile):
    """Run the row with the modelled census and watcher summary given."""
    directory, staged = profile
    healthy = _healthy(profile, daemon=True)
    account = harness.ActorAccount(directory)
    preservation = AsyncMock(return_value=PostQuit(valid=True))
    origin = SimpleNamespace(requests=[], accept_session=lambda _value: None)
    proxy = SimpleNamespace(url="http://127.0.0.1:9", decisions=[])
    owner = harness.OwnerIdentity(
        42, 1.0, "synthetic", str(account.auth_root), _Actor(42)
    )
    cleaned: list = []
    hooks: dict = {}

    async def host(*args, **kwargs):
        origin.requests.extend(healthy.row_requests)
        started = kwargs.get("started")
        if started is not None:
            started(4242)
        await kwargs["after_call"]()
        script = kwargs.get("script")
        if script is not None:
            # A row's scripted phase: every call answers at once.
            async def call(name, arguments):
                began = time.time()
                if name == harness.READ_TOOL:
                    # The origin sees the read's feed request while it runs.
                    origin.requests.extend(
                        dataclasses.replace(request, t=time.time())
                        for request in harness.feed_requests(healthy.row_requests)
                    )
                hook = hooks.get(name)
                if hook is not None:
                    hook()
                return {
                    "tool": name,
                    "began": began,
                    "ended": time.time(),
                    "is_error": False,
                    "read_the_post": name == harness.READ_TOOL,
                }

            await script(call)
        return healthy.host

    def identify(*args, **kwargs):
        # The descriptor names pid 42 until something replaces it; once 42 is
        # killed, it is no owner any longer.
        if owner.process.kills:
            return None, "pid 42 is not running"
        return owner, None

    monkeypatch.setattr(harness, "claim_account", lambda _: account)
    monkeypatch.setattr(harness, "row_identity", lambda: {})
    monkeypatch.setattr(harness, "evidence_refusal", lambda *a, **k: None)
    monkeypatch.setattr(
        harness, "stage_signed_in_session", AsyncMock(return_value=staged)
    )
    monkeypatch.setattr(
        harness, "resolved_browser_executable", AsyncMock(return_value="/b/chrome")
    )
    monkeypatch.setattr(harness, "actor_environment", lambda *a, **k: {})
    monkeypatch.setattr(harness, "Watcher", _Watcher)
    monkeypatch.setattr(harness, "run_host_session", host)
    monkeypatch.setattr(harness, "identify_owner", identify)
    monkeypatch.setattr(harness, "SignalOracle", _Oracle)
    monkeypatch.setattr(_Oracle, "outcome", _Oracle.outcome)
    # A published descriptor, which is what the row looks for before reading.
    published = tmp_path / "descriptor.json"
    published.write_text("{}")
    monkeypatch.setattr(
        harness.daemon_descriptor, "descriptor_path", lambda _root: published
    )
    monkeypatch.setattr(
        harness.daemon_descriptor,
        "read",
        lambda _: SimpleNamespace(
            pid=42, instance_id="synthetic", protocol_version=2, log_path=""
        ),
    )

    def retire(_account, identified):
        cleaned.append(identified)
        return DaemonCleanup("dir", True, False, True, True)

    monkeypatch.setattr(harness, "retire_daemon_state", retire)
    monkeypatch.setattr(harness, "observe_preservation", preservation)
    monkeypatch.setattr(harness, "harness_user", lambda: ME)
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)

    # A frozen runtime's identity, staging and browser, asked of no interpreter.
    monkeypatch.setattr(harness, "frozen_identity", lambda runtime: {})
    monkeypatch.setattr(harness, "frozen_refusal", lambda *a: None)
    monkeypatch.setattr(harness, "stage_frozen_session", lambda *a, **k: None)
    monkeypatch.setattr(harness, "bundled_executable", lambda runtime: "/b/chrome")

    async def run(*, processes, summary, observed=(), **row):
        monkeypatch.setattr(
            harness.psutil, "process_iter", lambda *a, **k: list(processes)
        )
        _Watcher.summary = {**(healthy.watcher or {}), **summary}
        _Watcher.records = list(observed)
        row.setdefault("daemon", True)
        row.setdefault("experiment", "K3")
        result = await measure_host_quit_row(
            profile=directory,
            # Modelled: the row reads only their request and decision logs.
            egress=cast(Any, (origin, proxy)),
            log=EventLog(tmp_path / "evidence", run="gate"),
            work_dir=tmp_path / "row",
            **row,
        )
        return result, preservation.await_count

    return _Row(run, owner, cleaned, EventLog(tmp_path / "evidence", run="gate"), hooks)


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    return directory, staged


_OPEN_POSSIBLE_BROWSER = {
    "pid": 777,
    "possible_browser": True,
    "resolution": "open",
    "failures": ["cmdline: AccessDenied"],
}
_FINISHED_PS = {
    "pid": 778,
    "exe": "/bin/ps",
    "possible_browser": False,
    "resolution": "exited",
    "failures": ["cmdline: AccessDenied"],
}


#: The checkout the modelled rows run, as ``row_identity`` names it.
_HEAD = "c" * 40


def _job_query_row(monkeypatch, tmp_path, native=None, *, module=None) -> ShimVenv:
    """The row's own script, cache, stall host and fate tracking, with one
    installer, 700, found as it starts; *native* answers for its handle.

    The actors import *module*'s process_tree, this checkout's by default,
    and the checkout is at ``_HEAD``.
    """
    store = tmp_path / "store"
    sources = [store / "chromium-1", store / "ffmpeg-2"]
    for source in sources:
        source.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(harness, "install_locations", lambda *a: sources)
    # No browser here to read as installed; test_failed_job_query covers that.
    monkeypatch.setattr(job_query, "record_install", lambda *a: None)
    monkeypatch.setattr(harness, "record_install", lambda *a: None)
    monkeypatch.setattr(harness, "row_identity", lambda: {"head": _HEAD})
    monkeypatch.setattr(harness, "_FAMILY_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(harness, "_LOG_SECONDS", 0.2)

    def installers(observed, *, watch, **kwargs):
        watch(700, 9.0)
        return [(700, 9.0)]

    monkeypatch.setattr(harness, "wait_for_installers", installers)
    if native is not None:
        monkeypatch.setattr(harness, "Fates", lambda: job_query.Fates(native))
    return ShimVenv(
        directory=tmp_path / "shim",
        python=sys.executable,
        source_python=sys.executable,
        site_packages=str(tmp_path),
        shim_sha256=SHIM_SHA256,
        pth_sha256="",
        source_code={},
        code={"module": str(module or Path(linkedin_mcp_server.__file__))},
    )


def _baseline_module(tmp_path: Path) -> Path:
    """Where a baseline runtime's actors would import the pinned process_tree."""
    package = tmp_path / "baseline-code" / "linkedin_mcp_server"
    package.mkdir(parents=True, exist_ok=True)
    (package / "process_tree.py").write_text(baseline_file(_PROCESS_TREE))
    (package / "__init__.py").write_text("")
    return package / "__init__.py"


#: A watcher that observed the whole row: nothing it could not see.
_SETTLED = {
    "read_failures": [],
    "relevant_read_failures": [],
    "stopped_by": "stop file",
    "observation_start": 0.0,
    "observation_end": time.time() + 3600,
    "max_gap_seconds": 0.1,
}


def _owner_log(monkeypatch, tmp_path) -> Path:
    """The daemon log the descriptor names, shared by every owner of the root."""
    log = tmp_path / "daemon.log"
    if not log.exists():
        log.write_text("")
    monkeypatch.setattr(
        harness.daemon_descriptor,
        "read",
        lambda _: SimpleNamespace(
            pid=42, instance_id="synthetic", protocol_version=2, log_path=str(log)
        ),
    )
    return log


def _log_line(log: Path, message: str) -> None:
    with log.open("a") as stream:
        stream.write(json.dumps({"level": "WARNING", "message": message}) + "\n")


def _witness(shim: ShimVenv, **fields: Any) -> None:
    """The shim's record of one planted failure: by default by the owner that
    closes (42, created 1.0), about installer 700 (created 9.0), now."""
    record = {
        "kind": "query",
        "t": time.time(),
        "pid": 42,
        "pid_created": 1.0,
        "member": 700,
        "created": 9.0,
        "job": 55,
        **fields,
    }
    with shim.reached_file.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


class _Marker:
    """The close marker: created at 2, and a clock that held."""

    def mark(self):
        pass

    def held(self):
        return True

    def after(self, pid, start):
        return start > 2.0


def _chain(*, gate: bool = True) -> list[dict]:
    """The frontend the harness started, the owner's gate and the owner (42)."""
    records = [_started(4242, os.getpid(), 0.2, "frontend")]
    if gate:
        records.append(dict(_started(3572, 4242, 0.5, "owner"), start_identity=0.5))
    records.append(_started(42, 4242, 1.0, "owner"))
    return records


async def _k1(row, monkeypatch, tmp_path, *, module=None, witness=False):
    """K1 through the real entry: Direct, the installer running at the close,
    no adopted Job, so nothing plants a failure unless *witness*."""
    shim = _job_query_row(monkeypatch, tmp_path, _Native(), module=module)
    row.hooks.clear()
    if witness:
        row.hooks["close_session"] = lambda: _witness(shim)
    result, _ = await row(
        processes=[],
        summary=_SETTLED,
        daemon=False,
        job_query_shim=shim,
        experiment="K1",
    )
    return result


async def _k2(row, monkeypatch, tmp_path, *, module=None, witness=True, summary=None):
    """K2 through the real entry: the owner that closes (42) reaches the
    planted failure about installer 700 inside its close, confirms its close
    and stays; the family settles, then the recovery."""
    shim = _job_query_row(monkeypatch, tmp_path, _Native(), module=module)
    _owner_log(monkeypatch, tmp_path)
    row.hooks.clear()
    if witness:
        row.hooks["close_session"] = lambda: _witness(shim)
    result, _ = await row(
        processes=[],
        summary=summary or _SETTLED,
        observed=_chain(),
        job_query_shim=shim,
        experiment="K2",
    )
    return result


#: One piece of K3's continuation, taken away or moved, and the problem the
#: common gate then names.
_K3_FAULTS = {
    "no-witness": "no planted failure witnesses the entry",
    "another-actor": "no planted failure witnesses the entry",
    "reused-pid": "no planted failure witnesses the entry",
    "the-successors": "no planted failure witnesses the entry",
    "before-the-close": "no planted failure witnesses the entry",
    "about-the-gate": "no planted failure witnesses the entry",
    "no-consumption": "core.close logged no consumption",
    "an-earlier-owners-consumption": "core.close logged no consumption",
    "no-stand-down": "the owner logged no stand-down",
    "unsettled-family": "no post-settlement recovery",
    "restoration-race": "restoration: the auth root's",
    "protected-change": "by the recovery boundary: the login generation",
    "impossible-successor": "no successor is shown to have served",
    "host-killed": "the harness had to kill the server",
    "no-first-post": "the first call did not read the synthetic post",
    "watcher-gap": "watcher: ",
    "cleanup-swept": "cleanup had to kill browsers",
}


async def _k3(row, monkeypatch, tmp_path, *, fault=None, stood=None):
    """K3 through the real entry, whole unless *fault* names one piece.

    The owner that closes (42) reaches the planted failure about installer 700
    inside its close and logs consuming the drain's False, stands down and is
    seen to exit; the family settles; after the harness's restoration a
    successor (43), begun after the close, serves the probe.
    """
    native = (
        _Native(wait=PermissionError()) if fault == "unsettled-family" else _Native()
    )
    shim = _job_query_row(monkeypatch, tmp_path, native)
    log = _owner_log(monkeypatch, tmp_path)
    if fault == "an-earlier-owners-consumption":
        _log_line(log, harness.NEGATIVE_CONSUMPTION_LINE)
    monkeypatch.setattr(harness, "WallClockMarker", _Marker)
    monkeypatch.setattr(harness, "_SUCCESSOR_SECONDS", 0.3)

    def gone(process, seconds, **kwargs):
        if stood is not None:
            stood.append(time.time())
        if fault != "no-stand-down":
            _log_line(log, harness.STAND_DOWN_LINE)
        return True

    monkeypatch.setattr(harness, "wait_until_dead", gone)
    # Seen before the probe returned: the run starts after these were taken.
    seen = time.time() - 1
    created = 1.5 if fault == "impossible-successor" else seen - 0.5
    successor = harness.OwnerIdentity(
        43, created, "successor", row.owner.auth_root, _Actor(43)
    )
    looked = {"n": 0}

    def identify(*args, **kwargs):
        looked["n"] += 1
        return (successor if looked["n"] > 1 else row.owner), None

    monkeypatch.setattr(harness, "identify_owner", identify)
    witness: dict[str, Any] = {
        "another-actor": {"pid": 99},
        "reused-pid": {"pid_created": 0.5},
        "the-successors": {"pid": 43, "pid_created": created},
        "before-the-close": {"t": time.time() - 3600},
        "about-the-gate": {"member": 3572, "created": 0.5},
    }.get(fault or "", {})

    def close():
        if fault != "no-witness":
            _witness(shim, **witness)
        if fault not in ("no-consumption", "an-earlier-owners-consumption"):
            _log_line(log, harness.NEGATIVE_CONSUMPTION_LINE)
        if fault == "protected-change":
            # A fresh login generation: a relogin nobody asked for.
            write_source_state(Path(row.owner.auth_root) / "profile")

    row.hooks.clear()
    row.hooks["close_session"] = close
    if fault == "restoration-race":

        def racing(*args):
            # Something besides the harness writes whenever it records.
            late = Path(row.owner.auth_root) / "profile" / "late.txt"
            late.write_text(str(time.monotonic_ns()))

        monkeypatch.setattr(job_query, "record_install", racing)
    if fault in ("host-killed", "no-first-post"):
        original = harness.run_host_session
        changes: dict[str, Any] = (
            {"killed_by_harness": True}
            if fault == "host-killed"
            else {"tool": {"is_error": True, "read_the_post": False, "text": ""}}
        )

        async def host(*args, **kwargs):
            return dataclasses.replace(await original(*args, **kwargs), **changes)

        monkeypatch.setattr(harness, "run_host_session", host)
    if fault == "cleanup-swept":
        monkeypatch.setattr(harness, "sweep_browsers", lambda account: [4711])
    summary = dict(_SETTLED)
    if fault == "watcher-gap":
        summary["max_gap_seconds"] = 5.0
    records = [
        *_chain(),
        dict(_started(43, 4242, seen - 0.5, "owner"), start_identity=created),
        _started(50, 43, seen, "driver"),
        _started(51, 50, seen + 0.1, "browser"),
    ]
    result, _ = await row(
        processes=[],
        summary=summary,
        observed=records,
        job_query_shim=shim,
        experiment="K3",
    )
    return result


def _gate(result, experiment: str) -> list[str]:
    return harness.continuation_problems(
        result.continuation, experiment=experiment, revision=_HEAD, run="gate"
    )


async def test_the_failed_job_query_row_writes_through_the_real_event_log(
    row, monkeypatch, tmp_path
):
    # The real EventLog: an event kind the schema does not know fails here.
    result = await _k3(row, monkeypatch, tmp_path)
    assert result.host is not None and result.host.error is None
    records = row.log.records()
    kinds = {record["kind"] for record in records}
    assert {
        "shim.planted",
        "job_query.window",
        "installer.fate",
        "shim.reached",
        "job_query.continuation",
    } <= kinds
    (planted,) = [r for r in records if r["kind"] == "shim.planted"]
    assert planted["shim_sha256"] == SHIM_SHA256
    (continuation,) = [r for r in records if r["kind"] == "job_query.continuation"]
    assert continuation["termination_cause"] == "unobserved"
    assert continuation["evidence"] == "native"
    # The row-private cache is gone and the linked directories are not.
    assert not Path(planted["private_cache"]).exists()
    assert all(source.is_dir() for source in (tmp_path / "store").iterdir()), (
        "a linked directory was removed"
    )


async def test_a_whole_k3_continuation_passes_the_common_gate(
    row, monkeypatch, tmp_path
):
    result = await _k3(row, monkeypatch, tmp_path)
    continuation = result.continuation
    assert continuation is not None
    assert _gate(result, "K3") == []
    assert continuation.termination_cause == harness.UNOBSERVED_CAUSE
    assert [(w["pid"], w["member"]) for w in continuation.witnesses] == [(42, 700)]
    assert continuation.recovery == harness.POST_SETTLEMENT
    # A packet that claims to have seen the caller, or to be more than native
    # evidence, claims what nothing here observed.
    for claim in (
        {"termination_cause": "the routine drain"},
        {"evidence": harness.SOURCE_MODEL},
    ):
        overclaimed = dataclasses.replace(continuation, **claim)
        assert any(
            "nothing native observed the caller" in problem
            for problem in harness.continuation_problems(
                overclaimed, experiment="K3", revision=_HEAD
            )
        )


@pytest.mark.parametrize(
    ("fault", "why"), list(_K3_FAULTS.items()), ids=list(_K3_FAULTS)
)
async def test_each_missing_piece_of_k3_fails_the_common_gate(
    row, monkeypatch, tmp_path, fault, why
):
    result = await _k3(row, monkeypatch, tmp_path, fault=fault)
    problems = _gate(result, "K3")
    assert any(why in problem for problem in problems), problems


async def test_k3_probes_only_after_its_owner_left_and_the_family_settled(
    row, monkeypatch, tmp_path
):
    # K3 on Windows: the probe reached the owner 33 ms after its unconfirmed
    # close, was told the owner was restarting, and no successor was asked
    # for (run 36384952466). Now the owner's exit, then the family's
    # settlement, then the harness's restoration, then the probe.
    stood: list[float] = []
    await _k3(row, monkeypatch, tmp_path, stood=stood)
    windows = [r for r in row.log.records() if r["kind"] == "job_query.window"]
    phases = [w["phase"] for w in windows]
    assert phases.index("close") < phases.index("family settled")
    assert phases.index("family settled") < phases.index("cache restored")
    assert phases.index("cache restored") < phases.index("probe")
    close = next(w for w in windows if w["phase"] == "close")
    settled = next(w for w in windows if w["phase"] == "family settled")
    assert close["ended"] <= stood[0] <= settled["t"]


async def test_an_unsettled_family_gets_no_restoration_and_no_probe(
    row, monkeypatch, tmp_path
):
    result = await _k3(row, monkeypatch, tmp_path, fault="unsettled-family")
    phases = [r["phase"] for r in row.log.records() if r["kind"] == "job_query.window"]
    assert "family settled" in phases
    assert "cache restored" not in phases and "probe" not in phases
    assert result.continuation is not None
    assert result.continuation.recovery.startswith("not made")


async def test_a_whole_k2_continuation_passes_without_any_consumer(
    row, monkeypatch, tmp_path
):
    # The baseline confirms its close and keeps serving: no consumption, no
    # stand-down, no successor, and none is asked of it.
    result = await _k2(row, monkeypatch, tmp_path)
    assert _gate(result, "K2") == []
    missing = await _k2(row, monkeypatch, tmp_path, witness=False)
    assert any("no planted failure" in p for p in _gate(missing, "K2"))


async def test_k1_needs_no_fault_and_refuses_one(row, monkeypatch, tmp_path):
    result = await _k1(row, monkeypatch, tmp_path)
    assert _gate(result, "K1") == []
    assert result.continuation is not None
    assert result.continuation.recovery == harness.NO_RECOVERY
    reached = await _k1(row, monkeypatch, tmp_path, witness=True)
    assert any("no adopted Job" in p for p in _gate(reached, "K1"))


async def test_a_cell_is_judged_against_its_own_experiment_revision_and_run(
    row, monkeypatch, tmp_path
):
    result = await _k2(row, monkeypatch, tmp_path)
    assert "the continuation is K2's, not K3's" in harness.continuation_problems(
        result.continuation, experiment="K3", revision=_HEAD
    )
    other = harness.continuation_problems(
        result.continuation, experiment="K2", revision="d" * 40
    )
    assert any(f"not {'d' * 40}" in p for p in other)
    elsewhere = harness.continuation_problems(
        result.continuation, experiment="K2", revision=_HEAD, run="another"
    )
    assert any("from run gate, not another" in p for p in elsewhere)
    assert harness.continuation_problems(None, experiment="K2", revision=_HEAD) == [
        "K2 left no native continuation"
    ]


@pytest.mark.parametrize(
    ("native", "starts"),
    [
        pytest.param(_Native(), 1, id="an-observed-exit"),
        pytest.param(_Native(wait=PermissionError()), 0, id="a-failed-wait"),
        pytest.param(_Native(exited=None), 0, id="no-kernel-exit"),
        pytest.param(_Native(created=5.0), 0, id="another-lifetime"),
    ],
)
async def test_the_post_quit_session_waits_for_every_installer_fate(
    row, monkeypatch, tmp_path, native, starts
):
    # An installer whose end was not observed may still be running on the
    # profile's setup: no session starts after the row until it is.
    shim = _job_query_row(monkeypatch, tmp_path, native)
    result, calls = await row(
        processes=[],
        summary=_SETTLED,
        daemon=False,
        job_query_shim=shim,
        experiment="K1",
    )
    assert calls == starts, result.failures
    problems = _gate(result, "K1")
    if not starts:
        assert result.post_quit is not None and result.post_quit.valid is None
        assert any("H-R11 evidence incomplete" in f for f in result.failures)
        assert any("installer inventory" in p for p in problems)
    else:
        assert problems == []


@pytest.mark.parametrize("case", ["reused-parent", "delayed-installer"])
@pytest.mark.parametrize("helper_exited", [False, True])
async def test_uncertain_installer_lineage_blocks_preservation_until_exit(
    row, monkeypatch, tmp_path, case, helper_exited
):
    shim = _job_query_row(monkeypatch, tmp_path, _Native())
    helper = dict(_started(702, 700, 9.08, "other"), start_identity=9.02)
    if case == "reused-parent":
        observed = [
            _started(700, os.getpid(), 9.0, "installer"),
            {"kind": "process.exit", "pid": 700, "start_identity": 9.0, "t": 9.03},
            dict(_started(700, os.getpid(), 9.08, "other"), start_identity=9.06),
            helper,
        ]
    else:
        observed = [
            {**helper, "ppid": -1, "t": 9.1, "in_row": False},
            dict(_started(700, os.getpid(), 9.2, "installer"), start_identity=9.0),
            {**helper, "kind": "process.update", "t": 9.2},
            {"kind": "process.exit", "pid": 700, "start_identity": 9.0, "t": 9.3},
        ]
    if helper_exited:
        observed.append(
            {"kind": "process.exit", "pid": 702, "start_identity": 9.02, "t": 9.4}
        )
    lineage = harness.Lineage(observed)
    life = lineage.lifetime(702, 9.02)
    assert life is None or lineage.of(life) != harness.FROM_HARNESS
    result, calls = await row(
        processes=[],
        summary=_SETTLED,
        observed=observed,
        daemon=False,
        job_query_shim=shim,
        experiment="K1",
    )
    problems = _gate(result, "K1")
    if helper_exited:
        assert calls == 1 and problems == [], result.failures
    else:
        assert calls == 0
        assert any("702 (unresolved)" in p for p in problems), problems


@pytest.mark.parametrize(
    ("recorded", "starts"),
    [
        # The watcher recorded it after the first look: the late look watches it.
        pytest.param(True, 1, id="started-late-and-recorded"),
        pytest.param("owner-gate", 1, id="a-known-row-process"),
        # Nothing recorded it: its fate is unknown, and it could be setup's.
        pytest.param(False, 0, id="never-recorded"),
    ],
)
async def test_every_queried_lifetime_must_be_accounted_for(
    row, monkeypatch, tmp_path, recorded, starts
):
    shim = _job_query_row(monkeypatch, tmp_path, _Native())
    # Some actor's drain asked about 702: positive evidence that it existed.
    row.hooks.clear()
    row.hooks["close_session"] = lambda: _witness(shim, pid=1, member=702)
    # Recorded as a late installer the late look watches, or as the owner's
    # own gate (run 36410976409), a row process that is no installer.
    role = "installer" if recorded != "owner-gate" else "owner"
    late = dict(_started(702, 4242, 2.0, role), start_identity=9.0)
    # The frontend, started by the harness: the gate's chain is complete.
    frontend = _started(4242, os.getpid(), 1.0, "frontend")
    result, calls = await row(
        processes=[],
        summary=_SETTLED,
        observed=[frontend, late] if recorded else [],
        job_query_shim=shim,
        experiment="K2",
    )
    assert calls == starts, result.failures
    assert result.continuation is not None
    unaccounted = [p for p in result.continuation.validity if "pid 702" in p]
    assert bool(unaccounted) is (not starts), result.continuation.validity


@pytest.mark.parametrize(
    "fault", ["cleanup", "watcher"], ids=["daemon-dir-survived", "watcher-gap"]
)
async def test_k2_never_accepts_a_cleanup_or_watcher_failure(
    row, monkeypatch, tmp_path, fault
):
    # E1EP-04, through the row: the baseline's own behaviour is K2's to keep,
    # a cleanup that did not finish or a watcher that could not see is not.
    summary = dict(_SETTLED)
    if fault == "cleanup":

        def retire(_account, identified):
            return DaemonCleanup(
                "dir",
                True,
                False,
                True,
                False,
                ("the row's daemon directory survived removal",),
            )

        monkeypatch.setattr(harness, "retire_daemon_state", retire)
    else:
        summary["max_gap_seconds"] = 5.0
    result = await _k2(row, monkeypatch, tmp_path, summary=summary)
    problems = _gate(result, "K2")
    expected = "cleanup: the row's daemon" if fault == "cleanup" else "watcher: "
    assert any(p.startswith(expected) for p in problems), problems


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, None),
        ({"killed_by_harness": True}, "the harness had to kill the server"),
        ({"exited_on_quit": False}, "the server did not exit within"),
        ({"exit_code": 1}, "the server exited abnormally"),
        ({"stdin_closed": False}, "closing the server's stdin failed"),
        ({"alive_before_quit": False}, "the server was already gone"),
    ],
    ids=["clean", "killed", "timeout", "nonzero", "stdin", "already-dead"],
)
async def test_k2_requires_a_successful_host_quit(
    row, monkeypatch, tmp_path, changes, expected
):
    original = harness.run_host_session

    async def host(*args, **kwargs):
        return dataclasses.replace(await original(*args, **kwargs), **changes)

    monkeypatch.setattr(harness, "run_host_session", host)
    result = await _k2(row, monkeypatch, tmp_path)
    assert result.host is not None and result.host.error is None
    problems = _gate(result, "K2")
    host_problems = harness.host_failures(result.host)
    if expected is None:
        assert host_problems == [] and problems == []
    else:
        assert any(expected in problem for problem in host_problems)
        assert all(problem in problems for problem in host_problems)


async def test_failed_private_setup_closes_the_listener_and_removes_its_links(
    row, monkeypatch, tmp_path
):
    store = tmp_path / "store"
    sources = [store / "chromium-1", store / "ffmpeg-2"]
    for source in sources:
        source.mkdir(parents=True)
        (source / "INSTALLATION_COMPLETE").write_text("")
    monkeypatch.setattr(harness, "install_locations", lambda *a: sources)
    monkeypatch.setattr(
        harness,
        "private_install",
        lambda python, locations, env, stall: job_query.private_install(
            python, locations, env, stall, parent=tmp_path
        ),
    )

    def refuse_record(*args):
        raise RuntimeError("private install is not ready")

    monkeypatch.setattr(job_query, "record_install", refuse_record)
    started: list[str] = []

    class TrackedStall(StallHost):
        def start(self):
            super().start()
            started.append(self.url)
            return self

    monkeypatch.setattr(harness, "StallHost", TrackedStall)
    shim = ShimVenv(
        directory=tmp_path / "shim",
        python=sys.executable,
        source_python=sys.executable,
        site_packages=str(tmp_path),
        shim_sha256=SHIM_SHA256,
        pth_sha256="",
        source_code={},
        code={},
    )
    with pytest.raises(RuntimeError, match="private install is not ready"):
        await row(
            processes=[],
            summary={"read_failures": [], "relevant_read_failures": []},
            job_query_shim=shim,
        )
    assert not (tmp_path / "browsers").exists()
    assert all((source / "INSTALLATION_COMPLETE").exists() for source in sources)
    assert len(started) == 1
    port = int(started[0].rsplit(":", 1)[1])
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)


# --- The composition: the source model and every native cell of this run ---------


@pytest.fixture
def cells(row, monkeypatch, tmp_path):
    """K1, K2 and K3 as three real rows of one run, K1 and K2 importing the
    pinned baseline's process_tree and K3 this checkout's."""
    import asyncio

    baseline = _baseline_module(tmp_path)

    async def run():
        k1 = await _k1(row, monkeypatch, tmp_path, module=baseline)
        k2 = await _k2(row, monkeypatch, tmp_path, module=baseline)
        k3 = await _k3(row, monkeypatch, tmp_path)
        return k1.continuation, k2.continuation, k3.continuation

    return asyncio.run(run())


@pytest.fixture(scope="module")
def model():
    return calibrate(
        {
            BASELINE: baseline_file(_PROCESS_TREE),
            CANDIDATE: Path(process_tree.__file__).read_text(encoding="utf-8"),
        }
    )


_REVISIONS = {"K1": _HEAD, "K2": _HEAD, "K3": _HEAD}


def _ledger(*continuations, run="gate") -> harness.R11Ledger:
    ledger = harness.R11Ledger(run)
    for continuation in continuations:
        ledger.record(continuation)
    return ledger


def test_the_composition_holds_for_a_calibrated_whole_run(cells, model):
    assert harness.r11_composition(model, _ledger(*cells), revisions=_REVISIONS) == []


def test_the_composition_needs_a_calibration_from_this_invocation(cells, model):
    problems = harness.r11_composition(None, _ledger(*cells), revisions=_REVISIONS)
    assert problems == ["no source-model calibration ran in this invocation"]
    failing = dataclasses.replace(model, problems=("unknown, candidate: ended 700",))
    problems = harness.r11_composition(failing, _ledger(*cells), revisions=_REVISIONS)
    assert problems == ["source model: unknown, candidate: ended 700"]


@pytest.mark.parametrize("missing", ["K1", "K2", "K3"])
def test_the_composition_needs_every_native_cell(cells, model, missing):
    # K1 and K3 selected without K2 compose nothing, nor does any other pair.
    present = [c for c in cells if c.experiment != missing]
    problems = harness.r11_composition(model, _ledger(*present), revisions=_REVISIONS)
    assert f"{missing}: {missing} left no native continuation" in problems


def test_a_valid_looking_cell_from_another_revision_or_run_fails(cells, model):
    k1, k2, k3 = cells
    moved = dataclasses.replace(k3, revision="d" * 40)
    problems = harness.r11_composition(
        model, _ledger(k1, k2, moved), revisions=_REVISIONS
    )
    assert any(p.startswith("K3: the actors ran dddd") for p in problems), problems
    problems = harness.r11_composition(
        model, _ledger(*cells, run="another"), revisions=_REVISIONS
    )
    assert sum("from run gate, not another" in p for p in problems) == 3


def test_a_cell_that_ran_other_code_than_the_model_fails(cells, model):
    k1, k2, k3 = cells
    swapped = dataclasses.replace(k3, process_tree_sha256=k1.process_tree_sha256)
    problems = harness.r11_composition(
        model, _ledger(k1, k2, swapped), revisions=_REVISIONS
    )
    assert any(p.startswith("K3: the actors imported process_tree") for p in problems)


def test_an_invocation_takes_each_cell_once(cells, model):
    ledger = _ledger(*cells)
    assert harness.r11_composition(model, ledger, revisions=_REVISIONS) == []
    # Emptied by the composition: nothing is left over for a later one.
    again = harness.r11_composition(model, ledger, revisions=_REVISIONS)
    assert {p for p in again if "left no native continuation" in p} == {
        f"{k}: {k} left no native continuation" for k in ("K1", "K2", "K3")
    }
    doubled = harness.r11_composition(
        model, _ledger(*cells, cells[2]), revisions=_REVISIONS
    )
    assert "a second K3 continuation in one invocation" in doubled


def test_k3_worse_than_k1_fails_the_composition(cells, model):
    k1, k2, k3 = cells
    assert k3.vector is not None
    worse = dataclasses.replace(
        k3, vector=dataclasses.replace(k3.vector, o4_session="lost")
    )
    problems = harness.r11_composition(
        model, _ledger(k1, k2, worse), revisions=_REVISIONS
    )
    assert any(p.startswith("K3 differs from K1 frozen: o4_session") for p in problems)


async def test_a_settled_complete_census_starts_the_post_quit_session_once(row):
    result, calls = await row(
        processes=[
            _process(10, cmdline=["python", "server"]),
            _process(11, cmdline=None, user="root"),
        ],
        summary={"read_failures": [_FINISHED_PS], "relevant_read_failures": []},
    )
    assert calls == 1
    assert result.post_quit is not None and result.post_quit.valid is True


@pytest.mark.parametrize(
    ("processes", "summary", "reported"),
    [
        pytest.param(
            [_process(777, cmdline=None)],
            {"relevant_read_failures": [_OPEN_POSSIBLE_BROWSER]},
            "incomplete",
            id="denied-arguments-and-open-episode",
        ),
        pytest.param(
            [_process(777, cmdline=None)],
            {"relevant_read_failures": []},
            "incomplete",
            id="denied-arguments",
        ),
        pytest.param(
            [_process(777, cmdline=None, exe="/b/chrome")],
            {"relevant_read_failures": []},
            "incomplete",
            id="denied-arguments-browser-exe",
        ),
        pytest.param(
            [],
            {"relevant_read_failures": [_OPEN_POSSIBLE_BROWSER]},
            "unresolved possible browsers",
            id="open-episode",
        ),
    ],
)
async def test_an_unsettled_census_starts_nothing(row, processes, summary, reported):
    result, calls = await row(processes=processes, summary=summary)
    assert calls == 0
    assert result.post_quit is not None and result.post_quit.valid is None
    assert any(reported in failure for failure in result.post_quit.failures)
    assert any("post-quit not run" in failure for failure in result.failures)


def _retained(profile: str) -> dict:
    """The watcher's note of a known root whose later argument read failed."""
    return {
        "pid": 777,
        "start_identity": 6.0,
        "exe": "/b/chrome",
        "first": 1.0,
        "last": 2.0,
        "failures": ["cmdline: AccessDenied"],
        "possible_browser": False,
        "resolution": "a known browser root's earlier reading retained",
        "retained_profile": profile,
    }


def _retained_summary(key: str, retained_profile: str) -> dict:
    return {
        "read_failures": [_retained(retained_profile)],
        "relevant_read_failures": [],
        "observation_start": 0.0,
        "observation_end": time.time() + 60.0,
        "max_roots": {key: 1},
    }


async def test_a_retained_row_root_does_not_settle_a_census_that_cannot_read_it(
    row, profile
):
    # The watcher's retained reading is history; the launch waits on what the
    # census reads now, and it cannot read the browser's arguments.
    key = harness.ActorAccount(profile[0]).browser_key
    result, calls = await row(
        processes=[_process(777, cmdline=None, exe="/b/chrome")],
        summary=_retained_summary(key, key),
    )
    assert calls == 0
    assert result.post_quit is not None and result.post_quit.valid is None
    assert any("incomplete" in failure for failure in result.post_quit.failures)


async def test_an_empty_profile_permits_the_session_and_keeps_o1_open(
    row, profile, tmp_path
):
    # A root retained on another profile: nothing is on the row's profile now,
    # so the session may start, but the row's O1 stays unestablished.
    key = harness.ActorAccount(profile[0]).browser_key
    result, calls = await row(
        processes=[], summary=_retained_summary(key, str(tmp_path / "elsewhere"))
    )
    assert calls == 1
    assert result.post_quit is not None and result.post_quit.valid is True
    assert result.vector is not None and not result.vector.o1_single_browser
    assert any("possible browser" in failure for failure in result.failures)


def test_the_census_keeps_a_denied_reading_apart_from_an_empty_one(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    ours = [
        str(tmp_path / "browsers" / "chrome"),
        f"--user-data-dir={account.profile}",
    ]
    census = harness.profile_census(
        account,
        browser_dir=tmp_path / "browsers",
        process_iter=lambda *a, **k: [
            _process(1, cmdline=ours),
            _process(2, cmdline=None),
            _process(3, cmdline=None, user="root"),
            _process(4, cmdline=None, exe="/bin/ps"),
            _process(5, cmdline=["python"]),
        ],
        user=ME,
    )
    assert census.pids == [1]
    assert census.unresolved == [2]
    assert not census.complete


def test_other_users_and_known_non_browsers_leave_the_census_complete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_dir=tmp_path / "browsers",
        process_iter=lambda *a, **k: [
            _process(3, cmdline=None, user="root"),
            _process(4, cmdline=None, exe="/bin/ps"),
        ],
        user=ME,
    )
    assert census.complete and census.pids == []


@pytest.mark.parametrize(
    ("user", "unresolved"),
    [
        pytest.param("root", [7], id="another-user-but-own-user-unknown"),
        pytest.param(ME, [7], id="same-user"),
    ],
)
def test_an_unknown_harness_user_excludes_nobody_by_user(
    tmp_path, monkeypatch, user, unresolved
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    monkeypatch.setattr(harness, "harness_user", lambda: None)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_exe="/b/chrome",
        process_iter=lambda *a, **k: [
            _process(7, cmdline=None, exe="/b/chrome", user=user)
        ],
    )
    assert census.unresolved == unresolved


def test_a_known_other_user_is_excluded_from_the_census(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        browser_exe="/b/chrome",
        process_iter=lambda *a, **k: [
            _process(7, cmdline=None, exe="/b/chrome", user="root")
        ],
        user=ME,
    )
    assert census.complete


@pytest.mark.parametrize(
    ("linux", "threads", "complete"),
    [
        pytest.param(True, 1, True, id="linux-leader-alone"),
        pytest.param(True, 2, False, id="linux-leader-with-live-threads"),
        pytest.param(True, None, False, id="linux-threads-unreadable"),
        pytest.param(False, 2, True, id="macos-or-windows"),
        pytest.param(False, None, True, id="macos-or-windows-threads-unreadable"),
    ],
)
def test_a_zombie_leaves_the_census_only_once_the_whole_process_exited(
    tmp_path, monkeypatch, linux, threads, complete
):
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    monkeypatch.setattr(
        harness,
        "exited_zombie",
        functools.partial(
            harness.exited_zombie, linux=linux, threads_of=lambda _: threads
        ),
    )
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    census = harness.profile_census(
        account,
        process_iter=lambda *a, **k: [_process(6, cmdline=[], status="zombie")],
        user=ME,
    )
    assert census.complete is complete
    assert census.unresolved == ([] if complete else [6])


def _status(pid: int, want: str, seconds: float = 10.0) -> bool:
    """Wait, without reaping, until *pid* reports *want*."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == want:
                return True
        except psutil.NoSuchProcess:
            return False
        time.sleep(0.02)
    return False


# The leader ends with pthread_exit while a worker thread keeps the process,
# and the lock it took, alive. Python cannot end its main thread on its own
# (the interpreter waits for the others), so libc does it.
_ZOMBIE_LEADER = """
import ctypes, fcntl, os, sys, threading
lock = open(sys.argv[1], "w")
fcntl.flock(lock, fcntl.LOCK_EX)
try:
    pthread_exit = ctypes.CDLL(None).pthread_exit
except AttributeError:
    print("no pthread_exit", flush=True)
    sys.exit(0)
pthread_exit.argtypes = [ctypes.c_void_p]
started = threading.Event()
def worker():
    started.set()
    sys.stdin.buffer.read(1)
    os._exit(0)
threading.Thread(target=worker).start()
started.wait()
print("ready", flush=True)
pthread_exit(None)
"""


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="a zombie leader with live threads is Linux's; elsewhere a zombie "
    "has exited as a whole",
)
def test_a_real_zombie_leader_with_a_live_thread_stays_unresolved(tmp_path):
    import fcntl

    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    lockfile = tmp_path / "lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _ZOMBIE_LEADER,
            str(lockfile),
            f"--user-data-dir={account.profile}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().decode().strip()
        if line == "no pthread_exit":
            pytest.skip("libc exports no pthread_exit to ctypes here")
        assert line == "ready"
        assert _status(child.pid, psutil.STATUS_ZOMBIE), "the leader never exited"
        with lockfile.open("w") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        census = harness.profile_census(account)
        assert child.pid in census.unresolved
        assert not census.complete
    finally:
        # Bounded however the body ended: release the worker, then force it.
        with contextlib.suppress(OSError, ValueError):
            assert child.stdin is not None
            child.stdin.write(b"x")
            child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        if child.stdout is not None:
            child.stdout.close()


@pytest.mark.skipif(os.name == "nt", reason="psutil reports no zombie on Windows")
def test_a_real_fully_exited_unreaped_child_leaves_the_census(tmp_path):
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    child = subprocess.Popen(
        [sys.executable, "-c", "pass", f"--user-data-dir={account.profile}"]
    )
    try:
        assert _status(child.pid, psutil.STATUS_ZOMBIE), "it never became a zombie"
        census = harness.profile_census(account)
        assert child.pid not in census.unresolved
        assert child.pid not in census.pids
    finally:
        child.wait(timeout=10)


# --- H-R6: the kill path, through the row entry ----------------------------------


def _guardian(principal: int, group: int) -> dict:
    return {
        "kind": "process.start",
        "actor": "guardian",
        "in_row": True,
        "pid": principal + 1,
        "ppid": principal,
        "cmdline": ["python", "-I", "-S", "-u", "/x/process_guardian.py", "5", "6"]
        + [str(group)],
    }


def _killed_events(row) -> list[dict]:
    return [r for r in row.log.records() if r["kind"] == "actor.killed"]


async def test_the_daemon_rows_kill_reaches_the_owner_and_is_reported(row):
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 42)],
        kill_actor=True,
        row="H-R6",
    )
    assert row.owner.process.kills == 1
    # A killed owner lingers as a zombie until its parent reaps it: dead.
    assert result.killed is not None
    assert result.killed["exit"] == "killed"
    if os.name != "nt":
        assert result.killed["guardian_owner_group"] == 42
    (event,) = _killed_events(row)
    assert (event["actor"], event["role"], event["pid"]) == ("harness", "owner", 42)


async def test_an_owner_nobody_replaced_is_still_handed_to_cleanup(row):
    # The frontend published no new owner after the kill: cleanup gets the
    # killed owner's handle, so it can settle that owner rather than meeting
    # its descriptor as one the row never identified.
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 0)],
        kill_actor=True,
        row="H-R6",
    )
    assert row.cleaned == [row.owner]
    assert result.owner is not None
    assert result.owner["replaced_after_kill"] is False
    assert result.owner["exit"]["how"] == "exited"


async def test_the_killed_zombie_found_again_is_no_successor(row, monkeypatch):
    # The stale descriptor still names the killed owner, unreaped, with its
    # own create time: identifying that lifetime again replaces nothing.
    monkeypatch.setattr(harness, "identify_owner", lambda *a, **k: (row.owner, None))
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(42, 0)],
        kill_actor=True,
        row="H-R6",
    )
    assert result.owner is not None
    assert result.owner["replaced_after_kill"] is False


async def test_the_direct_rows_kill_reaches_the_associated_server(row, monkeypatch):
    server = _Actor(4242)
    monkeypatch.setattr(
        harness,
        "associate_server",
        lambda pid, observed: (server, 1.0) if pid == 4242 else (None, None),
    )
    result, _ = await row(
        processes=[],
        summary={},
        observed=[_guardian(4242, 0)],
        kill_actor=True,
        daemon=False,
        experiment="K1",
        row="H-R6",
    )
    assert server.kills == 1 and row.owner.process.kills == 0
    assert result.killed is not None and result.killed["exit"] == "killed"
    (event,) = _killed_events(row)
    assert (event["role"], event["pid"]) == ("frontend", 4242)


# --- O2 through the row entry ---------------------------------------------------


def _timeline(*records: dict, ends=(1.0, 2.0, 5.0, 6.0, 6.5, 7.0)) -> list[dict]:
    """Watcher records with the ready line and a summary that logs its samples."""
    return [
        {"kind": "watcher.ready", "t": 0.5, "baseline_pgids": [1]},
        *records,
        {
            "kind": "watcher.summary",
            "t": ends[-1],
            "sample_log": [[end - 0.01, end, 100000] for end in ends],
            "read_failures": [],
        },
    ]


def _started(pid, ppid, t, actor, *, in_row=True):
    return {
        "kind": "process.start",
        "t": t,
        "pid": pid,
        "ppid": ppid,
        "pgid": pid,
        "start_identity": t,
        "in_row": in_row,
        "actor": actor,
    }


async def test_a_signal_is_reported_as_a_signal_and_never_as_a_death(row, monkeypatch):
    # The traced owner sent a SIGTERM outside its launched set. That fails
    # O2, as a delivered signal: nothing observed a death.
    monkeypatch.setattr(
        _Oracle,
        "outcome",
        OracleOutcome(
            status=COMPLETE,
            traced=[42],
            attached_at=5.0,
            calls=parse_strace("42  6.0 kill(95, SIGTERM) = 0\n"),
        ),
    )
    result, _ = await row(
        processes=[],
        summary={},
        observed=_timeline(
            _started(42, 1, 1.0, "owner"), _started(95, 7, 2.0, "other", in_row=False)
        ),
    )
    assert result.vector is not None and result.vector.o2 == "violated"
    kinds = [r["kind"] for r in row.log.records()]
    assert "process.death_unattributed" not in kinds
    (violation,) = [r for r in row.log.records() if r["kind"] == "signal.violation"]
    assert "delivered SIGTERM" in violation["violation"]
    assert any(f.startswith("O2 violation") for f in result.failures)


class _Helpers:
    """Canaries that record their own teardown."""

    stopped = 0

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return []

    def outside_the_harness(self):
        return []

    def deaths(self):
        return []

    def stop(self):
        _Helpers.stopped += 1


async def test_a_helper_that_fails_to_stop_skips_no_other_teardown(row, monkeypatch):
    class Stuck(_Watcher):
        def stop(self):
            raise RuntimeError("the watcher would not stop")

    monkeypatch.setattr(harness, "Watcher", Stuck)
    monkeypatch.setattr(harness, "Canaries", _Helpers)
    monkeypatch.setattr(_Helpers, "stopped", 0)
    result, _ = await row(processes=[], summary={})
    assert _Helpers.stopped == 1
    assert any("the watcher could not be stopped" in f for f in result.failures)
    assert row.cleaned, "the owner's cleanup still ran"


async def test_a_watcher_that_never_started_still_has_its_helpers_ended(
    row, monkeypatch
):
    class Failing(_Watcher):
        def start(self):
            raise RuntimeError("no baseline")

    monkeypatch.setattr(harness, "Watcher", Failing)
    monkeypatch.setattr(harness, "Canaries", _Helpers)
    monkeypatch.setattr(_Helpers, "stopped", 0)
    with pytest.raises(RuntimeError, match="no baseline"):
        await row(processes=[], summary={})
    assert _Helpers.stopped == 1
