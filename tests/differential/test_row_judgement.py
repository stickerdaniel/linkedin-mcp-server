"""The row's verdict, its owner cleanup, and K0, each from modelled observations.

The native row cannot run here, so these drive the functions it decides with:
``judge_row`` on observations that differ from a healthy row in one respect
each, ``settle_owner`` and ``retire_daemon_state`` on a modelled process and
descriptor, and the K0 test itself with its row replaced. Every family has a
clean control, so a verdict that refuses everything cannot pass.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from differential import harness
from differential import test_host_quit_row as rows
from differential.harness import (
    DaemonCleanup,
    HostSession,
    Observations,
    OwnerIdentity,
    PostQuit,
    PublishedOwner,
    RowResult,
    compare_to_direct,
    identify_owner,
    judge_row,
    preservation_refusals,
    repeat_verdict,
    retire_daemon_state,
    settle_owner,
)
from differential.session import (
    LAST_VERSION_FILE,
    RETAINED,
    snapshot,
    write_synthetic_cookie_file,
)
from differential.synthetic_origin import OriginRequest
from differential.test_watcher import BROWSER_EXE, _sampler
from differential.watcher import Tracker, canonical_user_data_dir
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

KEY = "/tmp/differential-row-profile"


# --- judge_row -----------------------------------------------------------------


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "auth" / "profile"
    directory.mkdir(parents=True)
    (directory / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(directory))
    write_source_state(directory)
    return directory, staged


def _healthy(profile, *, daemon: bool = True, **changes) -> Observations:
    directory, staged = profile
    shot = snapshot(directory, expected_digest=staged.li_at_digest)
    host = HostSession(
        stderr=["INFO Forwarding to the shared browser owner"] if daemon else [],
        tool={"is_error": False, "read_the_post": True, "text": "feed"},
        alive_before_quit=True,
        stdin_closed=True,
        exited_on_quit=True,
        exit_code=0,
    )
    host.user_lines = list(host.stderr)
    observed = Observations(
        daemon=daemon,
        browser_key=KEY,
        host=host,
        owner={"pid": 4321, "exit": {"how": "exited"}} if daemon else {},
        cleanup=DaemonCleanup("dir", True, False, True, True),
        swept=[],
        residual=[],
        watcher={
            "stopped_by": "stop file",
            "observation_start": 10.0,
            "observation_end": 100.0,
            "max_gap_seconds": 0.2,
            "relevant_read_failures": [],
            "max_roots": {KEY: 1},
        },
        actors_began=11.0,
        actors_ended=99.0,
        row_requests=[
            OriginRequest(
                "www.linkedin.com",
                "www.linkedin.com",
                "/feed/",
                ("li_at",),
                t=20.0,
                session_valid=True,
            )
        ],
        before=shot,
        after=shot,
        post_quit=PostQuit(valid=True),
    )
    return dataclasses.replace(observed, **changes)


@pytest.mark.parametrize("daemon", [True, False])
def test_a_healthy_row_has_no_failures(profile, daemon):
    vector, failures = judge_row(_healthy(profile, daemon=daemon))
    assert failures == []
    assert vector.o4_session == RETAINED
    assert vector.o1_single_browser and vector.watcher_healthy


def _watcher(profile, **summary):
    healthy = _healthy(profile)
    return dataclasses.replace(healthy, watcher={**(healthy.watcher or {}), **summary})


@pytest.mark.parametrize(
    ("observed", "reported"),
    [
        (lambda p: _watcher(p, stopped_by="deadline"), "stopped by 'deadline'"),
        (lambda p: _watcher(p, observation_end=50.0), "ended before the actors"),
        (lambda p: _watcher(p, observation_start=12.0), "began after the actors"),
        (lambda p: _watcher(p, max_gap_seconds=3.5), "largest gap"),
        (
            lambda p: _watcher(
                p, relevant_read_failures=[{"pid": 7, "failure": "cmdline"}]
            ),
            "anything but a possible browser",
        ),
        (lambda p: dataclasses.replace(_healthy(p), watcher=None), "no summary"),
    ],
)
def test_an_incomplete_observation_cannot_carry_o1(profile, observed, reported):
    vector, failures = judge_row(observed(profile))
    assert not vector.watcher_healthy
    assert not vector.o1_single_browser
    assert any(reported in failure for failure in failures), failures


def test_a_second_browser_fails_o1(profile):
    vector, failures = judge_row(_watcher(profile, max_roots={KEY: 2}))
    assert not vector.o1_single_browser
    assert any("O1" in failure for failure in failures)


def _host(profile, **changes):
    healthy = _healthy(profile)
    return dataclasses.replace(
        healthy, host=dataclasses.replace(healthy.host, **changes)
    )


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        ({"exit_code": 23}, "status 23"),
        ({"exit_code": -9}, "status -9"),
        ({"alive_before_quit": False}, "already gone"),
        ({"stdin_closed": False, "stdin_close_error": "BrokenPipe"}, "stdin failed"),
        ({"killed_by_harness": True, "exit_code": -9}, "had to kill"),
        ({"exited_on_quit": False, "exit_code": None}, "did not exit"),
        ({"error": "TimeoutError: init"}, "host session failed"),
    ],
)
def test_an_abnormal_quit_is_not_a_host_quit(profile, changes, reported):
    vector, failures = judge_row(_host(profile, **changes))
    assert not vector.host_exit_clean
    assert any(reported in failure for failure in failures), failures


def test_corruption_after_the_call_and_before_exit_fails_o4(profile):
    directory, staged = profile
    healthy = _healthy(profile)
    path = portable_cookie_path(directory)
    path.write_text(path.read_text().replace(staged.li_at, "synthetic-replaced"))
    after = snapshot(directory, expected_digest=staged.li_at_digest)
    vector, failures = judge_row(
        dataclasses.replace(healthy, after=after, post_quit=PostQuit(valid=False))
    )
    assert vector.tool_succeeded and vector.origin_saw_feed
    assert vector.o4_session != RETAINED
    assert any("O4" in failure for failure in failures)


def test_a_session_the_origin_rejected_after_quit_fails_o4(profile):
    vector, failures = judge_row(
        dataclasses.replace(_healthy(profile), post_quit=PostQuit(valid=False))
    )
    assert vector.o4_session != RETAINED
    assert any("O4" in failure for failure in failures)


def test_a_row_whose_feed_carried_another_session_fails(profile):
    healthy = _healthy(profile)
    request = dataclasses.replace(healthy.row_requests[0], session_valid=False)
    vector, failures = judge_row(dataclasses.replace(healthy, row_requests=[request]))
    assert not vector.feed_carried_session
    assert any("staged session" in failure for failure in failures)


def test_a_daemon_row_that_fell_back_fails(profile):
    healthy = _healthy(profile)
    host = dataclasses.replace(healthy.host, stderr=[], user_lines=[])
    vector, failures = judge_row(dataclasses.replace(healthy, host=host))
    assert vector.fell_back
    assert any("fell back" in failure for failure in failures)


def test_a_direct_row_that_reached_an_owner_fails(profile):
    healthy = _healthy(profile, daemon=False)
    vector, failures = judge_row(
        dataclasses.replace(healthy, owner={"descriptor_present": True})
    )
    assert vector.owner_published
    assert any("reached a shared owner" in failure for failure in failures)


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        ({"swept": [999]}, "had to kill browsers"),
        ({"residual": [998]}, "outlived the row"),
        (
            {"cleanup": DaemonCleanup("dir", True, True, True, True)},
            "cleanup had to intervene",
        ),
        (
            {"cleanup": DaemonCleanup("dir", True, False, False, False, ("kept",))},
            "kept",
        ),
    ],
)
def test_a_cleanup_intervention_fails_the_row(profile, changes, reported):
    vector, failures = judge_row(dataclasses.replace(_healthy(profile), **changes))
    assert not vector.cleanup_clean
    assert any(reported in failure for failure in failures), failures


# --- Owner cleanup ---------------------------------------------------------------


class _Process:
    """A modelled owner process: the handle the row kept when it found it."""

    def __init__(
        self,
        *,
        running=True,
        stops_on_kill=True,
        created=100.0,
        liveness_error=None,
        wait_error=None,
        zombie=False,
        threads=1,
        cmdline=("python", "-P", "-m", "linkedin_mcp_server.daemon_owner"),
    ):
        self.running = running
        self.zombie = zombie
        self.threads = threads
        # Only ``thread_count``'s fallback reads it: a pid no process has.
        self.pid = -1
        self.stops_on_kill = stops_on_kill
        self.created = created
        self.liveness_error = liveness_error
        self.wait_error = wait_error
        self._cmdline = list(cmdline)
        self.kills = 0

    def create_time(self):
        if isinstance(self.created, BaseException):
            raise self.created
        return self.created

    def cmdline(self):
        return self._cmdline

    def is_running(self):
        if self.liveness_error is not None:
            raise self.liveness_error
        return self.running

    def kill(self):
        self.kills += 1
        if self.stops_on_kill:
            self.running = False

    def wait(self, timeout=None):
        if self.wait_error is not None:
            raise self.wait_error
        if self.running:
            raise psutil.TimeoutExpired(timeout)

    def num_threads(self):
        if self.threads is None:
            raise psutil.AccessDenied(4321)
        return self.threads

    def status(self):
        if self.kills and self.wait_error is not None:
            raise self.wait_error
        if not self.running:
            raise psutil.NoSuchProcess(4321)
        return psutil.STATUS_ZOMBIE if self.zombie else psutil.STATUS_SLEEPING


@pytest.fixture
def no_pid_lookup(monkeypatch):
    """Fails the test if cleanup looks a process up by pid at all."""
    looked_up: list[int] = []

    def refuse(pid, *args, **kwargs):
        looked_up.append(pid)
        raise AssertionError(f"cleanup looked up pid {pid}")

    monkeypatch.setattr(psutil, "Process", refuse)
    return looked_up


def _owner(process, *, instance="instance-a", auth_root="/auth"):
    return OwnerIdentity(4321, 100.0, instance, auth_root, process)


def test_the_same_rows_live_owner_is_stopped_through_its_handle(no_pid_lookup):
    process = _Process()
    disposition = settle_owner(
        _owner(process), PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert (disposition.gone, disposition.signalled, disposition.failures) == (
        True,
        True,
        (),
    )
    assert process.kills == 1 and no_pid_lookup == []


def test_an_owner_that_already_exited_is_not_signalled(no_pid_lookup):
    process = _Process(running=False)
    disposition = settle_owner(
        _owner(process), PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert (disposition.gone, disposition.signalled) == (True, False)
    assert process.kills == 0


def test_a_killed_owner_its_parent_has_not_reaped_is_gone(no_pid_lookup):
    # H-R6 killed it; a zombie still reads as running, but has ended: on
    # Linux its dead leader is the only thread left.
    process = _Process(zombie=True, threads=1)
    disposition = settle_owner(
        _owner(process),
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root="/auth",
        linux=True,
    )
    assert (disposition.gone, disposition.signalled) == (True, False)
    assert process.kills == 0


@pytest.mark.parametrize(
    "threads",
    [pytest.param(2, id="live-threads"), pytest.param(None, id="count-unreadable")],
)
def test_a_zombie_leader_that_may_have_live_threads_is_not_gone(no_pid_lookup, threads):
    # ``exited_zombie``'s contract: on Linux only the dead leader alone is an
    # exited process. Such an owner is killed and waited for like a live one.
    process = _Process(zombie=True, threads=threads, stops_on_kill=False)
    disposition = settle_owner(
        _owner(process),
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root="/auth",
        wait_seconds=0.05,
        linux=True,
    )
    assert not disposition.gone and disposition.signalled
    assert process.kills == 1


def test_on_macos_and_windows_a_zombie_is_the_whole_process(no_pid_lookup):
    process = _Process(zombie=True, threads=2)
    disposition = settle_owner(
        _owner(process),
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root="/auth",
        linux=False,
    )
    assert disposition.gone and process.kills == 0


def test_a_stale_pid_now_naming_another_owner_is_never_signalled(no_pid_lookup):
    # The row's owner exited and its pid went to another owner. The kept handle
    # knows its own lifetime ended; the new process at that pid is never asked.
    ours = _Process(running=False)
    theirs = _Process()
    disposition = settle_owner(
        _owner(ours), PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert disposition.gone and not disposition.signalled
    assert ours.kills == 0 and theirs.kills == 0 and no_pid_lookup == []


def test_a_descriptor_naming_another_instance_is_refused(no_pid_lookup):
    process = _Process()
    disposition = settle_owner(
        _owner(process), PublishedOwner(4321, "instance-b"), None, auth_root="/auth"
    )
    assert not disposition.gone and not disposition.signalled
    assert process.kills == 0
    assert "instance-b" in disposition.failures[0]


def test_an_owner_of_another_auth_root_is_refused(no_pid_lookup):
    process = _Process()
    disposition = settle_owner(
        _owner(process, auth_root="/elsewhere"),
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root="/auth",
    )
    assert not disposition.gone and process.kills == 0


def test_an_owner_the_row_never_identified_is_refused(no_pid_lookup):
    disposition = settle_owner(
        None, PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert not disposition.gone and not disposition.signalled
    assert "never identified" in disposition.failures[0]


def test_an_unreadable_descriptor_is_refused(no_pid_lookup):
    process = _Process()
    disposition = settle_owner(
        _owner(process), None, "DescriptorError", auth_root="/auth"
    )
    assert not disposition.gone and process.kills == 0


def test_an_owner_that_survives_its_kill_is_not_gone(no_pid_lookup):
    process = _Process(stops_on_kill=False)
    disposition = settle_owner(
        _owner(process),
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root="/auth",
        wait_seconds=0.01,
    )
    assert disposition.signalled and not disposition.gone
    assert "still running" in disposition.failures[0]


def test_a_descriptor_naming_another_pid_is_refused(no_pid_lookup):
    process = _Process()
    disposition = settle_owner(
        _owner(process), PublishedOwner(4322, "instance-a"), None, auth_root="/auth"
    )
    assert disposition.state == "unknown" and not disposition.gone
    assert process.kills == 0
    assert "pid 4322" in disposition.failures[0]


def test_liveness_that_cannot_be_read_is_unknown_not_gone(no_pid_lookup):
    process = _Process(liveness_error=psutil.AccessDenied(4321))
    disposition = settle_owner(
        _owner(process), PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert disposition.state == "unknown" and not disposition.gone
    assert process.kills == 0 and "liveness" in disposition.failures[0]


def test_an_exit_that_cannot_be_confirmed_is_unknown(no_pid_lookup):
    process = _Process(stops_on_kill=False, wait_error=psutil.AccessDenied(4321))
    disposition = settle_owner(
        _owner(process), PublishedOwner(4321, "instance-a"), None, auth_root="/auth"
    )
    assert disposition.state == "unknown" and disposition.signalled
    assert not disposition.gone


def test_nothing_published_and_nothing_identified_is_gone(no_pid_lookup):
    disposition = settle_owner(None, None, None, auth_root="/auth")
    assert (disposition.gone, disposition.signalled, disposition.failures) == (
        True,
        False,
        (),
    )


@pytest.fixture
def row_state(tmp_path, monkeypatch):
    """A row's daemon directory, redirected to a temporary one."""
    directory = tmp_path / "daemon-state" / "row"
    directory.mkdir(parents=True)
    (directory / "daemon.json").write_text("{}")
    published: dict[str, object] = {}
    monkeypatch.setattr(harness.daemon_descriptor, "daemon_dir", lambda _: directory)

    def read(_):
        if "error" in published:
            raise harness.daemon_descriptor.DescriptorError("corrupt")
        return published.get("descriptor")

    monkeypatch.setattr(harness.daemon_descriptor, "read", read)
    account = SimpleNamespace(auth_root=Path("/auth"))
    return directory, published, account


