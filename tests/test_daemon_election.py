"""Getting one owner started, and never two.

The properties here are about process lifetime, file descriptor inheritance and
the kernel's arbitration, so most of these spawn real processes. A test that
stayed inside one interpreter could show the code takes the branches it means to
and would not show the thing that matters: that exactly one browser-owning
process exists, and that the lock dies with it.

Three of these pin defects that were live in this code and found by running it:
a failed child read as "somebody is starting" and cost a full deadline, a
crashed owner's descriptor was handed back as attachable, and the parent's copy
of the lock outliving the owner would have wedged every recovery.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import linkedin_mcp_server.daemon as daemon_module
import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon_election as election_module
from linkedin_mcp_server import __version__
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import Attachment, OwnerState
from linkedin_mcp_server.daemon_election import (
    _Attempt,
    ElectionOutcome,
    Reach,
    obtain_owner,
)
from linkedin_mcp_server.daemon_descriptor import (
    build,
    new_instance_id,
    new_token,
    publish,
)
from linkedin_mcp_server.daemon_lock import DaemonLock, daemon_is_running

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = "test-runtime"

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the lock is handed to the child only on POSIX"
)


@pytest.fixture(autouse=True)
def _isolate_daemon_state(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Keep in-process tests off the account's real daemon state.

    Skipped for the tests that spawn real owners. Those cannot use a redirected
    root, because the owner is started by production code and derives its own
    state from the account rather than from anything a test can hand it. They
    take the ``real_state_root`` fixture instead, which isolates by auth root
    and cleans up after itself.
    """
    if "real_state_root" in request.fixturenames:
        return
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)
    monkeypatch.setattr(daemon_module, "get_runtime_id", lambda: _RUNTIME)
    monkeypatch.setenv("HOME", str(home))


def _config(profile: Path) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    config.server.daemon_enabled = True
    return config


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _publish_stale_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    package_version: str = __version__,
) -> str:
    """Publish a descriptor for an owner that is not running.

    Exactly what a crash after publishing leaves behind: every field is valid,
    the token matches, and nothing is listening.
    """
    token = new_token()
    descriptor = build(
        instance_id=new_instance_id(),
        package_version=package_version,
        runtime_id=_RUNTIME,
        profile=profile,
        host="127.0.0.1",
        port=49152,
        path="/mcp",
        token=token,
        config=config,
        log_path=auth_root / "daemon.log",
    )
    publish(auth_root, descriptor, token)
    return descriptor.instance_id


def _attachment_for(profile: Path, config: AppConfig, port: int) -> Attachment:
    """An attachment pointing at *port*, for probing a socket directly.

    Not published: this is for handing to the probe by itself, without an
    election around it.
    """
    token = new_token()
    descriptor = build(
        instance_id=new_instance_id(),
        package_version=__version__,
        runtime_id=_RUNTIME,
        profile=profile,
        host="127.0.0.1",
        port=port,
        path="/mcp",
        token=token,
        config=config,
        log_path=profile.parent / "daemon.log",
    )
    return Attachment(descriptor=descriptor, token=token)


class TestLiveness:
    """A descriptor is a file. Only a request that is answered is a process."""

    def test_a_crashed_owners_descriptor_is_not_handed_back(self, tmp_path: Path):
        # Reproduced against a real owner before this was fixed: the process was
        # killed, and the next election read its leftovers as attachable and
        # returned the dead endpoint. Every check passes on that file, because
        # the owner wrote it while it was healthy.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.REFUSED,
        )

        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.state is OwnerState.INCOMPATIBLE

    def test_an_unreachable_descriptor_is_kept_rather_than_deleted(
        self, tmp_path: Path
    ):
        # Downgrading is not the same as cleaning up. A live owner this client
        # cannot reach for a moment would be stranded by a deletion, and so
        # would every other client attached to it. The lock decides what happens
        # next, not this reading.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)
        descriptor_file = daemon_descriptor_module.descriptor_path(auth_root)

        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.REFUSED,
        )

        assert descriptor_file.exists()

    def test_a_reachable_owner_is_attached_to_without_starting_anything(
        self, tmp_path: Path
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.ANSWERED,
        )

        assert outcome.worth_connecting
        assert not outcome.started_owner

    def test_a_corpse_is_not_probed_over_and_over(self, tmp_path: Path):
        # The descriptor stays on disk until a live owner overwrites it, so a
        # loop that re-read it would probe the same dead endpoint every pass.
        # Each probe is a network timeout, which is how a fail-fast path turns
        # into a stall.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        probes: list[Attachment] = []

        def never_answers(attachment: Attachment) -> Reach:
            probes.append(attachment)
            # REFUSED, which is what a corpse really produces: its port is
            # closed, so the kernel turns the connection away rather than
            # leaving the probe to time out. A SILENT stub here would be
            # testing a different animal, and the count below would rightly
            # fail.
            return Reach.REFUSED

        obtain_owner(
            auth_root, profile, config, deadline_seconds=1.0, connect=never_answers
        )

        # Exactly once for the corpse. Stated as a count rather than as
        # "no instance appears twice", which is the weaker claim it started as:
        # that version also passed when the probe was never called at all, so an
        # implementation that skipped reachability entirely would have looked
        # correct here.
        assert len(probes) == 1, [p.descriptor.instance_id for p in probes]


class TestSilenceIsNotDeath:
    """A probe that ran out of time proves nothing, and used to prove death.

    A frontend started an owner, watched it report ready, and then refused to
    attach to it: one ping landed in a scheduler stall, the instance was written
    off, and every later read in that election short-circuited on the record of
    that single observation. The owner kept running and kept the lock, so the
    frontend spent the rest of its budget contending with the healthy process it
    had started itself.
    """

    def test_a_dead_endpoint_and_a_frozen_one_are_told_apart(self, tmp_path: Path):
        """The distinction the whole fix rests on, against the real probe.

        Not against a stub. Everything else here keys off ``REFUSED`` versus
        ``SILENT``, so a test that manufactured the two would pass just as
        happily if the production probe collapsed them again.

        The two sockets differ in exactly one way, and it is the way that
        matters: a corpse's port is closed, so the kernel refuses the connection
        outright, while a paused process still holds its listening socket and
        the kernel completes the handshake into a backlog nobody reads. That is
        what makes a listening-but-unaccepting socket a faithful stand-in for a
        stopped process, without the cost of stopping one.
        """
        profile = _profile(tmp_path)
        config = _config(profile)

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead_port = closed.getsockname()[1]
        closed.close()

        began = time.monotonic()
        refused = election_module._reachable(
            _attachment_for(profile, config, dead_port)
        )
        refusal_took = time.monotonic() - began

        frozen = socket.socket()
        frozen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        frozen.bind(("127.0.0.1", 0))
        # Listening, and deliberately never accepted from.
        frozen.listen(64)
        try:
            began = time.monotonic()
            silent = election_module._reachable(
                _attachment_for(profile, config, frozen.getsockname()[1])
            )
            silence_took = time.monotonic() - began
        finally:
            frozen.close()

        assert refused is Reach.REFUSED, f"a closed port read as {refused}"
        assert silent is Reach.SILENT, f"an unaccepting socket read as {silent}"

        # And the refusal is genuinely cheap rather than merely different.
        # Measured at about ten milliseconds; asserted well above that so a
        # loaded runner cannot fail it, and well below the probe's own budget so
        # a refusal that started timing out would.
        assert refusal_took < election_module._REACHABLE_SECONDS / 2, (
            f"the refusal took {refusal_took:.2f}s"
        )
        assert silence_took >= election_module._REACHABLE_SECONDS - 0.5, (
            f"the silence took only {silence_took:.2f}s"
        )

    def test_an_owner_that_is_slow_twice_is_still_attached_to(self, tmp_path: Path):
        """Two silences, not one, and the count is the point.

        A single retry would satisfy a one-shot version of this test while still
        failing every stall longer than about ten seconds, because each probe
        costs the full budget. Measured against the real loop: one grace rescues
        a six second freeze and gives up on a fifteen second one, which is the
        bug as reported.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        answers = [Reach.SILENT, Reach.SILENT, Reach.ANSWERED]
        probes: list[Attachment] = []

        def slow_to_answer(attachment: Attachment) -> Reach:
            probes.append(attachment)
            return answers[min(len(probes) - 1, len(answers) - 1)]

        # The stalled owner holds the daemon lock, which is what makes the loop
        # keep going rather than start a replacement. That is not a convenience
        # of the test: a process too busy to answer a ping is still a process,
        # and it is holding the lock precisely because it is alive. Without this
        # the frontend finds the position free, tries to start a child, and the
        # spawn's own failure ends the election before any retry happens.
        held = DaemonLock(auth_root)
        assert held.try_acquire(), "could not simulate the stalled owner's lock"
        try:
            outcome = obtain_owner(
                auth_root,
                profile,
                config,
                deadline_seconds=20.0,
                connect=slow_to_answer,
            )
        finally:
            held.release()

        assert outcome.worth_connecting, outcome.attachment_lookup.reason
        assert len(probes) >= 3, "the owner was written off before it answered"

    def test_a_silence_is_reported_as_its_own_state(self, tmp_path: Path):
        """Which refusal it was, not merely that there was one.

        The original failure was diagnosed from this string, and reusing the
        burial's wording would have made a stalled owner indistinguishable from
        a corpse in exactly the report a user pastes into an issue.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        silent = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.SILENT,
        )
        refused = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.REFUSED,
        )

        assert silent.attachment_lookup.reason != refused.attachment_lookup.reason
        assert not silent.worth_connecting

    def test_a_refusal_still_buries_on_the_first_probe(self, tmp_path: Path):
        """The corpse path keeps its fail-fast, and the grace does not leak into it.

        Written as "would have answered next time" rather than "never answers":
        a stub that refuses forever passes even if refusals were retried, since
        the outcome is the same either way. This one fails if the retry is
        granted, because the second probe would succeed and the election would
        attach.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        probes: list[Attachment] = []

        def refuses_then_would_answer(attachment: Attachment) -> Reach:
            probes.append(attachment)
            return Reach.REFUSED if len(probes) == 1 else Reach.ANSWERED

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            connect=refuses_then_would_answer,
        )

        assert len(probes) == 1, "a refused instance was probed again"
        assert not outcome.worth_connecting

    def test_an_owner_that_never_answers_does_not_outlast_the_deadline(
        self, tmp_path: Path
    ):
        """Nothing is buried now, so the deadline is the only thing bounding this.

        The worst case for the change: an owner frozen for good that also still
        holds the daemon lock, so no replacement can be elected either. It has to
        end on time rather than probe forever.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        held = DaemonLock(auth_root)
        assert held.try_acquire(), "could not simulate the frozen owner's lock"
        try:
            began = time.monotonic()
            outcome = obtain_owner(
                auth_root,
                profile,
                config,
                deadline_seconds=1.0,
                connect=lambda attachment: Reach.SILENT,
            )
            elapsed = time.monotonic() - began
        finally:
            held.release()

        assert not outcome.worth_connecting
        # Generous, because the budget is spent inside probes and lock attempts
        # that are not interrupted mid-flight. What it catches is an unbounded
        # loop, which is the only real risk of never burying anything.
        assert elapsed < 15, f"the election ran {elapsed:.1f}s past a 1.0s budget"


