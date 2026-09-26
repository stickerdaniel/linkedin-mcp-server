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
    judge_row,
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
            "could not read",
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

    def __init__(self, *, running=True, stops_on_kill=True):
        self.running = running
        self.stops_on_kill = stops_on_kill
        self.kills = 0

    def is_running(self):
        return self.running

    def kill(self):
        self.kills += 1
        if self.stops_on_kill:
            self.running = False

    def wait(self, timeout=None):
        if self.running:
            raise psutil.TimeoutExpired(timeout)


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


def test_cleanup_keeps_the_directory_of_an_unidentified_owner(row_state):
    directory, published, account = row_state
    published["descriptor"] = SimpleNamespace(pid=4321, instance_id="instance-a")
    cleanup = retire_daemon_state(account, None)
    assert directory.exists() and not cleanup.cleaned


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