def test_cleanup_removes_the_directory_once_the_owner_is_gone(row_state):
    directory, published, account = row_state
    published["descriptor"] = SimpleNamespace(pid=4321, instance_id="instance-a")
    process = _Process()
    cleanup = retire_daemon_state(account, _owner(process))
    assert cleanup.cleaned and cleanup.owner_gone and cleanup.signalled
    assert not directory.exists()


@pytest.mark.parametrize(
    "state",
    [
        {"descriptor": SimpleNamespace(pid=4321, instance_id="instance-b")},
        {"error": True},
    ],
)
def test_cleanup_keeps_the_directory_when_the_owner_is_unconfirmed(row_state, state):
    directory, published, account = row_state
    published.update(state)
    process = _Process()
    cleanup = retire_daemon_state(account, _owner(process))
    assert not cleanup.cleaned and not cleanup.owner_gone
    assert directory.exists() and process.kills == 0
    assert any("kept" in failure for failure in cleanup.failures)


def test_cleanup_keeps_the_directory_when_liveness_is_unreadable(row_state):
    directory, published, account = row_state
    published["descriptor"] = SimpleNamespace(pid=4321, instance_id="instance-a")
    process = _Process(liveness_error=psutil.AccessDenied(4321))
    cleanup = retire_daemon_state(account, _owner(process))
    assert directory.exists() and not cleanup.owner_gone and not cleanup.cleaned