class TestFailingFast:
    def test_a_child_that_cannot_serve_does_not_cost_the_whole_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Measured before this was fixed: a failed child was indistinguishable
        # from "somebody else is starting", so the caller waited out its full
        # budget for a descriptor nothing was going to write. Sixty seconds of
        # a client apparently hanging, ending in no diagnosis at all.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        monkeypatch.setattr(
            election_module,
            "_start_owner",
            lambda *args, **kwargs: _Attempt.FAILED,
        )

        started = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=30,
            connect=lambda attachment: Reach.REFUSED,
        )
        elapsed = time.monotonic() - started

        assert not outcome.worth_connecting
        # Generously bounded: the point is that it returns rather than waiting
        # out the budget, not that it returns in any particular millisecond.
        assert elapsed < 5, elapsed

    def test_a_frontend_that_lost_the_race_takes_over_when_the_winner_dies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The recovery this loop exists for, and the one a plain "wait for a
        # descriptor" cannot provide. A wins the lock, its child adopts it and
        # then dies without ever publishing; B lost the race and is waiting.
        # Nothing will ever appear on disk, so only another lock attempt can
        # discover that the position came free.
        #
        # A loser that waits out its budget on the descriptor alone ends the
        # election with no owner at all, having had the lock available to it the
        # whole time.
        import threading

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        # The winner, holding the lock and releasing it shortly after.
        winner = DaemonLock(auth_root)
        assert winner.try_acquire()
        releasing = threading.Timer(1.0, winner.release)
        releasing.start()

        took_over: list[float] = []

        def contend(
            auth_root: Path, profile: Path, config: AppConfig, *, timeout: float
        ) -> _Attempt:
            contender = DaemonLock(auth_root)
            if not contender.try_acquire():
                return _Attempt.CONTENDED
            took_over.append(time.monotonic())
            _publish_stale_owner(auth_root, profile, config)
            contender.release()
            return _Attempt.STARTED

        monkeypatch.setattr(election_module, "_start_owner", contend)

        began = time.monotonic()
        try:
            outcome = obtain_owner(
                auth_root,
                profile,
                config,
                deadline_seconds=20,
                connect=lambda attachment: Reach.ANSWERED,
            )
        finally:
            releasing.cancel()
            winner.release()

        assert took_over, "the loser never retried the lock the winner freed"
        assert outcome.worth_connecting
        # And promptly: the point is recovery, not that it eventually happens.
        assert time.monotonic() - began < 10

    def test_a_child_that_never_reads_its_configuration_does_not_block_the_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The handshake timeout is reached only if the spawn gets that far. The
        # configuration is written to the child's pipe first, and a pipe buffer
        # is small — 64 KiB on Linux — while the configuration has no size limit
        # at all: user_agent, proxy_bypass and the paths are free-form strings.
        # A child that neither reads nor exits blocks that write indefinitely,
        # before any budget applies, with both processes holding the lock.
        #
        # Reproduced with a 10 MiB user agent and a child that only sleeps: the
        # outer process timeout fired and the wait was never entered.
        profile = _profile(tmp_path)
        config = _config(profile)
        config.browser.user_agent = "x" * (10 * 1024 * 1024)
        auth_root = profile.parent

        sleepers: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def deaf(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            # Ignores SIGTERM as well as never reading stdin. Both halves
            # matter: the second is the defect, and the first is what makes the
            # timing assertion below meaningful. A terminate-then-kill stop
            # would spend its whole grace period here, *after* the caller's
            # budget was already gone.
            child = real(
                [
                    command[0],
                    "-c",
                    "import signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "time.sleep(600)\n",
                ],
                **kwargs,
            )
            sleepers.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", deaf)

        began = time.monotonic()
        attempt = election_module._start_owner(auth_root, profile, config, timeout=0.5)
        elapsed = time.monotonic() - began

        try:
            # Close to the budget rather than merely finite. Stopping the child
            # happens after that budget is spent, so a grace period there is
            # time added to a deadline the caller was promised: terminate-first
            # turned this half-second election into five and a half against a
            # child that ignores SIGTERM. Measured at 0.6s as it stands.
            assert elapsed < 3, f"the spawn took {elapsed:.1f}s of a 0.5s budget"
            # Reported as a failure, not as "somebody is starting". A child that
            # never took its configuration is not coming up, and on POSIX it
            # already holds the inherited lock descriptor — so it is stopped
            # rather than left alone.
            assert attempt is _Attempt.FAILED

            # And the lock it inherited is free. This is the half that matters:
            # measured with the child merely abandoned, the lock stayed held
            # forever by a process that could never serve, and every later
            # election contended against it.
            probe = DaemonLock(auth_root)
            assert probe.try_acquire(), (
                "the abandoned child kept the daemon lock for this profile"
            )
            probe.release()

            assert all(sleeper.poll() is not None for sleeper in sleepers), (
                "the child that could not serve was left running"
            )

            # Nothing of the abandoned attempt is left in this process either.
            # These three are asserted together because the argument that the
            # cleanup is safe on Windows rests on all of them: the child is
            # confirmed dead *before* the handshake pipe is released, so the
            # blocked reader gets an end of file rather than needing a
            # cross-thread close, and the writer holds only the stdin stream —
            # never a lock descriptor — so a broken pipe is enough to end it.
            #
            # That argument is reasoned rather than measured on Windows, which
            # is why it is asserted here: this test runs on the Windows CI job,
            # where the reasoning would otherwise stand unchecked.
            #
            # What it does not do is prove the *ordering*. Moving the release
            # ahead of the stop leaves this green on POSIX, because `detach()`
            # never blocks here whatever the child is doing. On Windows the same
            # change is the difference between an end of file and a wait on a
            # live reader, and CI is the only place that distinction is
            # observed at all.
            assert all(
                sleeper.stdin is None or sleeper.stdin.closed for sleeper in sleepers
            ), "the configuration pipe was left open"
            assert not any(
                "daemon-config" in thread.name for thread in threading.enumerate()
            ), "the writer thread outlived the child it was writing to"
        finally:
            for sleeper in sleepers:
                if sleeper.poll() is None:  # pragma: no cover - the stop worked
                    sleeper.kill()
                    sleeper.wait(timeout=30)

    def test_a_child_that_says_nothing_does_not_keep_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A child that has neither answered nor exited by the end of the budget
        # is stopped, and the reasoning took two passes to get right.
        #
        # The first version called this "still trying" and left the child alone,
        # on the grounds that it might yet come up and that killing it could
        # leave the caller driving a second browser. The first half is true and
        # the second is not: an owner opens no browser before it answers, and on
        # POSIX the frontend holds its own lock until the spawn returns. What
        # the leniency actually bought was a child holding the inherited lock
        # while never serving — measured, the lock was still held afterwards,
        # and every later election would contend against it forever.
        #
        # This test asserted the lenient behaviour and then killed the child in
        # its own teardown, with a comment saying the lock would otherwise be
        # held for the rest of the run. That comment was the bug report.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        # An ordinary configuration, so the handover succeeds and the silence
        # happens at the handshake. The large-configuration case reaches the
        # same wedge by blocking the write instead, and is covered separately.
        real = subprocess.Popen
        started: list[subprocess.Popen[Any]] = []

        def mute(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            child = real([command[0], "-c", "import time; time.sleep(600)"], **kwargs)
            started.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", mute)

        began = time.monotonic()
        attempt = election_module._start_owner(auth_root, profile, config, timeout=0.5)
        elapsed = time.monotonic() - began

        try:
            assert attempt is _Attempt.FAILED

            # The half that matters. Nothing in production kills this child for
            # us, which is exactly what the old teardown was standing in for.
            probe = DaemonLock(auth_root)
            assert probe.try_acquire(), (
                "the silent child kept the daemon lock for this profile"
            )
            probe.release()
            assert all(child.poll() is not None for child in started)
            # Same three post-conditions as the blocked-write path next door,
            # for the same Windows reasoning: the child is confirmed dead before
            # the handshake pipe is released, so nothing has to interrupt a live
            # reader, and the writer holds no lock descriptor.
            assert all(child.stdin is None or child.stdin.closed for child in started)
            assert not any(
                "daemon-config" in thread.name for thread in threading.enumerate()
            )

            # And on time. The timeout bounds the *wait*, and the cleanup after
            # it used to hand that bound straight back: `child.stdout.close()`
            # waits on the reader thread's I/O lock, so it blocked for as long
            # as the child stayed silent — 29.27s against a 30s sleeper.
            assert elapsed < 5, f"the bounded wait took {elapsed:.1f}s"
        finally:
            for child in started:
                if child.poll() is None:  # pragma: no cover - the stop worked
                    child.kill()
                    child.wait(timeout=30)

    def test_losing_the_lock_race_waits_instead_of_giving_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The opposite case, and the reason the two cannot share a return value.
        # Somebody else holds the lock, so an owner *is* coming: giving up here
        # would leave this client driving its own browser against the same
        # profile for its whole life, which is the per-call handoff the daemon
        # exists to remove.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        attempts = 0

        def contended(*args: object, **kwargs: object) -> _Attempt:
            nonlocal attempts
            attempts += 1
            return _Attempt.CONTENDED

        monkeypatch.setattr(election_module, "_start_owner", contended)

        started = time.monotonic()
        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            connect=lambda attachment: Reach.REFUSED,
        )
        elapsed = time.monotonic() - started

        # Both halves, and the second one is what makes this a test. `attempts
        # >= 1` was the whole assertion at first, and it passes just as well for
        # an implementation that gives up after the first CONTENDED — which is
        # exactly the behaviour this test is named against.
        assert attempts > 1, "it stopped looking after the first contended attempt"
        assert elapsed >= 0.9, f"it gave up after {elapsed:.2f}s of a 1.0s budget"


# Deliberately unindented, and run as it stands rather than through
# textwrap.dedent. An indented block here reads as a nicely nested string and is
# one formatter pass away from being reindented into a different program:
# `ruff format` did exactly that to an earlier version of this file and every
# process test failed with an IndentationError from the child.
_INSPECT_OWNER = """
import json
import sys
from pathlib import Path

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner
from linkedin_mcp_server.daemon_lock import DaemonLock

profile = Path(sys.argv[1])
auth_root = profile.parent
config = AppConfig()
config.browser.user_data_dir = str(profile)

outcome = obtain_owner(auth_root, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
# Asked from *this* process, which is the frontend: it must not hold the lock it
# handed over.
print(json.dumps({
    "state": outcome.attachment_lookup.state.value,
    # Which refusal, not merely that there was one. There are five paths to
    # INCOMPATIBLE across `daemon.py` and `daemon_election.py`, and a failure
    # recorded without this cannot say which fired: one such failure in a full
    # suite run left nothing to distinguish a runtime mismatch from a profile
    # mismatch, and the state alone made it look like flake either way.
    "reason": outcome.attachment_lookup.reason,
    "started": outcome.started_owner,
    "pid": attachment.descriptor.pid if attachment else None,
    "url": attachment.descriptor.url if attachment else None,
    "frontend_holds_lock": DaemonLock(auth_root).try_acquire(),
}))
"""


#: A frontend that elects an owner and then *stays alive*, which is what a real
#: stdio server does for as long as its client is connected. It reports the
#: owner's pid and then waits, so the test can kill the owner while this process
#: still exists and ask whether the lock came free.
#:
#: Without a frontend that outlives the owner, the lock always frees: the
#: operating system closes the frontend's descriptors when it exits, so a leaked
#: copy is indistinguishable from a released one. Verified by mutation — with the
#: release removed, every other test here still passed.
_LINGERING_FRONTEND = """
import json
import sys
import time
from pathlib import Path

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner

profile = Path(sys.argv[1])
config = AppConfig()
config.browser.user_data_dir = str(profile)

outcome = obtain_owner(profile.parent, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
print(json.dumps({"pid": attachment.descriptor.pid if attachment else None}))
sys.stdout.flush()
# Long enough for the caller to kill the owner and watch the lock, short enough
# that a failure cannot leave this process behind.
time.sleep(30)
"""


@pytest.fixture
def real_state_root(tmp_path: Path):
    """Let the spawned processes use the account's real daemon state root.

    The unit tests above redirect that root, and the process tests cannot:
    ``_account_home`` reads the account's passwd entry and deliberately ignores
    ``HOME`` (``daemon_descriptor.py:161-190``), precisely so that a launcher
    overriding the environment for one process cannot split an account's
    election in two. The owner is started by production code, so there is
    nowhere to inject a redirection into it, and faking one would test a path
    users never take.

    So these run against the real root, keyed by a unique auth root under
    ``tmp_path``. The daemon directory is a hash of that auth root, so nothing
    here can collide with the user's own state or with a parallel test, and the
    same derivation gives cleanup an exact target.
    """
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    yield profile

    # Owners are detached, so an assertion that fails before its stop() would
    # otherwise leave a server running against the developer's machine.
    directory = daemon_descriptor_module.daemon_dir(profile.parent)
    descriptor = daemon_descriptor_module.descriptor_path(profile.parent)
    try:
        published = daemon_descriptor_module.read(profile.parent)
    except Exception:
        published = None
    if published is not None:
        _stop(published.pid)
    if directory.exists():
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
    assert not descriptor.exists()


def _run_frontend(profile: Path) -> dict[str, object]:
    """Elect an owner from a separate interpreter, as a real client would."""
    import json

    result = subprocess.run(
        [sys.executable, "-c", _INSPECT_OWNER, str(profile)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        cwd=_REPO_ROOT,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _alive(pid: int) -> bool:
    """Whether a process still exists. Signal 0 checks without delivering one."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _resume(pid: object) -> None:
    """Let a stopped process run again, tolerating one that is already gone.

    Separate from :func:`_stop` because a paused process that is never resumed
    cannot be killed cleanly either: ``SIGKILL`` is delivered, but the test's own
    cleanup has no way to observe the exit while the process is not scheduled.
    """
    if not isinstance(pid, int):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGCONT)


def _stop(pid: object) -> None:
    """Kill an owner, taking the pid as it comes out of the child's JSON.

    Untyped on purpose: every caller has just read this out of a subprocess's
    output, and threading a cast through each of them would add noise to the
    cleanup path without making anything safer. A pid that is not an integer is
    nothing to kill.
    """
    if not isinstance(pid, int):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.slow
class TestRealOwner:
    """The whole thing, with a real detached process on the other end.

    Slow, and worth it: everything these assert is invisible to a test that
    stays in one interpreter. Each spawn starts the full server graph.
    """

    def test_an_owner_is_started_and_answers(self, real_state_root: Path):
        profile = real_state_root

        result = _run_frontend(profile)
        try:
            assert result["state"] == OwnerState.ATTACHABLE.value, result
            assert result["started"] is True
            assert result["pid"] and result["pid"] != os.getpid()
        finally:
            _stop(result.get("pid"))

    @_POSIX_ONLY
    def test_a_paused_owner_is_waited_for_rather_than_written_off(
        self, real_state_root: Path
    ):
        """The reported bug, against a real owner over real loopback.

        The in-process tests above drive this through an injected probe, which
        proves the loop's logic and nothing about the transport. Here the socket,
        the token and the process are all real: the owner is genuinely stopped,
        so its port stays open with nobody reading it, and the frontend's probe
        genuinely times out rather than being told to.

        Deterministic rather than timed. The owner is stopped *before* the second
        frontend starts and released on a timer, so the frontend cannot miss the
        window: every probe it makes until the resume lands in a stalled process.
        The issue reproduced this by timing a stop against the post-start ping,
        which is the same condition arrived at by luck.
        """
        profile = real_state_root

        first = _run_frontend(profile)
        owner = first["pid"]
        assert isinstance(owner, int), first
        assert first["state"] == OwnerState.ATTACHABLE.value, first

        resume = threading.Timer(8.0, lambda: _resume(owner))
        try:
            os.kill(owner, signal.SIGSTOP)
            resume.start()

            second = _run_frontend(profile)

            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["pid"] == owner, "the paused owner was replaced"
            assert second["started"] is False, "a second owner was started"
        finally:
            resume.cancel()
            _resume(owner)
            _stop(owner)

    def test_the_frontend_lets_go_of_the_lock_it_handed_over(
        self, real_state_root: Path
    ):
        # The load-bearing one. Both descriptors refer to one locked open file
        # description, so a frontend that kept its copy would keep the daemon
        # lock alive after the owner died — and every recovery afterwards would
        # be locked out by a process that is not the owner and does not know it
        # holds anything.
        profile = real_state_root

        result = _run_frontend(profile)
        try:
            # Asked inside the frontend, before it exited: the lock is held by
            # the owner, and the frontend could not take it.
            assert result["frontend_holds_lock"] is False, result
        finally:
            _stop(result.get("pid"))

    def test_the_lock_frees_while_the_frontend_is_still_running(
        self, real_state_root: Path
    ):
        # The one that actually proves the release, and the reason it has to be
        # written this way. Both descriptors refer to one locked open file
        # description, so the lock lives as long as either is open. Every other
        # test here lets the frontend exit first, which closes its copy whether
        # the code released it or not — verified by mutation: with the release
        # removed, all of them still passed and this one fails.
        #
        # A frontend that leaked its copy would keep the daemon lock held after
        # the owner died, so no replacement could ever be elected while that
        # client stayed connected. That is a wedge with no visible cause.
        import json

        profile = real_state_root
        auth_root = profile.parent

        frontend = subprocess.Popen(
            [sys.executable, "-c", _LINGERING_FRONTEND, str(profile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
        )
        try:
            assert frontend.stdout is not None
            owner_pid = json.loads(frontend.stdout.readline())["pid"]
            assert isinstance(owner_pid, int), "no owner was elected"
            assert frontend.poll() is None, "the frontend exited too early"

            _stop(owner_pid)

            freed = False
            for _ in range(500):
                time.sleep(0.01)
                probe = DaemonLock(auth_root)
                if probe.try_acquire():
                    probe.release()
                    freed = True
                    break
            assert freed, (
                "the daemon lock is still held after the owner died, so the "
                "frontend kept its copy of it"
            )
        finally:
            frontend.kill()
            frontend.wait(timeout=30)

    def test_the_lock_frees_when_the_owner_dies(self, real_state_root: Path):
        # The other half of the same property. The frontend has exited by now,
        # so if it had leaked its copy the lock would still be held here even
        # though the owner is gone. Measured at 10 ms on this tree.
        profile = real_state_root
        auth_root = profile.parent

        result = _run_frontend(profile)
        pid = result["pid"]
        assert isinstance(pid, int)
        assert daemon_is_running(auth_root)

        _stop(pid)

        freed = False
        for _ in range(300):
            time.sleep(0.01)
            probe = DaemonLock(auth_root)
            if probe.try_acquire():
                probe.release()
                freed = True
                break
        assert freed, "the daemon lock outlived the owner that held it"

    def test_a_second_frontend_attaches_instead_of_starting_another_owner(
        self, real_state_root: Path
    ):
        # The point of the whole feature: one browser, however many clients.
        profile = real_state_root

        first = _run_frontend(profile)
        try:
            assert first["started"] is True
            second = _run_frontend(profile)

            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is False, "a second owner was started"
            assert second["pid"] == first["pid"]
        finally:
            _stop(first.get("pid"))

    def test_a_crashed_owner_is_replaced(self, real_state_root: Path):
        # The descriptor survives the process that wrote it, so the next client
        # must prove the endpoint before trusting it and elect a replacement
        # when it does not answer.
        profile = real_state_root

        first = _run_frontend(profile)
        dead = first["pid"]
        assert isinstance(dead, int)
        _stop(dead)
        # The kernel frees the lock at death; wait for it rather than assuming.
        for _ in range(300):
            if not daemon_is_running(profile.parent):
                break
            time.sleep(0.01)

        second = _run_frontend(profile)
        try:
            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is True, "the corpse was attached to"
            assert second["pid"] != dead
        finally:
            _stop(second.get("pid"))

    @_POSIX_ONLY
    def test_a_child_that_dies_after_adopting_the_lock_leaves_it_free(
        self, real_state_root: Path, tmp_path: Path
    ):
        # The worst interleaving in the whole handoff. The frontend takes the
        # lock, hands a duplicate to the child, and releases its own copy the
        # moment the child has it — so from then on the lock exists *only* in a
        # process that has not yet proved it can serve. A child that dies there
        # must leave the profile electable, or the daemon is permanently wedged
        # by a single bad start.
        #
        # Exercised with a child that really adopts and then exits, rather than
        # one that fails earlier: a child that never got as far as `adopt` would
        # pass this test while proving nothing about the case it is named for,
        # which is why the marker is asserted.
        #
        # What this cannot fail on: the kernel reclaims a dead process's
        # descriptors whatever the code did, so no mutation to `adopt` or
        # `release` makes it go red — both were tried. It is an end-to-end
        # statement that this sequence leaves a usable profile, not a test of
        # the release logic, and it earns its place by covering the composition
        # (adopt, die, elect again) that the unit-level tests each cover only
        # half of.
        child = tmp_path / "adopting_child.py"
        marker = tmp_path / "adopted"
        child.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon_lock as daemon_lock\n"
            "lock = daemon_lock.DaemonLock(Path(sys.argv[2]))\n"
            "lock.adopt(int(sys.argv[1]))\n"
            "Path(sys.argv[3]).write_text('adopted')\n"
            "raise SystemExit(7)\n"
        )

        profile = real_state_root
        auth_root = profile.parent
        # The substitution only rewrites the daemon's own command line and
        # forwards everything else untouched. Replacing `subprocess.Popen`
        # outright looks equivalent and is not: on Linux `ctypes.util
        # .find_library` shells out to `ldconfig` from deep inside the private
        # state hardening this very call performs, so an unconditional rewrite
        # tried to turn *that* into the daemon and failed the test on CI while
        # passing on macOS, where find_library takes another route.
        frontend = (
            "import sys\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon_election as election\n"
            "real = election.subprocess.Popen\n"
            "def substitute(command, **kwargs):\n"
            "    if '--lock-fd' not in command:\n"
            "        return real(command, **kwargs)\n"
            "    fd = command[command.index('--lock-fd') + 1]\n"
            "    return real(\n"
            "        [command[0], sys.argv[2], fd, sys.argv[3], sys.argv[4]],\n"
            "        **kwargs,\n"
            "    )\n"
            "election.subprocess.Popen = substitute\n"
            "from linkedin_mcp_server.config.schema import AppConfig\n"
            "profile = Path(sys.argv[1])\n"
            "config = AppConfig()\n"
            "config.browser.user_data_dir = str(profile)\n"
            "election.obtain_owner(\n"
            "    profile.parent, profile, config, deadline_seconds=15\n"
            ")\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                frontend,
                str(profile),
                str(child),
                str(auth_root),
                str(marker),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
            timeout=200,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert marker.exists(), "the child never reached the adoption being tested"

        probe = DaemonLock(auth_root)
        assert probe.try_acquire(), (
            "the lock survived the only process holding it, so nothing can ever "
            "elect an owner for this profile again"
        )
        probe.release()
        # What this does *not* establish: that the frontend released its own
        # copy. The frontend here is a subprocess that has exited by now, so the
        # kernel closed its descriptors whatever the code did — verified by
        # mutation, with `lock.release()` removed this still passed.
        # `test_the_lock_frees_while_the_frontend_is_still_running` is the one
        # that pins the release, by keeping the frontend alive across the kill.
        # This test's own subject is the child: adopting the lock and then dying
        # must not strand it.

        # And an ordinary election works afterwards, which is the outcome the
        # user actually needs.
        recovered = _run_frontend(profile)
        try:
            assert recovered["state"] == OwnerState.ATTACHABLE.value, recovered
        finally:
            _stop(recovered.get("pid"))

    def test_many_clients_starting_at_once_elect_exactly_one_owner(
        self, real_state_root: Path
    ):
        # The property the whole feature is for, under the condition that
        # actually produces races: several MCP clients launching together, each
        # spawning its own stdio server, all of them arriving at an empty state
        # directory at the same moment.
        #
        # Two browsers on one profile is the failure this must never have. The
        # single-frontend tests cannot see it, because nothing contends there.
        import json

        profile = real_state_root
        clients = 8

        running = [
            subprocess.Popen(
                [sys.executable, "-c", _INSPECT_OWNER, str(profile)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
                cwd=_REPO_ROOT,
            )
            for _ in range(clients)
        ]

        results = []
        for frontend in running:
            out, err = frontend.communicate(timeout=300)
            assert frontend.returncode == 0, err[-2000:]
            results.append(json.loads(out.strip().splitlines()[-1]))

        owners = {result["pid"] for result in results}
        try:
            assert None not in owners, "a client ended up with no owner"
            assert len(owners) == 1, f"more than one owner was elected: {owners}"
            # Exactly one of them did the starting; the rest attached to it.
            assert sum(1 for r in results if r["started"]) == 1, results
            assert all(r["state"] == OwnerState.ATTACHABLE.value for r in results)
            # And none of them kept the lock they may have taken on the way.
            assert not any(r["frontend_holds_lock"] for r in results)
        finally:
            for pid in owners:
                _stop(pid)

    @_POSIX_ONLY
    def test_the_owner_outlives_the_client_that_started_it(self, real_state_root: Path):
        # The premise of the whole feature. An owner that died with its first
        # client would give every later client a cold start plus a fresh
        # ``/feed/`` validation, which is the traffic this exists to remove.
        #
        # Killed by process *group*, not by pid, because that is how a client
        # shutdown reaches a server it spawned — and a child that merely had its
        # own pid would still be caught by it. ``start_new_session`` is what puts
        # the owner in a group of its own.
        import json

        profile = real_state_root

        frontend = subprocess.Popen(
            [sys.executable, "-c", _LINGERING_FRONTEND, str(profile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
            # The test process needs a group of its own too, or the killpg below
            # takes pytest with it. Found the direct way.
            start_new_session=True,
        )
        owner_pid = None
        try:
            assert frontend.stdout is not None
            owner_pid = json.loads(frontend.stdout.readline())["pid"]
            assert isinstance(owner_pid, int), "no owner was elected"

            os.killpg(os.getpgid(frontend.pid), signal.SIGKILL)
            frontend.wait(timeout=30)

            # Given a moment, because a group signal is not instantaneous and a
            # check that raced it would pass for the wrong reason.
            time.sleep(1.0)
            assert _alive(owner_pid), "the owner died with the client that started it"
        finally:
            _stop(owner_pid)
            if frontend.poll() is None:  # pragma: no cover - the kill worked
                frontend.kill()
                frontend.wait(timeout=30)

    def test_an_older_owner_hands_over_to_a_newer_build(self, real_state_root: Path):
        # The turnover, end to end against a live owner rather than a mock. Both
        # halves have to hold: the old process actually exits, and a replacement
        # is elected. Getting only the first is worse than doing nothing, and it
        # is what happened here twice while this was being built — the owner
        # stood down, and the frontend then spent its whole budget re-reading
        # the descriptor it had already rejected rather than taking the lock the
        # departing owner had just freed.
        #
        # The running owner is made to *claim* an old version by rewriting the
        # published descriptor. `package_version` is covered by neither the token
        # digest nor the configuration fingerprint, so the file stays valid in
        # every other respect and the process behind it is genuinely serving.
        import json

        profile = real_state_root
        auth_root = profile.parent

        first = _run_frontend(profile)
        old_pid = first["pid"]
        assert isinstance(old_pid, int)

        descriptor_file = daemon_descriptor_module.descriptor_path(auth_root)
        published = json.loads(descriptor_file.read_text())
        published["package_version"] = "1.0.0"
        descriptor_file.write_text(json.dumps(published, indent=2, sort_keys=True))

        started = time.monotonic()
        second = _run_frontend(profile)
        elapsed = time.monotonic() - started
        try:
            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is True, "no replacement owner was elected"
            assert second["pid"] != old_pid, "the stale owner is still serving"

            for _ in range(500):
                if not _alive(old_pid):
                    break
                time.sleep(0.01)
            assert not _alive(old_pid), "the stale owner never stood down"

            # And it has to be quick, because this happens on the launch after
            # every upgrade and the user is waiting for their client to come up.
            # Asserted separately from correctness because the two failed
            # separately: with the post-turnover wait reinstated the outcome was
            # still right and it took a minute and a quarter instead of three
            # seconds. Measured at 2.1s here, so the bound is generous enough for
            # a loaded CI machine and far below the stall it guards against.
            assert elapsed < 30, f"the upgrade took {elapsed:.1f}s"
        finally:
            _stop(second.get("pid"))
            _stop(old_pid)

    def test_the_owner_requires_its_token(self, real_state_root: Path):
        # The endpoint is loopback, which every process on the machine can
        # reach, and a website the user merely visits can reach through their
        # own browser. The token is what stands between that and a logged-in
        # LinkedIn session.
        import asyncio

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        profile = real_state_root
        result = _run_frontend(profile)
        url = result["url"]
        assert isinstance(url, str)

        try:

            async def unauthenticated() -> None:
                async with Client(StreamableHttpTransport(url, auth="wrong")) as client:
                    await client.ping()

            with pytest.raises(Exception):
                asyncio.run(unauthenticated())
        finally:
            _stop(result.get("pid"))

    def test_only_the_current_generations_token_stays_on_disk(
        self, real_state_root: Path
    ):
        # `publish` writes one token file per instance and removes none, so
        # without cleanup every restart leaves another credential behind.
        profile = real_state_root
        auth_root = profile.parent

        first = _run_frontend(profile)
        _stop(first["pid"])
        for _ in range(300):
            if not daemon_is_running(auth_root):
                break
            time.sleep(0.01)
        second = _run_frontend(profile)

        try:
            directory = daemon_descriptor_module.daemon_dir(auth_root)
            tokens = sorted(p.name for p in directory.glob("token-*"))
            assert len(tokens) == 1, tokens
            assert second["state"] == OwnerState.ATTACHABLE.value
        finally:
            _stop(second.get("pid"))

    def test_a_proxy_serves_the_real_owners_tools_over_loopback(
        self, real_state_root: Path
    ):
        """The whole feature, end to end, in the only test that proves it.

        Everything else about the proxy runs in memory against a stand-in owner.
        This is the one that exercises the published descriptor, the real bearer
        token and a real socket together, which is where a wrong URL, a
        mis-scoped credential or a refused loopback hop would actually show up.

        Deliberately no tool is *executed*: the owner has no LinkedIn session and
        driving a browser is not what this asserts. Serving the owner's list
        through an authenticated round trip is.
        """
        import asyncio

        from fastmcp import Client

        from linkedin_mcp_server.daemon import look_up_owner
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        profile = real_state_root
        result = _run_frontend(profile)
        try:
            assert result["state"] == OwnerState.ATTACHABLE.value, result

            # Read the way a frontend does, rather than reusing what the child
            # returned: the descriptor and token on disk are what a proxy is
            # built from.
            lookup = look_up_owner(profile.parent, profile, _config(profile))
            assert lookup.attachment is not None, lookup.reason

            proxy = create_mcp_server(
                tool_timeout=30.0,
                role=ServerRole.PROXY,
                proxy_attachment=lookup.attachment,
            )

            async def served() -> set[str]:
                async with Client(proxy) as client:
                    return {tool.name for tool in await client.list_tools()}

            names = asyncio.run(served())

            # The owner registers the full local set, so the proxy must show it
            # all — including `close_session`, the one defined inline rather than
            # in a `register_*` call.
            assert "get_person_profile" in names
            assert "close_session" in names
            assert len(names) == 19, sorted(names)
        finally:
            _stop(result.get("pid"))

    def test_a_real_owner_asks_the_client_to_sign_in(self, real_state_root: Path):
        """The auth marker, over a real socket, from a real detached owner.

        Everything else about the marker runs in memory. This is the one that
        proves the owner produces it with no LinkedIn session on disk and that it
        survives the authenticated loopback hop as a *result* rather than being
        flattened into a generic failure, which is what an exception would be.

        Observed at a plain client with no repair middleware installed, and that
        is what makes the test possible: a fully wired frontend *consumes* the
        marker, opens a real login window and waits out the login timeout. So this
        asserts what the owner sends; the consume-and-replay half is covered in
        memory with a stubbed login.
        """
        import asyncio

        from fastmcp import Client

        from linkedin_mcp_server.daemon import look_up_owner
        from linkedin_mcp_server.daemon_auth import (
            MARKER_KEY,
            MARKER_VERSION,
            FrontendAuthRepairMiddleware,
        )
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        profile = real_state_root
        # The browser cache is keyed to the auth root, and the fixture's is empty,
        # so without this the owner answers "still downloading Chromium" and never
        # reaches the auth gate at all. Symlinked rather than copied: it is about a
        # gigabyte, and nothing here launches a browser.
        _borrow_the_browser_cache(profile)

        result = _run_frontend(profile)
        try:
            lookup = look_up_owner(profile.parent, profile, _config(profile))
            assert lookup.attachment is not None, lookup.reason

            proxy = create_mcp_server(
                tool_timeout=30.0,
                role=ServerRole.PROXY,
                proxy_attachment=lookup.attachment,
            )
            # The repair middleware is removed rather than never added, because
            # `create_mcp_server` installs it for every proxy and that is correct:
            # a real frontend consumes this marker, opens a login window and waits
            # out the login timeout. Removing it is what lets the marker be
            # observed at all, and it is the reason this test asserts what the
            # owner *sends* rather than what a client finally sees.
            proxy.middleware[:] = [
                m
                for m in proxy.middleware
                if not isinstance(m, FrontendAuthRepairMiddleware)
            ]

            async def call_it():
                async with Client(proxy) as client:
                    return await client.call_tool(
                        "get_person_profile",
                        {"linkedin_username": "williamhgates"},
                        raise_on_error=False,
                    )

            answered = asyncio.run(call_it())

            assert answered.is_error is True
            marker = (answered.meta or {}).get(MARKER_KEY)
            assert marker is not None, answered.meta
            assert marker["v"] == MARKER_VERSION
            # `missing`, because the fixture's profile has never been logged in.
            assert marker["reason"] == "missing"
            # Nothing ran before the readiness gate refused, so a client may run
            # the call again once it has signed in.
            assert marker["replayable"] is True
            # And the owner never opened a browser to find this out, so the
            # profile is free for the login it is asking for.
            assert marker["browser_open"] is False

            # Worth recording what this does and does not pin. Removing the
            # owner's middleware makes it fail, and so does removing *both* role
            # claims. Removing only the one in `main` does not, because
            # `create_mcp_server` claims it again before any call arrives: the two
            # are deliberately redundant, and this test cannot tell them apart.
        finally:
            _stop(result.get("pid"))

    def test_a_proxy_refuses_an_owner_it_has_the_wrong_token_for(
        self, real_state_root: Path
    ):
        """The credential is load-bearing, not decoration.

        The owner listens on a loopback port that every process on the machine
        can reach, and that a website can reach through the user's own browser.
        If a wrong token were served anyway, that port would be an open door to a
        logged-in LinkedIn session.
        """
        import asyncio
        import dataclasses

        from fastmcp import Client

        from linkedin_mcp_server.daemon import look_up_owner
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        profile = real_state_root
        result = _run_frontend(profile)
        try:
            lookup = look_up_owner(profile.parent, profile, _config(profile))
            assert lookup.attachment is not None, lookup.reason

            wrong = dataclasses.replace(lookup.attachment, token="not-the-token")
            proxy = create_mcp_server(
                tool_timeout=30.0, role=ServerRole.PROXY, proxy_attachment=wrong
            )

            async def served() -> None:
                async with Client(proxy) as client:
                    await client.list_tools()

            # Matched on the status rather than catching anything at all. A bare
            # `Exception` passed on any failure whatsoever, including one raised
            # before a request ever left this process — so the test would have
            # gone green without the owner refusing anything. Measured: the real
            # refusal is `McpError: Client error '401 Unauthorized'`.
            with pytest.raises(Exception, match="401 Unauthorized"):
                asyncio.run(served())
        finally:
            _stop(result.get("pid"))


class TestVersionSkew:
    """``@latest`` means two versions can be on one machine at the same time."""

    def test_an_older_owner_is_asked_to_hand_the_browser_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The shipped code published `package_version` and never compared it, so
        # a months-old owner would serve its own tools to every freshly updated
        # frontend indefinitely. That is the "a pinned version quietly rots"
        # failure the README warns about, except the user *did* update.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config, package_version="1.0.0")

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            # Would attach if version were not consulted, which is what makes
            # this test about the version rather than about reachability.
            connect=lambda attachment: Reach.ANSWERED,
        )

        assert asked, "the stale owner was never asked to stand down"
        assert not outcome.worth_connecting

    def test_a_newer_owner_is_used_rather_than_downgraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The other direction, and it must not be symmetric. An older frontend
        # meeting a newer owner has found something at least as new as itself;
        # restarting the shared browser to satisfy one stale client would be the
        # worse trade, and it would also thrash whenever two versions coexist.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config, package_version="99.0.0")

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.ANSWERED,
        )

        assert not asked
        assert outcome.worth_connecting

    def test_an_unrecognisable_version_is_served_rather_than_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A local or editable install has a version neither side can order.
        # Treating that as a skew would force a restart on every single launch,
        # which is exactly where somebody is working on this code.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(
            auth_root, profile, config, package_version="not-a-version"
        )

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: Reach.ANSWERED,
        )

        assert not asked
        assert outcome.worth_connecting

    def test_a_stand_down_request_needs_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The control route is mounted outside fastmcp's authentication
        # middleware — measured on 3.4.4, an unauthenticated POST to a custom
        # route is served — so it checks the token itself. Without that, any
        # process on the machine, and any page the user's browser visits, could
        # stop the shared browser at will.
        import asyncio
        import socket

        import httpx
        import uvicorn

        from linkedin_mcp_server import config as config_module
        from linkedin_mcp_server import daemon_owner

        config = _config(_profile(tmp_path))
        # The owner installs its handed-over configuration before it serves, and
        # the lifespan depends on that: `get_config()` would otherwise parse this
        # process's command line, which under pytest means argparse exits and
        # startup fails. Found by writing this test without it.
        #
        # Set through monkeypatch rather than `set_config`, which is a module
        # global that would then leak into every test that ran afterwards.
        monkeypatch.setattr(config_module, "_config", config)

        stopped: list[str] = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = daemon_owner.create_owner_server(
            config=config,
            token="the-token",
            host="127.0.0.1",
            port=port,
            stand_down=lambda: stopped.append("asked"),
        )
        assert isinstance(server, uvicorn.Server)
        url = f"http://127.0.0.1:{port}{daemon_owner.STAND_DOWN_PATH}"

        async def exercise() -> tuple[int, int]:
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                # Through the production wait, which watches the serving task
                # alongside the flag. A bare `while not server.started` loop
                # never ends when startup fails, which is how this test hung
                # before it was written this way.
                await daemon_owner._await_started(server, serving)
                async with httpx.AsyncClient() as client:
                    without = await client.post(url)
                    wrong = await client.post(
                        url, headers={"Authorization": "Bearer nope"}
                    )
                    assert not stopped, "an unauthenticated caller stopped the daemon"

                    good = await client.post(
                        url, headers={"Authorization": "Bearer the-token"}
                    )
                    assert good.status_code == 200
                return without.status_code, wrong.status_code
            finally:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(serving, timeout=15)

        without_status, wrong_status = asyncio.run(exercise())

        assert without_status == 401
        assert wrong_status == 401
        assert stopped == ["asked"]

    def test_the_token_never_goes_through_a_configured_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Every request the daemon makes carries the bearer token for a server
        # driving a logged-in LinkedIn session, and every one is addressed to
        # loopback. httpx honours HTTP_PROXY by default and does so even for
        # 127.0.0.1 unless NO_PROXY happens to say otherwise — reproduced
        # against a capture proxy, which received the absolute loopback URL and
        # `Authorization: Bearer <token>` in full.
        #
        # A user with a corporate proxy configured is the ordinary case, not an
        # exotic one, and the browser's own proxy setting is deliberately about
        # LinkedIn's traffic rather than the server's.
        import socket
        import threading

        from linkedin_mcp_server import daemon_owner

        received: list[bytes] = []
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        proxy_port = listener.getsockname()[1]

        def capture() -> None:
            with contextlib.suppress(OSError):
                connection, _ = listener.accept()
                received.append(connection.recv(4096))
                connection.close()

        watcher = threading.Thread(target=capture, daemon=True)
        watcher.start()

        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        try:
            # Port 9 is discard: nothing listens, so this fails either way. What
            # is being tested is *where* it was addressed.
            with daemon_owner.direct_http_client(timeout=2) as client:
                with contextlib.suppress(Exception):
                    client.post(
                        "http://127.0.0.1:9/control/stand-down",
                        headers={"Authorization": "Bearer SUPERSECRET"},
                    )
            watcher.join(timeout=3)
        finally:
            listener.close()

        assert not received, "the request was routed through the proxy"
        assert not any(b"SUPERSECRET" in seen for seen in received)

    def test_the_owner_is_not_started_from_the_working_directory(self, tmp_path: Path):
        # `python -m` puts the inherited working directory first on sys.path, so
        # a workspace containing linkedin_mcp_server/daemon_owner.py is imported
        # in preference to the installed package. That code would receive the
        # inherited lock descriptor and the whole configuration on standard
        # input, proxy_password included — merely because an MCP client happened
        # to be started in that directory.
        #
        # Exercised against a real interpreter rather than by inspecting the
        # command, because what matters is which module actually loads.
        workspace = tmp_path / "workspace"
        shadow = workspace / "linkedin_mcp_server"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text("")
        (shadow / "daemon_owner.py").write_text("print('HIJACKED')\n")

        # The real command, with `-m <module>` swapped for a `-c` that reports
        # which file that module resolved to. Everything before it — the
        # interpreter and its flags — is exactly what production uses.
        command = election_module._spawn_command(lock_fd=None)
        assert command[-2:] == ["-m", "linkedin_mcp_server.daemon_owner"], command
        result = subprocess.run(
            [
                *command[:-2],
                "-c",
                "import linkedin_mcp_server.daemon_owner as m; print(m.__file__)",
            ],
            capture_output=True,
            text=True,
            cwd=workspace,
            # `PYTHONPATH=.` on purpose, and it is the whole point of the test.
            # `-P` alone drops only the *implicit* working directory and leaves
            # PYTHONPATH in force, so this very common setting puts the
            # workspace back at the front — measured, it loaded the local file.
            # Isolated mode is what refuses both.
            env={**os.environ, "PYTHONPATH": "."},
            timeout=60,
        )

        assert result.returncode == 0, result.stderr[-1000:]
        loaded = result.stdout.strip()
        assert "HIJACKED" not in result.stdout, "the working directory won"
        assert loaded.startswith(str(_REPO_ROOT)), loaded

    def test_the_owner_does_not_inherit_the_proxy_password(self):
        # The configuration travels on standard input specifically so that a
        # password is not left in a readable environment. That is only half done
        # while the child inherits the frontend's environment unchanged: the
        # documented way to configure a proxy is PROXY_PASSWORD, so the owner
        # would hold the raw value in /proc/<pid>/environ for its whole life,
        # which is far longer than the frontend's.
        import os as os_module

        original = dict(os_module.environ)
        os_module.environ["PROXY_PASSWORD"] = "hunter2"
        os_module.environ["PROXY_USERNAME"] = "someone"
        try:
            handed = election_module._owner_environment()
        finally:
            os_module.environ.clear()
            os_module.environ.update(original)

        assert "PROXY_PASSWORD" not in handed
        assert "PROXY_USERNAME" not in handed
        # And the rest of the environment still crosses: the owner needs PATH,
        # HOME and the rest to run at all.
        assert "PATH" in handed

    def test_a_non_ascii_token_is_refused_rather_than_raising(self):
        # `hmac.compare_digest` raises TypeError when either *string* holds a
        # character above ASCII, and the presented one arrives in a header from
        # anything that can reach the port. Compared as strings the route
        # answered 500, which turns an unauthenticated request into a way to
        # provoke an error rather than a refusal. Found by trying it.
        #
        # Tested against the comparison rather than over HTTP, because httpx
        # refuses to encode such a header and so cannot send the request at all.
        # That is a property of one client library, not of the endpoint, which
        # is reachable by anything on the machine.
        from linkedin_mcp_server.daemon_owner import _matches_token

        assert not _matches_token("töken", "the-token")
        assert not _matches_token("\udcff", "the-token")
        assert _matches_token("the-token", "the-token")
        assert _matches_token("  the-token  ", "the-token")


class TestBudgets:
    def test_the_owner_may_not_take_longer_than_the_frontend_will_wait(self):
        # The frontend now stops a child that has said nothing by the end of its
        # budget, which makes the relationship between the two numbers load
        # bearing rather than cosmetic: an owner allowed to take longer would be
        # killed while still following its own rules, on a slow machine, and the
        # user would see an election that never succeeds.
        #
        # They were out of step. The owner spent its full allowance twice, once
        # waiting for uvicorn and again probing, so it could legitimately answer
        # at 60s while the frontend gave up at 45. The two stages share one
        # deadline now, and this pins the remaining margin.
        from linkedin_mcp_server import daemon_owner

        assert (
            daemon_owner._STARTUP_PROBE_SECONDS
            < election_module.DEFAULT_ELECTION_SECONDS
        ), "an owner can now outlast the frontend that is waiting for it"

        # With room to spare, because the frontend spends part of its budget on
        # the configuration handover and on lock attempts before the owner's
        # clock even starts.
        assert daemon_owner._STARTUP_PROBE_SECONDS <= (
            election_module.DEFAULT_ELECTION_SECONDS / 2
        )


class TestNotHoldingTheLockForever:
    """An owner that cannot serve must not keep the position occupied.

    Both waits below are on the owner's side, after it already holds the daemon
    lock. Unbounded, either one turns a hung dependency into a profile nothing
    can ever elect an owner for again: the frontend times out and moves on, and
    the lock stays held by a process that is not serving anything.
    """

    def test_a_startup_that_never_completes_gives_up(self):
        # `server.started` never turning true is not the same as the server
        # dying, and only the second case was handled. An ASGI lifespan that
        # hangs leaves the task pending and the flag false for good.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        class NeverStarts:
            started = False
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                await asyncio.sleep(3600)

        async def exercise() -> None:
            server = NeverStarts()
            serving = asyncio.create_task(server.serve())
            try:
                with pytest.raises(TimeoutError):
                    await daemon_owner._await_started(server, serving, timeout=0.5)
            finally:
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving

        asyncio.run(exercise())

    def test_an_owner_that_cannot_release_the_profile_gives_way(self):
        """A wedged owner has to remove itself, because nothing else can.

        When Chromium's shutdown cannot be confirmed the profile lease is kept
        until the process exits, deliberately (``drivers/browser.py``). For a
        single-process server the resulting message is actionable: its client
        restarts it. For a detached owner it is not -- it outlives that client,
        and the next frontend attaches straight back to it and meets the same
        held profile. Measured before this: recovery needed killing the process
        by hand.

        Asserted through the real serve loop, because that is the only thing that
        can end the process cleanly; the request itself is made in
        ``dependencies`` and covered there.
        """
        import asyncio

        from linkedin_mcp_server import daemon_owner, server_role

        class Serving:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                while not self.should_exit:
                    await asyncio.sleep(0.02)

        async def exercise() -> bool:
            server = Serving()
            serving = asyncio.create_task(server.serve())
            loop = asyncio.create_task(
                daemon_owner._serve_until_stopped(server, serving, [])
            )
            try:
                await asyncio.sleep(0.3)
                assert not loop.done(), "a healthy owner stopped serving"

                server_role.ask_this_process_to_stand_down("the profile is held")
                await asyncio.wait_for(loop, timeout=5)
                return True
            except TimeoutError:
                return False
            finally:
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving
                server_role.reset_process_role_for_testing()

        assert asyncio.run(exercise()), "the wedged owner kept the lock"

    def test_a_shutdown_that_never_completes_stops_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Uvicorn's `timeout_graceful_shutdown` bounds the connection tasks and
        # nothing bounds the lifespan behind them. An owner stuck there has
        # already told a frontend it was standing down, so every later election
        # finds the position held by a process that will never serve again.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        class NeverStops:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                await asyncio.sleep(3600)

        monkeypatch.setattr(daemon_owner, "_STAND_DOWN_SHUTDOWN_SECONDS", 0.5)
        # The real one calls `os._exit`, which would take this test runner with
        # it. Substituted so the *wait* can be observed here; that it really
        # ends the process is the separate real-process test below, which is the
        # only way to see that at all.
        exited: list[bool] = []
        monkeypatch.setattr(daemon_owner, "_exit_hard", lambda: exited.append(True))

        async def exercise() -> float:
            server = NeverStops()
            serving = asyncio.create_task(server.serve())
            began = time.monotonic()
            try:
                await daemon_owner._serve_until_stopped(server, serving, ["asked"])
                return time.monotonic() - began
            finally:
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving

        elapsed = asyncio.run(exercise())

        assert elapsed < 5, f"the stand-down wait took {elapsed:.1f}s"
        assert exited, "the wait ended without asking the process to end"

    def test_a_shutdown_that_resists_cancellation_still_ends_the_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Ending the *wait* is not ending the process, and only the second frees
        # the lock. Returning from the bounded wait leaves the serving task
        # pending; `asyncio.run` then cancels it and waits, unbounded, for that
        # cancellation to finish. A task that suppresses cancellation therefore
        # keeps the interpreter alive after every timeout above it has fired.
        #
        # Not a hypothetical shape: `close_browser` holds cancellation back
        # until teardown finishes by design, and the export it waits on first is
        # unbounded, so an unresponsive Chromium produces exactly this.
        #
        # Run as a real process, because what is being tested is that the
        # process ends — which is precisely what an in-process test cannot see.
        # Not `tmp_path / "home"`, which the isolation fixture already made.
        home = tmp_path / "child-home"
        home.mkdir()
        auth_root = tmp_path / "child-auth"
        auth_root.mkdir()

        script = tmp_path / "wedged_owner.py"
        script.write_text(
            "import asyncio, os, sys\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon_descriptor as descriptor\n"
            "descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "from linkedin_mcp_server import daemon_owner\n"
            "from linkedin_mcp_server.daemon_lock import DaemonLock\n"
            "lock = DaemonLock(Path(sys.argv[2]))\n"
            "assert lock.try_acquire()\n"
            "daemon_owner._STAND_DOWN_SHUTDOWN_SECONDS = 0.5\n"
            "class Stubborn:\n"
            "    started = True\n"
            "    should_exit = False\n"
            "    async def serve(self, sockets=None):\n"
            "        try:\n"
            "            await asyncio.sleep(3600)\n"
            "        except asyncio.CancelledError:\n"
            "            await asyncio.sleep(3600)\n"
            "async def main():\n"
            "    server = Stubborn()\n"
            "    serving = asyncio.create_task(server.serve())\n"
            "    await daemon_owner._serve_until_stopped(server, serving, ['asked'])\n"
            "asyncio.run(main())\n"
            "print('THE PROCESS SURVIVED')\n"
        )

        result = subprocess.run(
            [sys.executable, str(script), str(home), str(auth_root)],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            # Generous next to the half-second grace the child is given, and far
            # below the forever this guards against.
            timeout=60,
        )

        assert "THE PROCESS SURVIVED" not in result.stdout, (
            "the interpreter outlived a shutdown it had already given up on"
        )
        # And the lock it held is free, which is the outcome the next election
        # depends on. Asked with the child's own state root, which is where the
        # lock file it took actually lives.
        monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)
        probe = DaemonLock(auth_root)
        assert probe.try_acquire(), "the wedged owner kept the lock"
        probe.release()


class TestPublishingLast:
    """Nothing is discoverable until it has been proved to work."""

    def test_a_descriptor_is_published_only_after_the_endpoint_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The ordering is the whole point of the startup handshake, and it is
        # invisible from the outside once startup has succeeded: both orders end
        # with a published descriptor and a working endpoint. What separates them
        # is the failure case — published first, a client can find and attach to
        # an endpoint that refuses its token, and the diagnosis lands in a
        # different process from the cause.
        #
        # Verified by mutation: with publish moved ahead of the probe, every
        # other test in this file still passed.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        order: list[str] = []

        async def probe(url: str, token: str) -> None:
            order.append("probe")

        def publish_spy(root: Path, descriptor: object, token: str):
            order.append("publish")
            return Path("descriptor"), Path("token")

        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor, "publish", publish_spy, raising=True
        )
        monkeypatch.setattr(
            daemon_owner, "_forget_superseded_tokens", lambda *a, **k: None
        )

        class _StoppedServer:
            """Serves nothing and stops at once, so only the order is observed."""

            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )
        monkeypatch.setattr(
            daemon_owner, "create_owner_server", lambda **kwargs: _StoppedServer()
        )

        handshake = daemon_owner._Handshake(None)
        asyncio.run(
            daemon_owner._serve(
                lock=DaemonLock(auth_root),
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=auth_root / "daemon.log",
                ready=handshake,
            )
        )

        assert order == ["probe", "publish"], order

    def test_ready_is_signalled_only_after_the_descriptor_is_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The frontend treats "ready" as permission to go and read the
        # descriptor, so signalling before writing it turns a rare scheduling
        # order into a client that looks for a file that is not there yet and
        # elects a second owner. Verified by mutation: with the signal moved
        # ahead of the publish, nothing else in this suite noticed.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        order: list[str] = []

        class _Recording:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        class _RecordingHandshake:
            def succeed(self) -> None:
                order.append("ready")

            def fail(self) -> None:  # pragma: no cover - not reached here
                order.append("failed")

            def close(self) -> None:
                return None

        async def probe(url: str, token: str) -> None:
            return None

        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "publish",
            lambda *a, **k: (order.append("publish"), (Path("d"), Path("t")))[1],
        )
        monkeypatch.setattr(
            daemon_owner, "_forget_superseded_tokens", lambda *a, **k: None
        )
        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )
        monkeypatch.setattr(
            daemon_owner, "create_owner_server", lambda **kwargs: _Recording()
        )

        asyncio.run(
            daemon_owner._serve(
                lock=DaemonLock(auth_root),
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=auth_root / "daemon.log",
                ready=_RecordingHandshake(),
            )
        )

        assert order == ["publish", "ready"], order


class _FakeSocket:
    """Stands in for the bound listening socket, which nothing here serves."""

    def __init__(self, address: tuple[str, int]) -> None:
        self._address = address

    def getsockname(self) -> tuple[str, int]:
        return self._address

    def close(self) -> None:
        return None


class TestOutcome:
    def test_the_outcome_reports_what_the_lookup_says(self, tmp_path: Path):
        # A thin wrapper, pinned because callers branch on it and the two
        # answers must not be able to disagree.
        from linkedin_mcp_server.daemon import OwnerLookup

        absent = ElectionOutcome(OwnerLookup(state=OwnerState.ABSENT))

        assert not absent.worth_connecting


def _borrow_the_browser_cache(profile: Path) -> None:
    """Point an isolated auth root at the account's real browser cache.

    The cache lives under the auth root (``bootstrap.browsers_path``), so an
    isolated root looks like a fresh install and the readiness gate reports a
    download in progress before anything about authentication is decided. A
    symlink is enough: these tests read the gate's verdict and never launch
    Chromium.

    Skips the test rather than downloading one, because a test that pulls a
    gigabyte on a cold machine is a test nobody runs.
    """
    real = Path.home() / ".linkedin-mcp" / "patchright-browsers"
    if not real.is_dir():
        pytest.skip("no patchright browser cache to borrow")
    (profile.parent / "patchright-browsers").symlink_to(real)