def test_cleanup_keeps_the_directory_of_a_zombie_leader_with_live_threads(
    row_state,
):
    directory, published, account = row_state
    published["descriptor"] = SimpleNamespace(pid=4321, instance_id="instance-a")
    process = _Process(zombie=True, threads=2, stops_on_kill=False)
    cleanup = retire_daemon_state(
        account, _owner(process), linux=True, wait_seconds=0.05
    )
    assert directory.exists() and not cleanup.owner_gone and not cleanup.cleaned


def test_cleanup_keeps_the_directory_of_an_unidentified_owner(row_state):
    directory, published, account = row_state
    published["descriptor"] = SimpleNamespace(pid=4321, instance_id="instance-a")
    cleanup = retire_daemon_state(account, None)
    assert directory.exists() and not cleanup.cleaned


# --- Owner association -------------------------------------------------------------


def _row_account(tmp_path):
    auth = tmp_path / "auth"
    (auth / "profile").mkdir(parents=True)
    return harness.ActorAccount(auth / "profile")


def _published(account, *, pid=4321, instance="instance-a", profile=None):
    return SimpleNamespace(
        pid=pid,
        instance_id=instance,
        profile_path=str(profile if profile is not None else account.profile),
    )


def _observed_owner(pid=4321, start=100.0, *, in_row=True, actor="owner"):
    return [
        {
            "kind": "process.start",
            "actor": actor,
            "pid": pid,
            "start_identity": start,
            "in_row": in_row,
        }
    ]


def test_an_owner_the_watcher_saw_this_row_start_is_identified(tmp_path):
    account = _row_account(tmp_path)
    process = _Process()
    identity, problem = identify_owner(
        _published(account), account, _observed_owner(), open_process=lambda _: process
    )
    assert problem is None and identity is not None
    assert (identity.pid, identity.create_time, identity.instance_id) == (
        4321,
        100.0,
        "instance-a",
    )
    assert identity.auth_root == str(account.auth_root)


def test_an_owner_first_seen_before_its_exec_is_identified_by_its_update(tmp_path):
    account = _row_account(tmp_path)
    observed = [
        {**_observed_owner()[0], "actor": "frontend"},
        {**_observed_owner()[0], "kind": "process.update"},
    ]
    identity, _ = identify_owner(
        _published(account), account, observed, open_process=lambda _: _Process()
    )
    assert identity is not None


def test_a_stale_descriptor_whose_pid_another_roots_owner_took_is_refused(tmp_path):
    # This row's owner, pid 4321, started at 100.0 and has gone; the pid now
    # names another auth root's owner, started later.
    account = _row_account(tmp_path)
    foreign = _Process(created=250.0)
    identity, problem = identify_owner(
        _published(account), account, _observed_owner(), open_process=lambda _: foreign
    )
    assert identity is None and problem and "not an owner the watcher saw" in problem
    disposition = settle_owner(
        identity,
        PublishedOwner(4321, "instance-a"),
        None,
        auth_root=str(account.auth_root),
    )
    assert not disposition.gone and foreign.kills == 0


@pytest.mark.parametrize(
    "observed",
    [
        pytest.param([], id="no-observation"),
        pytest.param(_observed_owner(in_row=False), id="not-this-rows-actor"),
        pytest.param(_observed_owner(actor="frontend"), id="not-an-owner"),
        pytest.param(_observed_owner(pid=9999), id="another-pid"),
    ],
)
def test_an_owner_without_evidence_of_this_rows_start_is_refused(tmp_path, observed):
    account = _row_account(tmp_path)
    identity, problem = identify_owner(
        _published(account), account, observed, open_process=lambda _: _Process()
    )
    assert identity is None and problem


def test_a_descriptor_for_another_profile_is_refused(tmp_path):
    account = _row_account(tmp_path)
    identity, problem = identify_owner(
        _published(account, profile=tmp_path / "other" / "profile"),
        account,
        _observed_owner(),
        open_process=lambda _: _Process(),
    )
    assert identity is None and problem and "another profile" in problem


def test_an_owner_whose_create_time_cannot_be_read_is_refused(tmp_path):
    account = _row_account(tmp_path)
    identity, problem = identify_owner(
        _published(account),
        account,
        _observed_owner(),
        open_process=lambda _: _Process(created=psutil.AccessDenied(4321)),
    )
    assert identity is None and problem and "could not be read" in problem


# --- The post-quit gate ----------------------------------------------------------------


_SETTLED = DaemonCleanup("dir", True, False, True, True)


def test_settled_actors_admit_the_post_quit_session():
    assert (
        preservation_refusals(
            _SETTLED, owner_exit="exited", residual=[], swept=[], remaining=[]
        )
        == []
    )


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        ({"owner_exit": "still running"}, "owner's exit"),
        ({"owner_exit": "unknown (AccessDenied)"}, "owner's exit"),
        (
            {"cleanup": DaemonCleanup("dir", True, False, False, False, ("kept",))},
            "could not confirm",
        ),
        ({"cleanup": DaemonCleanup("dir", True, False, True, False)}, "did not finish"),
        ({"residual": [7]}, "outlived"),
        ({"swept": [8]}, "had to kill"),
        ({"remaining": [9]}, "still run"),
    ],
)
def test_unsettled_actors_refuse_the_post_quit_session(changes, reported):
    arguments: dict[str, Any] = {
        "cleanup": _SETTLED,
        "owner_exit": "exited",
        "residual": [],
        "swept": [],
        "remaining": [],
        **changes,
    }
    refusals = preservation_refusals(
        arguments["cleanup"],
        owner_exit=arguments["owner_exit"],
        residual=arguments["residual"],
        swept=arguments["swept"],
        remaining=arguments["remaining"],
    )
    assert any(reported in refusal for refusal in refusals), refusals


# --- K0, at its call site ------------------------------------------------------------


def _valid_vector(profile):
    vector, failures = judge_row(_healthy(profile))
    assert failures == []
    return vector


async def _k0(monkeypatch, result: RowResult, reference) -> None:
    async def row(*args, **kwargs):
        return result

    monkeypatch.setattr(rows, "_run", row)
    monkeypatch.setitem(rows._VECTORS, "K3", reference)
    await rows.test_the_daemon_row_repeats_identically(
        None, (None, None), None, monkeypatch
    )


async def test_k0_accepts_a_clean_matching_repeat(profile, monkeypatch):
    vector = _valid_vector(profile)
    await _k0(monkeypatch, RowResult("K0", "daemon", vector=vector), vector)


@pytest.mark.parametrize(
    "failure",
    [
        "the server did not exit within 90.0s of stdin EOF",
        "daemon mode published no owner descriptor",
        "cleanup had to kill browsers: [999]",
    ],
)
async def test_k0_fails_a_repeat_that_failed_its_own_row(profile, monkeypatch, failure):
    vector = _valid_vector(profile)
    result = RowResult("K0", "daemon", vector=vector, failures=[failure])
    with pytest.raises(AssertionError, match="own expectations"):
        await _k0(monkeypatch, result, vector)


async def test_k0_fails_a_repeat_in_another_mode(profile, monkeypatch):
    vector = _valid_vector(profile)
    other = dataclasses.replace(vector, mode="direct")
    with pytest.raises(AssertionError, match="mode"):
        await _k0(monkeypatch, RowResult("K0", "daemon", vector=other), vector)


def test_k0_without_a_valid_reference_fails(profile):
    vector = _valid_vector(profile)
    assert repeat_verdict(None, RowResult("K0", "daemon", vector=vector))


# --- The e1cb census model, judged in full -------------------------------------------


def _census(unreadable: bool) -> dict:
    """Two browser roots on the row's profile, the first one's identity denied
    when *unreadable*; the watcher's own summary of that table."""
    table: dict[int, dict[str, Any]] = {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]}
    }
    sampler, tracker = _sampler(table), Tracker()
    tracker.observe(sampler.sample(), 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": ["python", "pre-exec"]}
    tracker.observe(sampler.sample(), 1.0)
    chrome = [BROWSER_EXE, f"--user-data-dir={KEY}"]
    table[2].update(cmdline=chrome, exe=BROWSER_EXE)
    if unreadable:
        table[2]["start"] = psutil.AccessDenied(2)
    table[3] = {"start": 3.0, "ppid": 1, "exe": BROWSER_EXE, "cmdline": chrome}
    tracker.observe(sampler.sample(), 2.0)
    return {
        "stopped_by": "stop file",
        "observation_start": 10.0,
        "observation_end": 100.0,
        "max_gap_seconds": 0.2,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


@pytest.mark.parametrize("unreadable", [True, False])
def test_two_browser_roots_fail_the_row_whether_or_not_one_is_readable(
    profile, unreadable
):
    vector, failures = judge_row(
        dataclasses.replace(
            _healthy(profile),
            browser_key=canonical_user_data_dir(KEY),
            watcher=_census(unreadable),
        )
    )
    assert not vector.o1_single_browser
    assert failures


# --- First observations, judged against the Direct reference ---------------------


def _first_observation_census(case: str) -> dict:
    """The e1cc model: browser roots alive through five samples, then gone.

    ``pid 2`` is the row's browser, readable unless *case* says how its very
    first observation fails; ``pid 3`` is a second, readable root on the same
    profile unless *case* is a one-root or non-browser control.
    """
    table: dict[int, dict[str, Any]] = {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]}
    }
    sampler, tracker = _sampler(table), Tracker()
    tracker.observe(sampler.sample(), 0.0)
    chrome = [BROWSER_EXE, f"--user-data-dir={KEY}"]
    table[2] = {"start": 2.0, "ppid": 1, "exe": BROWSER_EXE, "cmdline": chrome}
    if case not in ("one-root", "other-user", "ps-exe"):
        table[3] = {"start": 3.0, "ppid": 1, "exe": BROWSER_EXE, "cmdline": chrome}
    if case == "open-denied":
        table[3]["open"] = psutil.AccessDenied(3)
    elif case == "create-time-denied":
        table[3]["start"] = psutil.AccessDenied(3)
    elif case == "browser-exe-ancestry-and-arguments-denied":
        table[3].update(ppid=psutil.AccessDenied(3), cmdline=psutil.AccessDenied(3))
    elif case == "open-oserror":
        table[3]["open"] = OSError("synthetic read failure")
    elif case == "other-user":
        table[3] = {
            "start": 3.0,
            "ppid": 50,
            "exe": BROWSER_EXE,
            "cmdline": psutil.AccessDenied(3),
            "user": "root",
        }
    elif case == "ps-exe":
        table[3] = {
            "start": 3.0,
            "ppid": 1,
            "exe": "/bin/ps",
            "cmdline": psutil.AccessDenied(3),
        }
    for tick in range(1, 6):
        tracker.observe(sampler.sample(), float(tick))
    table.pop(2)
    table.pop(3, None)
    tracker.observe(sampler.sample(), 6.0)
    return {
        "stopped_by": "stop file",
        "observation_start": 10.0,
        "observation_end": 100.0,
        "max_gap_seconds": 0.2,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


def _judged(profile, case: str, *, daemon: bool):
    return judge_row(
        dataclasses.replace(
            _healthy(profile, daemon=daemon),
            browser_key=canonical_user_data_dir(KEY),
            watcher=_first_observation_census(case),
        )
    )


@pytest.mark.parametrize(
    "case",
    [
        "open-denied",
        "create-time-denied",
        "browser-exe-ancestry-and-arguments-denied",
        "open-oserror",
        "two-readable-roots",
    ],
)
def test_a_second_root_that_cannot_be_excluded_fails_o1_and_the_comparison(
    profile, case
):
    direct, direct_failures = _judged(profile, "one-root", daemon=False)
    assert direct_failures == [] and direct.o1_single_browser
    vector, failures = _judged(profile, case, daemon=True)
    assert not vector.o1_single_browser
    assert failures
    assert compare_to_direct(direct, vector)


@pytest.mark.parametrize("case", ["one-root", "other-user", "ps-exe"])
def test_what_can_be_excluded_leaves_a_single_root(profile, case):
    direct, _ = _judged(profile, "one-root", daemon=False)
    vector, failures = _judged(profile, case, daemon=True)
    assert failures == []
    assert vector.o1_single_browser
    assert compare_to_direct(direct, vector) == []


# --- Exclusions bound to one lifetime, judged against the Direct reference ------


def _lifetime_census(case: str) -> dict:
    """The e1ce models: a readable root (pid 3) and, unless *case* is the
    one-root control, a second root at pid 2 whose exclusion rests on
    evidence about another lifetime, or on an ancestry that was never
    complete. pid 1 is init and pid 10 the harness.
    """
    chrome = [BROWSER_EXE, f"--user-data-dir={KEY}"]

    def browser(start: float, ppid: int = 10) -> dict[str, Any]:
        return {"start": start, "ppid": ppid, "exe": BROWSER_EXE, "cmdline": chrome}

    table: dict[int, dict[str, Any]] = {
        1: {"start": 0.0, "ppid": 0, "cmdline": ["init"]},
        10: {"start": 5.0, "ppid": 1, "cmdline": ["pytest"]},
    }
    if case.startswith("cached-unrelated"):
        table[2] = {"start": 0.2, "ppid": 0, "cmdline": ["daemon"], "user": "root"}
    if case == "incomplete-baseline-ancestry":
        # A staging leftover whose parent vanished before the first sample.
        table[9] = {"start": 5.1, "ppid": 10, "open": psutil.NoSuchProcess(9)}
        table[2] = {"start": 5.3, "ppid": 9, "cmdline": ["python", "pre-exec"]}
    if case.startswith("calendar-reads-older"):
        # The e1dc and e1dd models: a staging leftover born after the harness
        # whose wall-clock create time reads older, as after a backward clock
        # step anywhere before the first sample, with its parent already gone
        # or itself already adopted by init.
        ppid = 1 if case.endswith("adopted-by-init") else 9
        table[2] = {"start": 4.9, "ppid": ppid, "cmdline": ["python", "pre-exec"]}
    sampler, tracker = _sampler(table, root=10), Tracker()
    tracker.observe(sampler.sample(), 0.0)
    table[3] = browser(6.0)
    if case == "two-readable-roots":
        table[2] = browser(6.0)
    elif case in ("unknown-stays-unknown", "unknown-pid-reused-by-another-user"):
        table[2] = {**browser(6.0), "open": psutil.AccessDenied(2)}
    elif case == "cached-unrelated-pid-reused-open-denied":
        table[2] = {**browser(6.0), "open": psutil.AccessDenied(2)}
    elif case == "cached-unrelated-pid-reused-create-time-denied":
        table[2] = {**browser(6.0), "start": psutil.AccessDenied(2)}
    elif case == "incomplete-baseline-ancestry":
        table.pop(9)
        table[2] = browser(5.3, ppid=1)
    elif case.startswith("calendar-reads-older"):
        # Adopted by init and exec'd into the row's browser: same pid, same
        # create time, new command line.
        table[2] = browser(4.9, ppid=1)
    for tick in (1.0, 2.0, 3.0):
        tracker.observe(sampler.sample(), tick)
    for pid in (2, 3):
        table.pop(pid, None)
    tracker.observe(sampler.sample(), 4.0)
    if case == "unknown-pid-reused-by-another-user":
        table[2] = {"start": 15.0, "ppid": 0, "cmdline": ["daemon"], "user": "root"}
        tracker.observe(sampler.sample(), 5.0)
        table.pop(2)
        tracker.observe(sampler.sample(), 6.0)
    return {
        "stopped_by": "stop file",
        "observation_start": 10.0,
        "observation_end": 100.0,
        "max_gap_seconds": 0.2,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


def _judged_lifetime(profile, case: str, *, daemon: bool):
    return judge_row(
        dataclasses.replace(
            _healthy(profile, daemon=daemon),
            browser_key=canonical_user_data_dir(KEY),
            watcher=_lifetime_census(case),
        )
    )


@pytest.mark.parametrize(
    "case",
    [
        "two-readable-roots",
        "unknown-stays-unknown",
        "unknown-pid-reused-by-another-user",
        "cached-unrelated-pid-reused-open-denied",
        "cached-unrelated-pid-reused-create-time-denied",
        "incomplete-baseline-ancestry",
        "calendar-reads-older-parent-gone",
        "calendar-reads-older-adopted-by-init",
    ],
)
def test_an_exclusion_about_another_lifetime_fails_o1_and_the_comparison(profile, case):
    direct, direct_failures = _judged_lifetime(profile, "one-root", daemon=False)
    assert direct_failures == [] and direct.o1_single_browser
    vector, failures = _judged_lifetime(profile, case, daemon=True)
    assert not vector.o1_single_browser
    assert failures
    assert compare_to_direct(direct, vector)


# --- A known root's failed read, judged in full ----------------------------------

OTHER = "/tmp/differential-other-profile"


def _chrome_on(profile: object) -> list[str]:
    return [BROWSER_EXE, f"--user-data-dir={profile}"]


def _failed_read_census(
    case: str, *, row: str = KEY, alias: tuple[Path, Path] | None = None
) -> dict:
    """The e1eh model: pid 70 is a browser root and pid 71 its renderer, both
    readable for two samples; pid 72 is a driver. At the third sample one
    lifetime's arguments are refused, as *case* says, and at the fourth
    everything is gone. pid 1 is init and pid 10 the harness.

    pid 70 runs on the row's profile *row*, on ``OTHER`` for the
    ``other-profile`` cases, or through the link of *alias*, which points at
    *row* until the refused read and at its second path from then on. pid 60,
    where a case has it, is a second root on *row*, always readable.
    """
    table: dict[int, dict[str, Any]] = {
        1: {"start": 0.0, "ppid": 0, "cmdline": ["init"]},
        10: {"start": 5.0, "ppid": 1, "cmdline": ["pytest"]},
    }
    sampler, tracker = _sampler(table, root=10), Tracker()
    tracker.observe(sampler.sample(), 0.0)
    if case.startswith("other-profile"):
        first: object = OTHER
        table[60] = {"start": 5.5, "ppid": 10, "exe": BROWSER_EXE}
        table[60]["cmdline"] = _chrome_on(row)
    else:
        first = alias[0] if alias is not None else row
    table[70] = {"start": 6.0, "ppid": 10, "exe": BROWSER_EXE}
    table[70]["cmdline"] = _chrome_on(first)
    table[71] = {
        "start": 6.1,
        "ppid": 70,
        "exe": BROWSER_EXE,
        "cmdline": [BROWSER_EXE, "--type=renderer", f"--user-data-dir={row}"],
    }
    table[72] = {
        "start": 6.2,
        "ppid": 10,
        "exe": "/usr/bin/node",
        "cmdline": ["node", "driver"],
    }
    for tick in (1.0, 2.0):
        tracker.observe(sampler.sample(), tick)
    if case in ("known-root", "peer", "other-profile-hidden", "alias-retarget"):
        # The packet: read with its profile, one refused read, then gone.
        table[70]["cmdline"] = psutil.AccessDenied(70)
    if case in ("peer", "alias-retarget"):
        table[60] = {"start": 7.0, "ppid": 10, "exe": BROWSER_EXE}
        table[60]["cmdline"] = _chrome_on(row)
    if case == "alias-retarget":
        assert alias is not None
        link, elsewhere = alias
        link.unlink()
        link.symlink_to(elsewhere, target_is_directory=True)
    elif case == "other-profile-readable":
        table[70]["cmdline"] = _chrome_on(row)
    elif case == "known-root-exe-changed":
        table[70].update(
            exe=f"{BROWSER_EXE}_crashpad_handler", cmdline=psutil.AccessDenied(70)
        )
    elif case == "known-root-parent-unread":
        table[70].update(ppid=psutil.AccessDenied(70), cmdline=psutil.AccessDenied(70))
    elif case == "driver-now-the-browser":
        table[72].update(exe=BROWSER_EXE, cmdline=psutil.AccessDenied(72))
    elif case == "never-read":
        table[73] = {
            "start": 7.0,
            "ppid": 10,
            "exe": BROWSER_EXE,
            "cmdline": psutil.AccessDenied(73),
        }
    elif case == "renderer":
        table[71]["cmdline"] = psutil.AccessDenied(71)
    tracker.observe(sampler.sample(), 3.0)
    for pid in (60, 70, 71, 72, 73):
        table.pop(pid, None)
    tracker.observe(sampler.sample(), 4.0)
    return {
        "stopped_by": "stop file",
        "observation_start": 10.0,
        "observation_end": 100.0,
        "max_gap_seconds": 0.2,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


def _judged_failed_read(
    profile, case: str, *, row: str = KEY, alias: tuple[Path, Path] | None = None
):
    census = _failed_read_census(case, row=row, alias=alias)
    vector, failures = judge_row(
        dataclasses.replace(
            _healthy(profile),
            browser_key=canonical_user_data_dir(row),
            watcher=census,
        )
    )
    return census, vector, failures


def test_a_known_roots_refused_arguments_leave_it_one_root(profile):
    census, vector, failures = _judged_failed_read(profile, "known-root")
    assert failures == []
    assert vector.o1_single_browser and vector.watcher_healthy
    # The failed read is kept, marked as the earlier reading retained.
    (note,) = census["read_failures"]
    assert note["pid"] == 70 and note["failures"] == ["cmdline: AccessDenied"]
    assert note["resolution"] == "a known browser root's earlier reading retained"
    assert note["retained_profile"] == canonical_user_data_dir(KEY)
    assert not note["possible_browser"]


def test_a_retained_root_still_counts_beside_a_new_peer(profile):
    census, vector, failures = _judged_failed_read(profile, "peer")
    assert census["relevant_read_failures"] == []
    assert census["max_roots"][canonical_user_data_dir(KEY)] == 2
    assert not vector.o1_single_browser
    assert failures


def test_a_retained_root_on_another_profile_stays_unidentified_for_the_row(profile):
    # Unchanged image, parent and lifetime; the arguments that would say
    # whether it re-executed onto the row's profile are hidden.
    census, vector, failures = _judged_failed_read(profile, "other-profile-hidden")
    (note,) = census["read_failures"]
    assert note["retained_profile"] == canonical_user_data_dir(OTHER)
    assert census["max_roots"][canonical_user_data_dir(KEY)] == 1
    assert not vector.o1_single_browser and not vector.watcher_healthy
    assert any("possible browser" in failure for failure in failures)


def test_the_same_move_read_in_full_counts_two_roots(profile):
    census, vector, failures = _judged_failed_read(profile, "other-profile-readable")
    assert census["read_failures"] == []
    assert census["max_roots"][canonical_user_data_dir(KEY)] == 2
    assert not vector.o1_single_browser
    assert failures


def test_a_retained_root_keeps_the_profile_it_was_read_on(profile, tmp_path):
    # Its arguments name a link, retargeted while they cannot be read: the
    # old arguments now resolve elsewhere, the reading does not.
    row, elsewhere, link = tmp_path / "row", tmp_path / "elsewhere", tmp_path / "link"
    row.mkdir()
    elsewhere.mkdir()
    link.symlink_to(row, target_is_directory=True)
    census, vector, failures = _judged_failed_read(
        profile, "alias-retarget", row=str(row), alias=(link, elsewhere)
    )
    (note,) = census["read_failures"]
    assert note["retained_profile"] == canonical_user_data_dir(str(row))
    assert census["max_roots"][canonical_user_data_dir(str(row))] == 2
    assert canonical_user_data_dir(str(elsewhere)) not in census["max_roots"]
    assert not vector.o1_single_browser
    assert failures


@pytest.mark.parametrize(
    ("case", "pid"),
    [
        # Read before as a driver: an exec since could have made it a root.
        ("driver-now-the-browser", 72),
        ("never-read", 73),
        # Chromium starts its helpers from its own executable, so an
        # unchanged image says nothing about whether a helper became a root.
        ("renderer", 71),
        # Neither is the reading it was counted by.
        ("known-root-exe-changed", 70),
        ("known-root-parent-unread", 70),
    ],
)
def test_any_other_refused_arguments_leave_o1_unestablished(profile, case, pid):
    census, vector, failures = _judged_failed_read(profile, case)
    assert [e["pid"] for e in census["relevant_read_failures"]] == [pid]
    assert not vector.o1_single_browser and not vector.watcher_healthy
    assert failures


def _moving_root_census(readings: str, *, peer_from: int | None) -> dict:
    """The e1ej model: pid 70, one lifetime with one executable throughout,
    sampled once per letter of *readings*: ``A`` or ``B`` its arguments read
    naming that profile, ``-`` refused. From sample *peer_from* on, pid 60 is
    a second, readable root on A. Then both are gone.
    """
    table: dict[int, dict[str, Any]] = {
        1: {"start": 0.0, "ppid": 0, "cmdline": ["init"]},
        10: {"start": 5.0, "ppid": 1, "cmdline": ["pytest"]},
    }
    sampler, tracker = _sampler(table, root=10), Tracker()
    tracker.observe(sampler.sample(), 0.0)
    table[70] = {"start": 6.0, "ppid": 10, "exe": BROWSER_EXE}
    for tick, reading in enumerate(readings, start=1):
        profile = {"A": KEY, "B": OTHER}.get(reading)
        table[70]["cmdline"] = (
            _chrome_on(profile) if profile else psutil.AccessDenied(70)
        )
        if tick == peer_from:
            table[60] = {"start": 7.0, "ppid": 10, "exe": BROWSER_EXE}
            table[60]["cmdline"] = _chrome_on(KEY)
        tracker.observe(sampler.sample(), float(tick))
    table.pop(60, None)
    table.pop(70)
    tracker.observe(sampler.sample(), float(len(readings) + 1))
    return {
        "stopped_by": "stop file",
        "observation_start": 10.0,
        "observation_end": 100.0,
        "max_gap_seconds": 0.2,
        "max_roots": dict(tracker.max_roots),
        "read_failures": sampler.read_failures,
        "relevant_read_failures": sampler.relevant_read_failures,
    }


@pytest.mark.parametrize(
    ("readings", "peer_from", "established"),
    [
        # Retained on A, read on B, retained on B beside a peer on A: the
        # second refused read could name A again.
        pytest.param("A-B-", 4, {"A": False, "B": False}, id="A-then-B-hidden"),
        # Retained on B, read on A, retained on A: the first refused read
        # could have named A, the second B.
        pytest.param("B-A-", None, {"A": False, "B": False}, id="B-then-A-hidden"),
        pytest.param("A-", None, {"A": True, "B": False}, id="retained-on-A-only"),
        # Nothing of B was hidden: the peer on A overlaps a readable B.
        pytest.param("A-BB", 3, {"A": True, "B": False}, id="A-then-readable-B"),
    ],
)
def test_every_profile_a_root_was_retained_on_is_seen_by_each_judge(
    profile, readings, peer_from, established
):
    census = _moving_root_census(readings, peer_from=peer_from)
    for judged, expected in established.items():
        vector, failures = judge_row(
            dataclasses.replace(
                _healthy(profile),
                browser_key=canonical_user_data_dir({"A": KEY, "B": OTHER}[judged]),
                watcher=census,
            )
        )
        assert vector.o1_single_browser is expected, judged
        assert (failures == []) is expected, (judged, failures)
