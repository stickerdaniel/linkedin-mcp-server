"""Getting one owner started, and never two.

The properties here are about process lifetime, file descriptor inheritance and
the kernel's arbitration, so most of these spawn real processes. A test that
stayed inside one interpreter could show the code takes the branches it means to
and would not show the thing that matters: that exactly one browser-owning
process exists, and that the lock dies with it.

Four of these pin defects that were live in this code and found by running it:
a failed child read as "somebody is starting" and cost a full deadline, a child
that reported failure kept the inherited lock, a crashed owner's descriptor was
handed back as attachable, and the parent's copy of the lock outliving the owner
would have wedged every recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import linkedin_mcp_server.daemon as daemon_module
import linkedin_mcp_server.daemon_config as daemon_config
import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon_election as election_module
import linkedin_mcp_server.daemon_owner as daemon_owner
from linkedin_mcp_server import __version__, process_control
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import Attachment, OwnerLookup, OwnerState
from linkedin_mcp_server.daemon_election import (
    _Attempt,
    _Started,
    ElectionOutcome,
    Reach,
    obtain_owner,
)
from linkedin_mcp_server.daemon_descriptor import (
    CommitPreflightError,
    build,
    new_instance_id,
    new_token,
    publish,
)
from linkedin_mcp_server.daemon_lock import (
    DaemonLock,
    DaemonLockError,
    daemon_is_running,
)
from linkedin_mcp_server.private_state import harden_directory
from linkedin_mcp_server.process_tree import ProcessTreeError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = "test-runtime"
_HANDSHAKE_NONCE = "0123456789abcdef" * 4

#: What a call given a 0.01s bound may take before the bound is not the thing
#: deciding. A hundred times the argument, and still two orders below the
#: multi-second defaults these paths would otherwise use, so it fails an
#: implementation that ignores its timeout without failing a busy machine. Ten
#: times was too tight: a Windows runner spent 0.141s on thread scheduling
#: alone and failed a bound that had worked correctly.
_BOUNDED_CALL_SECONDS = 1.0

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the lock is handed to the child only on POSIX"
)


async def _until(condition: Callable[[], bool], *, seconds: float) -> float:
    """Poll *condition* for at most *seconds* and say how long that took.

    A wall-clock budget rather than a loop count, because one ``asyncio.sleep``
    of a millisecond costs a whole timer tick on Windows, about 15 ms, against
    well under one here. A count calibrated on either platform measures a
    different amount of real time on the other, and the tests that used one
    were reading the timer rather than the behaviour under it.
    """
    started = time.monotonic()
    deadline = started + seconds
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return time.monotonic() - started


def _outcome(task: Any) -> str:
    """What a task that should still be running ended as."""
    if task.cancelled():
        return "cancelled"
    return repr(task.exception())


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
    # Hardened rather than merely created, because on Windows this stands in
    # for the account's profile root and the daemon refuses to key state under
    # one whose permissions are still inherited. A real profile root carries
    # its own protected access list; a plain directory under %TEMP% does not,
    # so a plain one would be testing against a machine nobody has.
    harden_directory(home)
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


def _proxy_backend_for(attachment: Attachment):
    """The state object a proxy is built from, around a proved attachment.

    The election's inputs travel with the answer so that a later change can find
    a replacement. These tests only need the answer, and the profile they pass is
    the one the attachment already describes.
    """
    from linkedin_mcp_server.daemon_proxy import DaemonProxyBackend

    profile = Path(attachment.descriptor.profile_path)
    return DaemonProxyBackend(
        attachment=attachment,
        auth_root=profile.parent,
        profile=profile,
        config=_config(profile),
    )


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

    def test_an_owner_that_is_slow_twice_is_still_attached_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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

        # The stalled owner holds the daemon lock. The child owns lock acquisition,
        # so model its ordinary contention verdict directly rather than starting a
        # real subprocess from this isolated in-process state root.
        monkeypatch.setattr(
            election_module,
            "_start_owner",
            lambda *a, **k: election_module._Attempt.CONTENDED,
        )
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=20.0,
            connect=slow_to_answer,
        )

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


class TestRetryPacing:
    """A buried generation must not turn the retry loop into a spin.

    A descriptor stays compatible on disk after the owner behind it is found
    unusable, so the read that the loop paces itself with answered instantly on
    a file the election had already written off. Nothing slept, and the whole
    start backoff was spent re-reading that file as fast as the filesystem
    would answer.
    """

    def test_a_buried_descriptor_still_costs_the_wait_it_was_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        real_inspect = daemon_module._inspect
        inspections = 0

        def inspect(*args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            return real_inspect(*cast(Any, args))

        real_look_up = election_module.look_up_owner
        passes = 0

        def look_up(*args: object, **kwargs: object) -> OwnerLookup:
            nonlocal passes
            passes += 1
            return real_look_up(*cast(Any, args), **cast(Any, kwargs))

        probes = 0

        def refuse(_attachment: Attachment) -> Reach:
            nonlocal probes
            probes += 1
            return Reach.REFUSED

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(election_module, "look_up_owner", look_up)
        monkeypatch.setattr(
            election_module, "_start_owner", lambda *a, **k: _Attempt.FAILED
        )
        # One start, then nothing but backoff for the rest of the budget. That
        # window is where the spin lived: no start is due, so every pass through
        # the loop was pure observation.
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 30.0)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 30.0)

        began = time.monotonic()
        outcome = obtain_owner(
            auth_root, profile, config, deadline_seconds=0.6, connect=refuse
        )
        elapsed = time.monotonic() - began

        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.state is OwnerState.INCOMPATIBLE
        # The election still uses its budget rather than giving up early.
        assert 0.55 <= elapsed < 5, f"the election ran {elapsed:.2f}s"
        # The bound is the point. Each pass costs a descriptor read on its own
        # thread and a touch of state storage. Measured on this tree against the
        # numbers below: 10 inspections over 6 lookup passes with the pacing in
        # place, and 676 inspections in the same 0.6s without it.
        assert inspections < 40, (
            f"{inspections} descriptor inspections in {elapsed:.2f}s"
        )
        assert passes < 20, f"{passes} lookup passes in {elapsed:.2f}s"
        # And the buried endpoint is never contacted again, however long the
        # loop keeps watching the file it left behind.
        assert probes == 1, f"the buried owner was probed {probes} times"

    def test_a_new_generation_is_still_picked_up_promptly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Pacing must not become blindness.

        The wait is spent watching the same file, so an owner that publishes
        over the buried generation has to be seen inside it rather than after
        it. Otherwise the fix would trade a spin for a stall.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        stale = _publish_stale_owner(auth_root, profile, config)

        published = threading.Event()
        fresh: list[str] = []

        def replace_after_burial(*_args: object, **_kwargs: object) -> _Attempt:
            # Stands in for the owner a real start would have produced: the
            # descriptor is replaced while the loop is inside its paced wait.
            def publish_later() -> None:
                time.sleep(0.1)
                fresh.append(_publish_stale_owner(auth_root, profile, config))
                published.set()

            threading.Thread(target=publish_later, daemon=True).start()
            return _Attempt.FAILED

        seen: list[str] = []

        def reach(attachment: Attachment) -> Reach:
            seen.append(attachment.descriptor.instance_id)
            return (
                Reach.REFUSED
                if attachment.descriptor.instance_id == stale
                else (Reach.ANSWERED)
            )

        monkeypatch.setattr(election_module, "_start_owner", replace_after_burial)
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 30.0)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 30.0)
        # A long paced wait, so that the answer has to come from *inside* it.
        # At the production 0.2s the loop would come round again on its own and
        # a wait that simply slept would look identical to one that watched.
        monkeypatch.setattr(election_module, "_RETRY_SECONDS", 4.0)

        began = time.monotonic()
        outcome = obtain_owner(
            auth_root, profile, config, deadline_seconds=8.0, connect=reach
        )
        elapsed = time.monotonic() - began

        assert published.is_set()
        assert outcome.worth_connecting, "the replacement generation was never seen"
        assert seen == [stale, fresh[0]]
        # One poll interval past publication, not the whole of the wait it landed
        # in. Sleeping the wait out instead of polling it lands at 4s here.
        assert elapsed < 2.0, f"the replacement took {elapsed:.2f}s to be noticed"


class TestFailingFast:
    def test_owner_start_backoff_begins_after_the_initial_burst(self):
        delay = election_module._OWNER_START_RETRY_SECONDS
        delays: list[float] = []

        for starts in range(1, 7):
            delay = election_module._owner_start_delay_after(starts, delay)
            delays.append(delay)

        assert delays == [0.5, 0.5, 1.0, 2.0, 4.0, 8.0]

    def test_failed_children_are_paced_through_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # This child is gone, but a sibling frontend may already have acquired the
        # lock it freed. The election keeps observing and retrying without creating
        # one failed process per polling pass.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        attempts = 0

        def fail(*args: object, **kwargs: object) -> _Attempt:
            nonlocal attempts
            attempts += 1
            return _Attempt.FAILED

        monkeypatch.setattr(election_module, "_start_owner", fail)
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 0.05)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 0.2)

        started = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0.7,
            connect=lambda attachment: Reach.REFUSED,
        )
        elapsed = time.monotonic() - started

        assert not outcome.worth_connecting
        assert 2 <= attempts <= 4
        assert elapsed >= 0.65, f"the election gave up after {elapsed:.2f}s"
        assert elapsed < 2, elapsed

    @pytest.mark.parametrize(
        "refusal",
        [OSError("no loopback listener is available"), DaemonLockError("unusable")],
        ids=["pre-spawn", "unusable-lock"],
    )
    def test_a_start_that_never_ran_hands_back_at_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: Exception
    ):
        """The pair to the paced retry above, and the reason it is not one case.

        Pacing exists for a child that ran and failed, which can have freed a
        lock a sibling frontend now holds. Neither of these ever created one:
        an OSError leaves ``_start_owner`` before ``Popen`` returns and while
        ``_spawn`` holds no lock, and an unusable lock is a filesystem or an
        account, which no amount of waiting changes. Retrying them spends the
        caller's whole budget before it can fall back to a direct browser.
        """
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        attempts = 0

        def refuse(*args: object, **kwargs: object) -> _Attempt:
            nonlocal attempts
            attempts += 1
            raise refusal

        monkeypatch.setattr(election_module, "_start_owner", refuse)
        # Paced as the neighbouring test paces it, so a version that retried
        # would get several attempts inside this budget rather than one.
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 0.05)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 0.2)

        started = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            connect=lambda attachment: Reach.REFUSED,
        )
        elapsed = time.monotonic() - started

        assert not outcome.worth_connecting
        assert attempts == 1
        assert elapsed < 0.5, f"the caller waited {elapsed:.2f}s for its fallback"

    def test_one_blocked_descriptor_inspection_spans_the_whole_election(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        blocked = threading.Event()
        release = threading.Event()
        inspections = 0

        def inspect(*_args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            blocked.set()
            release.wait()
            return OwnerLookup(state=OwnerState.ABSENT)

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.01)
        monkeypatch.setattr(
            election_module, "_start_owner", lambda *a, **k: _Attempt.FAILED
        )
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _config(profile),
                deadline_seconds=0.08,
                connect=lambda attachment: Reach.REFUSED,
            )
        finally:
            release.set()

        assert blocked.is_set()
        assert not outcome.worth_connecting
        assert inspections == 1

    def test_a_later_pass_consumes_the_completed_descriptor_inspection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        release = threading.Event()
        inspections = 0

        def inspect(*_args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            release.wait()
            return OwnerLookup(state=OwnerState.ABSENT)

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        inspector = daemon_module._DescriptorInspector(
            profile.parent, profile, _config(profile)
        )

        with pytest.raises(daemon_module._DescriptorReadTimeout):
            inspector.inspect_until(timeout=0.01)
        release.set()
        lookup = inspector.inspect_until(timeout=1.0)

        assert lookup.state is OwnerState.ABSENT
        assert inspections == 1

    def test_successful_commit_gets_a_fresh_descriptor_inspection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        blocked = threading.Event()
        release = threading.Event()
        real_inspect = daemon_module._inspect
        inspections = 0

        def inspect(*args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                blocked.set()
                release.wait()
                return OwnerLookup(state=OwnerState.ABSENT)
            return real_inspect(*cast(Any, args))

        def start(
            auth_root: Path,
            started_profile: Path,
            started_config: AppConfig,
            **_kwargs: object,
        ) -> _Attempt:
            _publish_stale_owner(auth_root, started_profile, started_config)
            return _Attempt.STARTED

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.01)
        monkeypatch.setattr(election_module, "_start_owner", start)
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                config,
                deadline_seconds=1.0,
                connect=lambda attachment: Reach.ANSWERED,
            )
        finally:
            release.set()

        assert blocked.is_set()
        assert outcome.worth_connecting
        assert inspections == 2

    def test_a_posix_election_never_waits_for_an_exclusion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # There is no legacy namespace to exclude off Windows, so the gate is not
        # a gate there: an inspection that never finishes still leaves the
        # election free to start an owner, exactly as before.
        profile = _profile(tmp_path)
        inspector = daemon_module._DescriptorInspector(
            profile.parent, profile, _config(profile)
        )
        monkeypatch.setattr(election_module, "_IS_WINDOWS", False)

        began = time.monotonic()
        exclusion = election_module._exclusion_state(inspector, deadline=began + 30.0)

        assert exclusion is election_module._Exclusion.READY
        assert not inspector.settled, "the inspection was started to answer this"
        assert time.monotonic() - began < 1.0

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
        releasing = threading.Timer(0.55, winner.release)
        releasing.start()
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 0.01)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 0.2)

        took_over: list[float] = []

        def contend(
            auth_root: Path,
            profile: Path,
            config: AppConfig,
            *,
            timeout: float,
            inspector: object,
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
                deadline_seconds=2,
                connect=lambda attachment: Reach.ANSWERED,
            )
        finally:
            releasing.cancel()
            winner.release()

        assert took_over, "the loser never retried the lock the winner freed"
        assert outcome.worth_connecting
        # And promptly: the point is recovery, not that it eventually happens.
        assert time.monotonic() - began < 1.8

    @_POSIX_ONLY
    def test_group_cleanup_and_fallback_share_one_stop_budget(self, monkeypatch):
        clock = [0.0]
        waits: list[float] = []

        class _Child:
            pid = 4242

            def kill(self) -> None:
                return None

            def wait(self, timeout: float) -> int:
                waits.append(timeout)
                return 0

        def exhaust_group_budget(pgid: int, *, timeout: float, child: object) -> bool:
            assert (pgid, timeout, child) == (4242, 2.0, process)
            clock[0] += timeout
            return False

        process: Any = _Child()
        monkeypatch.setattr(election_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            election_module, "terminate_process_group", exhaust_group_budget
        )

        election_module._stop_child(process)

        assert waits == [0.0]

    @_POSIX_ONLY
    def test_a_child_that_never_reads_its_configuration_does_not_block_the_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The handshake timeout is reached only if the spawn gets that far. The
        # configuration is written to the child's pipe first, and a pipe buffer
        # is small — 64 KiB on Linux — while the configuration has no size limit
        # at all: proxy_bypass and the paths are free-form strings. A child that
        # neither reads nor exits blocks that write indefinitely, before any
        # budget applies, with both processes holding the lock.
        #
        # Reproduced with a 10 MiB bypass list and a child that only sleeps: the
        # outer process timeout fired and the wait was never entered.
        profile = _profile(tmp_path)
        config = _config(profile)
        config.browser.proxy_bypass = "x" * (10 * 1024 * 1024)
        auth_root = profile.parent

        sleepers: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def deaf(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            # Never reads its standard input, which is the defect, and takes
            # SIGTERM at its default disposition, which is what makes the signal
            # that ends it readable off the exit status below. An earlier
            # version ignored SIGTERM so that a grace period would show up as
            # elapsed time; that made the evidence a duration, and durations are
            # the runner's to decide.
            child = real(
                [command[0], "-c", "import time\nwhile True:\n    time.sleep(600)\n"],
                **kwargs,
            )
            sleepers.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", deaf)

        # Timed where it happens. The total cannot carry this: see the bound
        # below for what it costs and what it buys.
        stopping: list[float] = []
        stop = election_module._stop_child

        def timed(
            child: subprocess.Popen[bytes],
            *,
            windows_job: Any = None,
            assigned: bool = False,
        ) -> None:
            at = time.monotonic()
            try:
                stop(child, windows_job=windows_job, assigned=assigned)
            finally:
                stopping.append(time.monotonic() - at)

        monkeypatch.setattr(election_module, "_stop_child", timed)

        began = time.monotonic()
        attempt = election_module._start_owner(auth_root, profile, config, timeout=0.5)
        elapsed = time.monotonic() - began

        try:
            # Killed outright, and this is the assertion that carries the
            # test's weight. Stopping happens after the budget is spent, so a
            # SIGTERM grace period there is time added to a deadline the caller
            # was already promised: terminate-first turned this half-second
            # election into five and a half against a child that declined the
            # signal.
            #
            # Read off the signal rather than off a stopwatch, because the
            # stopwatch was measuring the machine. The call takes 0.55s here and
            # stayed within 20ms of that across every run of three full
            # CI-shaped suites, while a shared four-core runner under
            # `-n auto --cov` spent 4.1s on it and failed a 3s bound — against a
            # defect that costs 5.5s. A threshold does fit in that 1.4s gap;
            # what does not fit is any confidence that the gap is stable, since
            # the total also contains forking an interpreter and encoding a
            # 10 MiB configuration and neither is this code's to bound.
            #
            # The exit status has no such problem. `_stop_child` kills, so the
            # child dies of SIGKILL; anything that asks first kills it with
            # SIGTERM instead, whatever the grace period is set to, including
            # none at all. That last case is the one no duration could ever have
            # caught.
            #
            # It rests on the child above taking SIGTERM at its default
            # disposition. Give that child a handler again and this goes quiet
            # rather than red.
            #
            assert [child.returncode for child in sleepers] == [-signal.SIGKILL], (
                "the child that could not serve was asked to leave rather than "
                f"killed: {[child.returncode for child in sleepers]}"
            )

            # Delay that sends no signal is the other way to spend the caller's
            # deadline, and the exit status says nothing about it. A duration
            # still has to answer for that, so this one is taken around the stop
            # alone, where it is affordable. A kill and one wait cost 2 to 10ms
            # here, and 5.5ms was the worst of 200 samples taken against 48
            # busy processes on 24 cores, so 1.5s is a measured margin of about
            # 150 and not a limit anything guarantees: `Popen.wait` polls with
            # sleeps capped at 50ms and cannot bound what the scheduler or a
            # suspended VM does to the process holding it. It buys a wide gap,
            # not an impossibility.
            #
            # 1.5 rather than `_STOP_CHILD_SECONDS + something`, which was the
            # first attempt and was wrong: an expectation derived from the
            # constant under test moves with it, and raising that constant to 5
            # then let a five-second grace period through. A literal cannot do
            # that.
            #
            # Per call, not in total. Two stops of a few milliseconds each pass
            # this, which is harmless — the second kill finds a dead child —
            # but it is not the assertion that would notice them.
            assert stopping, "the child that could not serve was never stopped"
            assert max(stopping) < 1.5, (
                f"stopping the child took {max(stopping):.1f}s, and a kill it "
                f"cannot decline should cost nothing like that"
            )

            # What neither reaches is a stall in `_spawn` itself, anywhere
            # outside that one call: between the budget expiring and the stop,
            # or after it in the handshake release and the reap. No signal, and
            # outside the window above. Measured on both sides, 13s of it
            # passes. The total is the only net under that and it is a coarse
            # one, kept loose because a tight one is what flaked.
            assert elapsed < 15, f"the spawn took {elapsed:.1f}s of a 0.5s budget"
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
            # That argument is reasoned rather than measured on Windows, and
            # nothing checks it. The platform-behaviour job names five files and
            # this is not one of them, and the skip above says why it could not
            # be added as it stands: `_start_owner` takes the contending branch
            # there and answers CONTENDED where this expects FAILED. Asserted
            # anyway, because the argument is what the cleanup order rests on
            # and it should at least be written down where the order is.
            #
            # What it does not do is prove the *ordering*. Moving the release
            # ahead of the stop leaves this green on POSIX, because `detach()`
            # never blocks here whatever the child is doing. On Windows the same
            # change is the difference between an end of file and a wait on a
            # live reader — and nothing observes that distinction, here or in
            # CI. The paragraph above says why.
            #
            # Joined first, because nothing in production closes that
            # descriptor: the writer's own `with stream:` does, as it unwinds
            # from the broken pipe, so asserting the instant `_start_owner`
            # returns races that unwinding by construction. It has lost that
            # race twice on CI, both times on these two assertions and both
            # times green on a rerun. The rate is a property of the machine
            # rather than of the code: on a saturated laptop it failed 4 runs in
            # 10, and elsewhere it would not reproduce at all.
            #
            # So the property meant here is that the writer ends *promptly* once
            # the child is gone, which is what the join states and the instant
            # read only approximated. One second, and it masks nothing: both the
            # exit status and `elapsed` are already fixed by the time it runs.
            #
            # It buys nothing beyond that. A writer blocked on a full pipe
            # survives the handshake release and ends only when the child dies,
            # in either order, so the ordering above stays a Windows claim.
            for thread in threading.enumerate():
                if "daemon-config" in thread.name:
                    thread.join(timeout=1)
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

    @_POSIX_ONLY
    def test_a_child_that_reports_failure_does_not_keep_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # This is the missing third startup failure. The child has consumed its
        # configuration and reported that it cannot serve, but it stays alive.
        # Removing the stop attached to the terminal ``NO`` verdict leaves its
        # returncode unset and the lock probe below contended.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        children: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def reports_failure(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[1:4] != ["-P", "-m", "linkedin_mcp_server.daemon_owner"]:
                return real(command, **kwargs)
            child = real(
                [
                    command[0],
                    "-c",
                    "import json, sys, time\n"
                    "handover = json.loads(sys.stdin.readline())\n"
                    "nonce = handover['handshake_nonce']\n"
                    "sys.stdout.write(f'owner {nonce} failed\\n')\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(600)\n",
                ],
                **kwargs,
            )
            children.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", reports_failure)

        try:
            attempt = election_module._start_owner(
                auth_root, profile, config, timeout=5.0
            )
            assert attempt is _Attempt.FAILED
            assert [child.returncode for child in children] == [-signal.SIGKILL], (
                "the child was left alive after reporting startup failure"
            )

            probe = DaemonLock(auth_root)
            assert probe.try_acquire(), (
                "the failed child kept the daemon lock for this profile"
            )
            probe.release()
        finally:
            for child in children:
                if child.poll() is None:  # pragma: no cover - the stop worked
                    child.kill()
                    child.wait(timeout=30)

    @_POSIX_ONLY
    def test_a_failed_owner_leaves_no_grandchild_process_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.process_tree import process_group_exists

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        pid_file = tmp_path / "grandchild.pid"
        children: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def reports_failure(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[1:4] != ["-P", "-m", "linkedin_mcp_server.daemon_owner"]:
                return real(command, **kwargs)
            script = (
                "import json, pathlib, subprocess, sys, time\n"
                "handover = json.loads(sys.stdin.readline())\n"
                "nonce = handover['handshake_nonce']\n"
                "grandchild = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(600)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(grandchild.pid))\n"
                "sys.stdout.write(f'owner {nonce} failed\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(600)\n"
            )
            child = real([command[0], "-c", script], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", reports_failure)

        try:
            attempt = election_module._start_owner(
                auth_root, profile, config, timeout=5.0
            )
            assert attempt is _Attempt.FAILED
            assert children
            assert pid_file.exists(), "the child never reached its grandchild spawn"
            assert not process_group_exists(children[0].pid)
        finally:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=30)

    @_POSIX_ONLY
    def test_a_child_that_says_nothing_does_not_keep_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A child that has neither answered nor exited by the end of the budget
        # is stopped, and the reasoning took two passes to get right.
        #
        # The first version called this "still trying" and left the child alone,
        # on the grounds that it might yet come up. What the leniency actually
        # bought was a child holding the inherited lock while never serving —
        # measured, the lock was still held afterwards, and every later election
        # would contend against it forever. Publication is not atomic with the
        # private verdict; #790 tracks the opposite timeout race.
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
            #
            # Five, which is ten times the budget and catches only the 29s
            # cleanup defect above. It is not a check on `_spawn`'s
            # one-budget-across-both-halves rule, though it sits close enough to
            # read as one: measured, handing `_await_ready` a fresh 0.5s instead
            # of the remainder takes the call to 0.98s and passes this. Nothing
            # covers that rule; #780 is where it is written down.
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
import faulthandler
import json
import sys
from pathlib import Path

# A frontend that dies natively says nothing otherwise. Measured on a Windows
# runner at the declared floor: one of eight exited 0xC0000005 with an empty
# stderr, and the assertion could only report the number. This turns the next
# one into a stack that names where it happened.
faulthandler.enable()

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner
from linkedin_mcp_server.daemon_lock import DaemonLock
from linkedin_mcp_server.profile_claim import ensure_profile_claim

profile = Path(sys.argv[1])
auth_root = profile.parent
ensure_profile_claim(profile, claim_anyway=True)
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
from linkedin_mcp_server.profile_claim import ensure_profile_claim

profile = Path(sys.argv[1])
ensure_profile_claim(profile, claim_anyway=True)
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


def _windows_alive(pid: int) -> bool:
    """Query a Windows owner without signaling or terminating it."""
    win32api = importlib.import_module("win32api")
    win32con = importlib.import_module("win32con")
    win32process = importlib.import_module("win32process")
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
    except Exception:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == win32con.STILL_ACTIVE
    finally:
        handle.Close()


def _alive(pid: int) -> bool:
    """Whether a process still exists, without changing it."""
    if os.name == "nt":
        return _windows_alive(pid)
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


def _stop_windows(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )


def _stop(pid: object) -> None:
    """Kill an owner, taking the pid as it comes out of the child's JSON.

    Untyped on purpose: every caller has just read this out of a subprocess's
    output, and threading a cast through each of them would add noise to the
    cleanup path without making anything safer. A pid that is not an integer is
    nothing to kill.
    """
    if not isinstance(pid, int):
        return
    if os.name == "nt":
        _stop_windows(pid)
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(("exit_code", "expected"), [(259, True), (7, False)])
def test_windows_owner_liveness_is_a_query(
    exit_code: int, expected: bool, monkeypatch: pytest.MonkeyPatch
):
    class _Handle:
        closed = False

        def Close(self) -> None:
            self.closed = True

    handle = _Handle()

    class _Api:
        @staticmethod
        def OpenProcess(access: int, inherit: bool, pid: int) -> _Handle:
            assert (access, inherit, pid) == (0x1000, False, 4242)
            return handle

    class _Constants:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

    class _Process:
        @staticmethod
        def GetExitCodeProcess(opened: _Handle) -> int:
            assert opened is handle
            return exit_code

    modules = {
        "win32api": _Api,
        "win32con": _Constants,
        "win32process": _Process,
    }
    monkeypatch.setattr(importlib, "import_module", modules.__getitem__)

    assert _windows_alive(4242) is expected
    assert handle.closed


def test_windows_owner_cleanup_uses_taskkill(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    _stop_windows(4242)

    assert calls == [
        (
            ["taskkill", "/PID", "4242", "/T", "/F"],
            {"capture_output": True, "check": False},
        )
    ]


@pytest.mark.parametrize(
    ("startup_output", "authenticated", "expected"),
    [
        (b"committed\nfailed\n", "failed", _Started.NO),
        (b"failed\ncommitted\n", "committed", _Started.YES),
        (b"sitecustomize: committed", "committed", _Started.YES),
    ],
)
def test_owner_startup_verdicts_require_the_post_spawn_nonce(
    startup_output: bytes, authenticated: str, expected: _Started
):
    nonce = "0123456789abcdef" * 4
    frame = f"owner {nonce} {authenticated}\n".encode()
    child = SimpleNamespace(stdout=io.BytesIO(startup_output + frame))

    assert (
        election_module._await_committed(
            cast(Any, child), handshake_nonce=nonce, timeout=1
        )
        is expected
    )


def test_owner_rejects_unauthenticated_startup_verdicts_at_eof():
    nonce = "0123456789abcdef" * 4
    child = SimpleNamespace(stdout=io.BytesIO(b"committed\nfailed\n"))

    assert (
        election_module._await_committed(
            cast(Any, child), handshake_nonce=nonce, timeout=1
        )
        is _Started.NO
    )


def test_owner_accepts_a_verdict_after_large_finite_startup_output():
    nonce = "0123456789abcdef" * 4
    frame = f"owner {nonce} ready\n".encode()
    child = SimpleNamespace(stdout=io.BytesIO(b"x" * 5000 + frame))

    assert (
        election_module._await_committed(
            cast(Any, child),
            handshake_nonce=nonce,
            timeout=1,
        )
        is _Started.YES
    )


#: A bound on every wait below, so a regression fails an assertion instead of
#: hanging the suite.
_PARKED = 5.0

_NONCE = "0123456789abcdef" * 4
_COMMITTED = f"owner {_NONCE} committed\n".encode()


class _Pipe:
    """A handshake pipe that parks its read until the child writes.

    ``blocking_close`` is the Windows behaviour: a close waits for a read in
    flight instead of ending it, which is what POSIX does.
    """

    def __init__(self, payload: bytes = b"", *, blocking_close: bool = False) -> None:
        self._payload = io.BytesIO(payload)
        self._parked = not payload
        self._blocking_close = blocking_close
        self.reading = threading.Event()
        self.wrote = threading.Event()
        self.shut = threading.Event()
        self.closes = 0
        self.detaches = 0

    def readline(self, size: int = -1) -> bytes:
        self.reading.set()
        if self._parked:
            self.wrote.wait(_PARKED)
        return self._payload.readline(size)

    def detach(self) -> _Pipe:
        self.detaches += 1
        return self

    def close(self) -> None:
        if self._blocking_close and self._parked:
            self.wrote.wait(_PARKED)
        self.closes += 1
        self.wrote.set()
        self.shut.set()


def test_windows_release_leaves_a_parked_reader_holding_the_pipe(
    monkeypatch: pytest.MonkeyPatch,
):
    """A committed owner holds stdout open, so closing here would wait for it."""
    monkeypatch.setattr(election_module, "_IS_WINDOWS", True)
    pipe = _Pipe(blocking_close=True)
    child = SimpleNamespace(stdout=pipe)

    verdict = election_module._await_committed(
        cast(Any, child), handshake_nonce=_NONCE, timeout=0.05
    )

    assert verdict is _Started.STILL_TRYING
    assert pipe.reading.wait(_PARKED)

    started = time.monotonic()
    election_module._release_handshake(cast(Any, child))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert (pipe.detaches, pipe.closes) == (0, 0)
    assert child.stdout is None

    # The child stops writing, and the reader closes what it kept, exactly once.
    pipe.wrote.set()
    assert pipe.shut.wait(_PARKED)
    assert pipe.closes == 1


@pytest.mark.parametrize(
    ("windows", "make_pipe", "expected"),
    [
        (True, lambda: _Pipe(_COMMITTED, blocking_close=True), _Started.YES),
        (False, _Pipe, _Started.STILL_TRYING),
    ],
    ids=["windows-read-over", "posix-read-parked"],
)
def test_the_caller_closes_the_pipe_it_may_take(
    windows: bool,
    make_pipe: Callable[[], Any],
    expected: _Started,
    monkeypatch: pytest.MonkeyPatch,
):
    """Only Windows keeps a read in flight; every other case closes here, once."""
    monkeypatch.setattr(election_module, "_IS_WINDOWS", windows)
    pipe = make_pipe()
    child = SimpleNamespace(stdout=pipe)
    before = set(threading.enumerate())

    verdict = election_module._await_committed(
        cast(Any, child), handshake_nonce=_NONCE, timeout=0.05
    )
    readers = [thread for thread in threading.enumerate() if thread not in before]

    assert verdict is expected
    assert pipe.reading.wait(_PARKED)

    election_module._release_handshake(cast(Any, child))
    for reader in readers:
        reader.join(_PARKED)

    assert (pipe.detaches, pipe.closes) == (1, 1)


def test_a_read_that_ended_hands_its_pipe_back():
    """The window the release tests cannot reach: a read ending as the caller
    times out, where both sides walking away would leave the pipe open."""
    pipe = _Pipe()
    active = election_module._ActiveRead()

    active.finish(pipe)

    assert pipe.closes == 0
    assert active.abandon() is True


@_POSIX_ONLY
class TestSaturatedControlQueue:
    """The child's rendezvous, already full when the child goes looking for it.

    The listener is bound before the spawn, because its address travels in the
    configuration record. Anything running as this account can therefore queue on
    it before the child exists, and the queue holds sixteen. A child that finds it
    full does not fail: it blocks in connect for its own thirty second timeout, so
    it never reports the prepared generation the parent waited for before it would
    accept anything. The parent then killed a healthy owner for never attaching,
    on every attempt, for as long as the strangers sat there.

    Real sockets and a real protocol-3 exchange, because the defect is in the
    order two processes reach one kernel queue. The Windows path puts a Job Object
    in front of the same exchange, and its ordering is pinned by the doubles in
    ``TestWindowsAtomicOwnerHandoff``.
    """

    class _Child:
        """A protocol-3 owner, in a thread, doing what one does and in that order.

        It attaches before it prepares anything, which is the ordering the whole
        defect turns on: nothing it says can reach the parent until it is through
        the queue.
        """

        def __init__(self, instance_id: str) -> None:
            configuration_read, configuration_write = os.pipe()
            report_read, report_write = os.pipe()
            self.pid = 424242
            self.stdin = os.fdopen(configuration_write, "wb")
            self.stdout = os.fdopen(report_read, "rb")
            self.stderr = None
            self.returncode: int | None = None
            self.killed = False
            self.attached_at: float | None = None
            self.failure: BaseException | None = None
            self._instance_id = instance_id
            self._configuration = os.fdopen(configuration_read, "rb")
            self._report = os.fdopen(report_write, "wb")
            self._nonce = ""
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

        def _run(self) -> None:
            try:
                with self._configuration as stream:
                    record = json.loads(stream.readline())
                self._nonce = record["handshake_nonce"]
                channel = process_control.attach(
                    record["control_host"],
                    record["control_port"],
                    nonce=self._nonce,
                    timeout=20.0,
                )
                self.attached_at = time.monotonic()
                try:
                    self._say(f"prepared {self._instance_id}")
                    commit = f"owner {self._nonce} commit\n"
                    self._say("committed" if channel.readline() == commit else "failed")
                finally:
                    cast(Any, channel).close()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                self.failure = exc
            finally:
                with contextlib.suppress(OSError, ValueError):
                    self._report.close()

        def _say(self, message: str) -> None:
            self._report.write(f"owner {self._nonce} {message}\n".encode("ascii"))
            self._report.flush()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode if self.returncode is not None else 0

        def poll(self) -> int | None:
            return self.returncode

        def close(self) -> None:
            self.thread.join(timeout=30.0)
            # ``stdout`` is None once the parent has released the handshake pipe.
            for stream in (self.stdin, self.stdout, self._report):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        cast(Any, stream).close()

    def test_a_queue_saturated_before_the_child_still_reaches_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        instance_id = new_instance_id()
        child = self._Child(instance_id)
        silent: list[socket.socket] = []
        opened = election_module.ControlListener.open

        def open_and_saturate() -> election_module.ControlListener:
            # Every slot taken before the child exists, by peers that say
            # nothing: the cheapest way to occupy the rendezvous and the one the
            # per-peer allowance alone does not answer, because that bound
            # divides the wait among peers ahead of a connection that is already
            # queued.
            listener = opened()
            silent.extend(
                socket.create_connection((listener.host, listener.port))
                for _ in range(process_control._BACKLOG)
            )
            return listener

        monkeypatch.setattr(
            election_module.ControlListener, "open", staticmethod(open_and_saturate)
        )
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module,
            "terminate_process_group",
            lambda *a, **k: pytest.fail("the healthy owner was stopped"),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "validate_prepared",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor, "discard_prepared", lambda *a, **k: None
        )

        began = time.monotonic()
        try:
            outcome = election_module._spawn(
                profile.parent, config, lock_fd=None, timeout=8.0
            )
            elapsed = time.monotonic() - began
        finally:
            child.close()
            for peer in silent:
                peer.close()

        assert child.failure is None, child.failure
        assert outcome is election_module._Started.YES
        assert not child.killed, "a child that could not reach the queue was killed"
        # And it got there while the strangers were still standing in front of
        # it, rather than after its own connect timeout gave up on them.
        assert child.attached_at is not None
        assert child.attached_at - began < 8.0
        assert elapsed < 8.0, "the spawn spent its whole budget on the queue"


class TestWindowsExclusionNamespace:
    """No owner is started while the legacy exclusion object is still pending.

    Windows keeps daemon state under ``.mcp-server-linkedin-v2``, and every
    release before it kept it under ``.mcp-server-linkedin``. What stops those
    releases from ever creating that directory again is a *file* published at the
    same name, and ``prepare_daemon_state`` publishes it inside the inspector's
    reader thread.

    The frontend's one second read budget expiring does not stop that thread. It
    only stops the frontend from hearing about it, and the election went straight
    on to spawn. Under a package rolled back on disk that child is a predecessor,
    which creates the legacy directory for itself: v2 refuses to run beside it
    from then on, and the frontends that could report it are exactly the ones
    that no longer run.

    The child here is that predecessor, taking the name if it is free, and the
    inspection is blocked where the real one blocks.
    """

    @staticmethod
    def _legacy(home: Path) -> Path:
        return home / daemon_descriptor_module._LEGACY_WINDOWS_STATE_DIR

    def _predecessor(self, home: Path, taken: list[str]) -> Callable[..., _Attempt]:
        """A child from a rolled-back package, competing for the legacy name."""

        def start(*_args: object, **_kwargs: object) -> _Attempt:
            try:
                self._legacy(home).mkdir()
            except FileExistsError:
                taken.append("excluded")
            else:
                taken.append("took-the-name")
            return _Attempt.FAILED

        return start

    def test_no_owner_is_started_while_the_exclusion_is_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        home = daemon_descriptor_module._account_home()
        blocked = threading.Event()
        release = threading.Event()
        taken: list[str] = []
        inspections = 0

        def inspect(*_args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            blocked.set()
            release.wait()
            # What prepare_daemon_state does on Windows, at the moment it does
            # it: the tombstone lands, and only then is the name excluded.
            self._legacy(home).write_bytes(
                daemon_descriptor_module._LEGACY_WINDOWS_TOMBSTONE
            )
            daemon_descriptor_module._windows_exclusion_established = True
            return OwnerLookup(state=OwnerState.ABSENT)

        monkeypatch.setattr(election_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.05)
        monkeypatch.setattr(
            election_module, "_start_owner", self._predecessor(home, taken)
        )

        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _config(profile),
                deadline_seconds=0.4,
                connect=lambda attachment: Reach.REFUSED,
            )
            observed = list(taken)
            legacy_exists = self._legacy(home).exists()
        finally:
            release.set()

        assert blocked.is_set()
        assert observed == [], "an owner was started before the exclusion existed"
        assert not legacy_exists
        # Bounded all the same: the election spends its budget and falls back,
        # which is a slow start rather than an installation nothing can repair.
        assert not outcome.worth_connecting
        # And one reader for the whole of it. Asking again would put a second
        # process into the state storage the first is still inside.
        assert inspections == 1

    def test_an_owner_started_after_the_exclusion_cannot_take_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        home = daemon_descriptor_module._account_home()
        taken: list[str] = []

        def inspect(*_args: object) -> OwnerLookup:
            self._legacy(home).write_bytes(
                daemon_descriptor_module._LEGACY_WINDOWS_TOMBSTONE
            )
            daemon_descriptor_module._windows_exclusion_established = True
            return OwnerLookup(state=OwnerState.ABSENT)

        monkeypatch.setattr(election_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(
            election_module, "_start_owner", self._predecessor(home, taken)
        )

        obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=0.4,
            connect=lambda attachment: Reach.REFUSED,
        )

        # Startup proceeds the moment the inspection is safely done, and the
        # predecessor that used to win this race now finds the name occupied.
        assert taken, "the election never started an owner"
        assert set(taken) == {"excluded"}
        assert self._legacy(home).is_file()

    def _failing_election(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ) -> tuple[ElectionOutcome | None, BaseException | None, list[str], float]:
        """Run an election whose reader stops without excluding anything.

        Anything that escapes comes back rather than being raised, so a caller
        asserts on what the election *did* before it asserts on what it said.
        Under the guard nothing escapes and the election reports a fallback; a
        guard that treats a stopped reader as permission spawns first and then
        raises on the next read, and the spawn is the part that matters.
        """
        profile = _profile(tmp_path)
        home = daemon_descriptor_module._account_home()
        taken: list[str] = []

        def inspect(*_args: object) -> OwnerLookup:
            # After the read budget rather than before it, so the election
            # reaches the gate while this reader is still in flight and has to
            # decide on a reading it did not have when the budget expired.
            time.sleep(0.05)
            raise failure

        monkeypatch.setattr(election_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.01)
        monkeypatch.setattr(
            election_module, "_start_owner", self._predecessor(home, taken)
        )

        outcome: ElectionOutcome | None = None
        escaped: BaseException | None = None
        began = time.monotonic()
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _config(profile),
                deadline_seconds=30.0,
                connect=lambda attachment: Reach.REFUSED,
            )
        except Exception as exc:  # noqa: BLE001 - returned for the caller to judge
            escaped = exc
        return outcome, escaped, taken, time.monotonic() - began

    def test_a_transient_state_failure_starts_no_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The case a stopped reader must never be read as permission. An ACL that
        # answered once, a descriptor that ran out, a read that failed and would
        # succeed on the next attempt: the reader settles having established
        # nothing, and there is no legacy directory anywhere. A child started on
        # the strength of that stopping is a predecessor that creates the
        # directory itself, which is this guard's whole subject rather than an
        # outcome it may permit.
        home = daemon_descriptor_module._account_home()

        outcome, escaped, taken, elapsed = self._failing_election(
            tmp_path, monkeypatch, OSError("private state is temporarily unreadable")
        )

        assert taken == [], "a child was started against unestablished state"
        assert not self._legacy(home).exists(), (
            "a predecessor took the legacy name a transient failure left free"
        )
        assert escaped is None, escaped
        assert outcome is not None and not outcome.worth_connecting
        # And it says so at once rather than spending the deadline on a reader
        # whose answer cannot change; the next election reads the state afresh.
        assert elapsed < 5.0

    def test_a_permanent_conflict_starts_no_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The legacy directory already standing, which is the failure an operator
        # has to clear by hand. No child either: one would fail the same way, and
        # this election cannot tell this case from the transient one above, which
        # is exactly why neither may start anything.
        from linkedin_mcp_server.private_state import PrivateStateError

        home = daemon_descriptor_module._account_home()
        self._legacy(home).mkdir()

        outcome, escaped, taken, elapsed = self._failing_election(
            tmp_path,
            monkeypatch,
            PrivateStateError("Legacy Windows daemon state still exists"),
        )

        assert taken == []
        # Untouched, so the directory an operator is told to remove is the one
        # that was already there.
        assert self._legacy(home).is_dir()
        assert escaped is None, escaped
        assert outcome is not None and not outcome.worth_connecting
        assert elapsed < 5.0

    def test_the_reason_no_owner_could_start_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ):
        # The reader's own diagnosis, which is the only thing that names what to
        # fix. Dropped, the fallback is silent and reads as a machine that simply
        # never shares a browser.
        from linkedin_mcp_server.private_state import PrivateStateError

        with caplog.at_level(logging.WARNING):
            self._failing_election(
                tmp_path,
                monkeypatch,
                PrivateStateError("Legacy Windows daemon state still exists at C:/x"),
            )

        assert "will use its own browser" in caplog.text
        assert "Legacy Windows daemon state still exists at C:/x" in caplog.text


class TestWindowsAtomicOwnerHandoff:
    def test_owner_requests_breakaway_from_the_host_job(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(election_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(
            election_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x1, raising=False
        )
        monkeypatch.setattr(
            election_module.subprocess, "DETACHED_PROCESS", 0x2, raising=False
        )
        monkeypatch.setattr(
            election_module.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x4, raising=False
        )

        assert election_module._detachment_flags() == 0x7

    class _Stream:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _Child:
        def __init__(self, events: list[str]) -> None:
            self.pid = 4242
            self.stdin = TestWindowsAtomicOwnerHandoff._Stream()
            self.stdout = object()
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self._events = events

        def wait(self, timeout: float | None = None) -> int:
            self._events.append("wait-child")
            self.returncode = 1
            return self.returncode

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        committed: election_module._Started,
        validation_error: BaseException | None = None,
        settlement: election_module._Started | None = None,
    ) -> tuple[election_module._Started, list[str]]:
        profile = _profile(tmp_path)
        config = _config(profile)
        instance_id = new_instance_id()
        events: list[str] = []
        handshake_nonce = "0123456789abcdef" * 4
        child = self._Child(events)

        class _Job:
            name = "named-owner-job"

            def __init__(self) -> None:
                self.closed = False

            @classmethod
            def named(cls, purpose: str) -> _Job:
                assert purpose == "owner"
                events.append("create-job")
                return cls()

            def assign_popen(self, process: object) -> None:
                assert process is child
                assert not self.closed
                events.append("assign-gate")

            def terminate(self) -> None:
                assert not self.closed
                events.append("terminate-job")

            def release_popen_handle(self, process: object) -> None:
                assert process is child
                assert child.returncode is not None
                events.append("release-gate-handle")

            def wait_until_empty(self, *, timeout: float) -> None:
                assert not self.closed
                assert timeout <= 2
                events.append("drain-job")
                self.closed = True

            def close(self) -> None:
                if not self.closed:
                    events.append("close-parent-job")
                    self.closed = True

        def gate(target: list[str], nonce: str) -> list[str]:
            assert target[-2:] == ["--job-name", "named-owner-job"]
            assert target[-4:-2] == ["-m", "linkedin_mcp_server.daemon_owner"]
            assert nonce == "nonce"
            events.append("build-gate")
            return ["gate", nonce, "--", *target]

        def popen(command: list[str], **_kwargs: object) -> object:
            assert command[:3] == ["gate", "nonce", "--"]
            events.append("start-gate")
            return child

        def release(stream: object, nonce: str) -> None:
            assert stream is child.stdin
            assert nonce == "nonce"
            events.append("release-gate")

        def make_handshake_nonce() -> str:
            events.append("create-handshake-nonce")
            return handshake_nonce

        class _Control:
            """The rendezvous, stubbed so no real socket is bound here.

            Only the ordering is recorded. Where the drain and the attachment sit
            relative to the Job and to the child's own report is what this class
            is about; the channel's own behaviour is pinned in
            ``tests/test_process_control.py``.
            """

            host = "127.0.0.1"
            port = 4321

            @classmethod
            def open(cls) -> _Control:
                return cls()

            def start_accepting(self, *, nonce: str, timeout: float) -> None:
                assert nonce == handshake_nonce
                assert timeout == 1.0
                events.append("drain")

            def attached_within(self, *, timeout: float) -> None:
                assert timeout <= 1.0
                assert "drain" in events, "the attachment was taken without a drain"
                events.append("attach")

            def close(self) -> None:
                pass

        def hand_over(
            process: object,
            sent: AppConfig,
            *,
            handshake_nonce: str,
            control: object,
            timeout: float,
        ) -> None:
            assert process is child
            assert sent is config
            assert handshake_nonce == "0123456789abcdef" * 4
            assert isinstance(control, _Control)
            assert timeout == 1.0
            events.append("config")

        def prepared(process: object, *, handshake_nonce: str, timeout: float) -> str:
            assert process is child
            assert handshake_nonce == "0123456789abcdef" * 4
            assert timeout <= 1.0
            events.append("prepared")
            return instance_id

        def validate(
            root: Path,
            candidate: str,
            *,
            profile: Path,
            config: AppConfig,
            timeout: float,
        ) -> None:
            assert root == profile.parent
            assert candidate == instance_id
            assert config is not None
            assert timeout <= 1.0
            events.append("validate")
            if validation_error is not None:
                raise validation_error

        def send_commit(channel: object, *, handshake_nonce: str) -> None:
            assert isinstance(channel, _Control)
            assert handshake_nonce == "0123456789abcdef" * 4
            events.append("commit")

        def await_committed(
            process: object, *, handshake_nonce: str, timeout: float
        ) -> object:
            assert process is child
            assert handshake_nonce == "0123456789abcdef" * 4
            events.append(
                daemon_owner.COMMITTED
                if committed is election_module._Started.YES
                else committed.value
            )
            return committed

        monkeypatch.setattr(
            election_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        monkeypatch.setattr(election_module, "WindowsJob", _Job)
        monkeypatch.setattr(election_module, "ControlListener", _Control)
        monkeypatch.setattr(election_module, "release_nonce", lambda: "nonce")
        monkeypatch.setattr(election_module, "new_nonce", make_handshake_nonce)
        monkeypatch.setattr(election_module, "windows_gate_command", gate)
        monkeypatch.setattr(election_module.subprocess, "Popen", popen)
        monkeypatch.setattr(election_module, "release_windows_gate", release)
        monkeypatch.setattr(election_module, "_detachment_flags", lambda: 0)
        monkeypatch.setattr(election_module, "_hand_over_config", hand_over)
        monkeypatch.setattr(election_module, "_await_prepared", prepared)
        monkeypatch.setattr(
            election_module, "_resolve_profile_until", lambda path, timeout: profile
        )
        monkeypatch.setattr(election_module, "_validate_prepared_until", validate)
        monkeypatch.setattr(election_module, "_send_commit", send_commit)
        monkeypatch.setattr(election_module, "_await_committed", await_committed)
        monkeypatch.setattr(
            election_module, "_report_child_failure", lambda report: None
        )
        monkeypatch.setattr(election_module, "_release_handshake", lambda process: None)
        monkeypatch.setattr(election_module, "_reap", lambda process: None)
        monkeypatch.setattr(
            election_module,
            "_discard_prepared_until",
            lambda *args, **kwargs: events.append("discard"),
        )
        if settlement is not None:
            monkeypatch.setattr(
                election_module,
                "_settle_commit_result",
                lambda *args, **kwargs: (events.append("settle"), settlement)[1],
            )

        outcome = election_module._spawn(
            profile.parent, config, lock_fd=None, timeout=1.0
        )
        return outcome, events

    def test_success_closes_only_after_the_committed_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(
            tmp_path, monkeypatch, committed=election_module._Started.YES
        )

        assert outcome is election_module._Started.YES
        assert events == [
            "create-job",
            "build-gate",
            "start-gate",
            "create-handshake-nonce",
            # The drain begins on the nonce and before the configuration record,
            # so the queue is being emptied from before the child can reach the
            # port. It is parent-side work and changes nothing about the Job,
            # which is still assigned before the gate is released, so no owner
            # code exists until the parent owns the Job.
            "drain",
            "assign-gate",
            "release-gate",
            "config",
            "prepared",
            "attach",
            "validate",
            "commit",
            "committed",
            "close-parent-job",
        ]

    def test_pre_assignment_cleanup_stops_only_the_gate(self):
        events: list[str] = []

        class _Stream:
            def close(self) -> None:
                events.append("close-gate-stdin")

        class _Child:
            pid = 4242
            stdin = _Stream()
            waits = 0

            def wait(self, timeout: float) -> int:
                self.waits += 1
                events.append(f"wait-{self.waits}")
                if self.waits == 1:
                    raise subprocess.TimeoutExpired("gate", timeout)
                return 1

            def kill(self) -> None:
                events.append("kill-gate")

        class _Job:
            def close(self) -> None:
                events.append("close-job")

            def terminate(self) -> None:  # pragma: no cover - unassigned
                raise AssertionError("an unassigned Job was terminated")

        election_module._stop_child(
            cast(Any, _Child()), windows_job=cast(Any, _Job()), assigned=False
        )

        assert events == [
            "close-gate-stdin",
            "wait-1",
            "kill-gate",
            "close-job",
            "wait-2",
        ]

    @pytest.mark.parametrize(
        ("committed", "settlement", "tail"),
        [
            (election_module._Started.UNCERTAIN, None, ["uncertain"]),
            (
                election_module._Started.STILL_TRYING,
                election_module._Started.UNCERTAIN,
                ["still_trying", "settle"],
            ),
        ],
    )
    def test_both_uncertain_paths_close_without_termination(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        committed: election_module._Started,
        settlement: election_module._Started | None,
        tail: list[str],
    ):
        outcome, events = self._run(
            tmp_path,
            monkeypatch,
            committed=committed,
            settlement=settlement,
        )

        assert outcome is election_module._Started.UNCERTAIN
        assert events[-(len(tail) + 1) :] == [*tail, "close-parent-job"]
        assert "drain-job" not in events

    def test_post_assignment_cleanup_releases_gate_handle_before_drain(self):
        events: list[str] = []

        class _ProcessHandle:
            closed = False

            def Close(self) -> None:
                self.closed = True
                events.append("close-gate-handle")

        class _Child:
            pid = 4242
            stdin = None
            returncode: int | None = None
            _handle = _ProcessHandle()

            def wait(self, timeout: float) -> int:
                events.append("reap-gate")
                self.returncode = 1
                return 1

        child = _Child()

        class _Job:
            def terminate(self) -> None:
                events.append("terminate-job")

            def release_popen_handle(self, process: object) -> None:
                assert process is child
                assert child.returncode == 1
                child._handle.Close()

            def wait_until_empty(self, *, timeout: float) -> None:
                assert timeout <= 2
                assert child._handle.closed
                events.append("drain-job")

        election_module._stop_child(
            cast(Any, child), windows_job=cast(Any, _Job()), assigned=True
        )

        assert events == [
            "terminate-job",
            "reap-gate",
            "close-gate-handle",
            "drain-job",
        ]

    @pytest.mark.parametrize("failing", ["terminate", "wait", "release"])
    def test_a_failed_post_assignment_stop_still_closes_the_job(self, failing: str):
        """Kill-on-close is the fallback, and nothing else is left to reach it.

        The caller clears its own cleanup before calling this, so a Job left open
        here is one nobody closes until this process exits, and the tree the stop
        existed to end keeps the daemon lock for that whole time. Parametrised
        rather than written once because each failing call leaves the Job in a
        different state, and the close is the only thing all three share.
        """
        events: list[str] = []

        class _Child:
            pid = 4242
            stdin = None
            returncode: int | None = None

            def wait(self, timeout: float) -> int:
                if failing == "wait":
                    raise subprocess.TimeoutExpired("gate", timeout)
                events.append("reap-gate")
                self.returncode = 1
                return 1

        class _Job:
            def terminate(self) -> None:
                if failing == "terminate":
                    raise ProcessTreeError("Windows could not terminate the Job")
                events.append("terminate-job")

            def release_popen_handle(self, process: object) -> None:
                if failing == "release":
                    raise ProcessTreeError("Windows could not release the handle")
                events.append("release-gate-handle")

            def wait_until_empty(self, *, timeout: float) -> None:
                raise AssertionError("a Job that never stopped was asked to drain")

            def close(self) -> None:
                events.append("close-job")

        with pytest.raises(ProcessTreeError):
            election_module._stop_child(
                cast(Any, _Child()), windows_job=cast(Any, _Job()), assigned=True
            )

        assert events[-1] == "close-job"

    def test_precommit_failure_releases_gate_handle_before_job_drain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(
            tmp_path,
            monkeypatch,
            committed=election_module._Started.NO,
            validation_error=TimeoutError(),
        )

        assert outcome is election_module._Started.NO
        assert events[-5:] == [
            "terminate-job",
            "wait-child",
            "release-gate-handle",
            "drain-job",
            "discard",
        ]
        assert "commit" not in events

    def test_adoption_failure_verdict_drains_the_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(
            tmp_path, monkeypatch, committed=election_module._Started.ABORTED
        )

        assert outcome is election_module._Started.ABORTED
        assert events[-5:] == [
            "aborted",
            "terminate-job",
            "wait-child",
            "release-gate-handle",
            "drain-job",
        ]
        assert "close-parent-job" not in events


@_POSIX_ONLY
class TestPredecessorOwnerCompatibility:
    """A current frontend starting an owner from a package rolled back on disk.

    ``@latest`` is the documented install, so the frontend in memory and the
    package on disk are two different things whenever one of them moved while the
    other ran. A rollback makes the child older than its parent, and the
    predecessor's framing is the part that breaks: it reads its configuration to
    end of file, so a parent holding the pipe open for a commit record waits for
    a verdict from a child that is waiting for the parent to close.

    Driven through a real process rather than a stub of the protocol, because
    the deadlock is in the process boundary itself. ``tests/predecessor_owner.py``
    carries the predecessor's own read, quoted from the commit before the commit
    boundary existed.

    POSIX only, matching the neighbouring real-process tests: the Windows path
    puts a Job Object and the release gate in front of the same exchange, and
    neither is available to assert here.
    """

    def _run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, timeout: float
    ) -> tuple[object, list[subprocess.Popen[Any]], float]:
        profile = _profile(tmp_path)
        config = _config(profile)
        script = Path(__file__).with_name("predecessor_owner.py")
        started: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        monkeypatch.setattr(
            election_module,
            "_spawn_command",
            lambda **_kwargs: [sys.executable, str(script)],
        )

        def record(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            child = real(command, **kwargs)
            started.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", record)

        began = time.monotonic()
        outcome = election_module._spawn(
            profile.parent, config, lock_fd=None, timeout=timeout
        )
        return outcome, started, time.monotonic() - began

    def test_a_predecessor_owner_is_released_by_the_configuration_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, started, elapsed = self._run(tmp_path, monkeypatch, timeout=20.0)

        try:
            assert started, "no child was started"
            # Without the close, this child is still parked in its first read and
            # the parent has spent the whole budget waiting for a verdict.
            assert outcome is election_module._Started.YES, (
                "the predecessor owner never got past its configuration read"
            )
            assert elapsed < 10.0, (
                "the parent waited out its budget rather than releasing the child"
            )
            # And it is still running. A predecessor publishes on its own
            # authority before it reports, so this process may no longer stop it.
            assert started[0].poll() is None
            assert started[0].stdin is None or started[0].stdin.closed
        finally:
            for child in started:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=30)

    def test_the_predecessor_verdict_is_authenticated_like_any_other(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The same nonce check as every other startup record. A ``ready`` line
        # from interpreter startup output, written before the nonce existed,
        # cannot stand in for the one the owner sends.
        monkeypatch.setattr(election_module, "new_nonce", lambda: _HANDSHAKE_NONCE)
        frame = f"owner {_HANDSHAKE_NONCE} ready\n".encode()

        class _Child:
            pid = 424242
            stdin = io.BytesIO()
            stdout = io.BytesIO(b"ready\nowner deadbeef ready\n" + frame)
            stderr = io.BytesIO()
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

        child = _Child()
        assert (
            election_module._await_prepared(
                cast(Any, child), handshake_nonce=_HANDSHAKE_NONCE, timeout=5.0
            )
            is election_module._Started.YES
        )

        forged = _Child()
        forged.stdout = io.BytesIO(b"ready\nowner deadbeef ready\n")
        assert (
            election_module._await_prepared(
                cast(Any, forged), handshake_nonce=_HANDSHAKE_NONCE, timeout=1.0
            )
            is election_module._Started.NO
        )


class TestSpawnCleanupBoundary:
    """Everything after Popen returns is the caller's to clean up."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX child stop path")
    def test_a_diagnosis_that_cannot_start_stops_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A host out of threads must not be left holding a running owner.

        The bootstrap diagnosis starts one, and it is created after the spawn,
        so its failure is the first that can strand a child: nothing would send
        the configuration record it waits its whole handover timeout for.
        """
        profile = _profile(tmp_path)
        config = _config(profile)

        class _Child:
            pid = 515151
            stdin = io.BytesIO()
            stdout = io.BytesIO()
            stderr = io.BytesIO()
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                self.returncode = -9
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        child = _Child()
        stopped: list[int] = []
        listeners: list[process_control.ControlListener] = []
        opened = election_module.ControlListener.open

        def remember() -> election_module.ControlListener:
            listener = opened()
            listeners.append(listener)
            return listener

        def refuse(_stream: object) -> object:
            raise RuntimeError("can't start new thread")

        def stop(pid: int, **_kwargs: object) -> bool:
            stopped.append(pid)
            return True

        monkeypatch.setattr(
            election_module.ControlListener, "open", staticmethod(remember)
        )
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(election_module, "_BootstrapReport", refuse)
        monkeypatch.setattr(election_module, "terminate_process_group", stop)

        with pytest.raises(RuntimeError, match="start new thread"):
            election_module._spawn(profile.parent, config, lock_fd=None, timeout=5.0)

        assert stopped == [child.pid], "the child nobody could configure was stopped"
        assert child.returncode is not None, "and collected"
        assert len(listeners) == 1
        with pytest.raises(OSError):
            socket.create_connection(
                (listeners[0].host, listeners[0].port), timeout=1.0
            ).close()


class TestAtomicStartupCommit:
    @pytest.fixture(autouse=True)
    def _stop_fake_groups(self, monkeypatch: pytest.MonkeyPatch):
        def stop(*args: object, child: object | None = None, **kwargs: object) -> bool:
            assert child is not None
            cast(Any, child).kill()
            return True

        monkeypatch.setattr(election_module, "terminate_process_group", stop)
        monkeypatch.setattr(election_module, "new_nonce", lambda: _HANDSHAKE_NONCE)

    @pytest.fixture(autouse=True)
    def control_peers(
        self, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ):
        """Give each control channel the peer a real child would attach.

        The real listener rather than a stub, so these tests keep exercising the
        authentication they depend on. A connect completes against the backlog
        without an accept, so this needs no thread: the parent finds the peer
        waiting exactly as it does in production.

        Not for a case whose child is a real process. The drain takes the first
        peer that presents the nonce and closes the rendezvous behind it, exactly
        as the single accept did, so a stand-in planted alongside a real owner
        would win the attachment that owner is there to make.
        """
        if request.node.get_closest_marker("real_control_peer"):
            yield []
            return
        opened = election_module.ControlListener.open
        peers: list[process_control.ControlChannel] = []

        def open_and_attach() -> election_module.ControlListener:
            listener = opened()
            peers.append(
                process_control.attach(
                    listener.host,
                    listener.port,
                    nonce=_HANDSHAKE_NONCE,
                    timeout=5.0,
                )
            )
            return listener

        monkeypatch.setattr(
            election_module.ControlListener, "open", staticmethod(open_and_attach)
        )
        yield peers
        for peer in peers:
            cast(Any, peer).close()

    class _Input(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.written = b""

        def write(self, data: Any) -> int:
            self.written += bytes(data)
            return super().write(data)

    class _Child:
        def __init__(self, instance_id: str, final: str = "committed") -> None:
            self.pid = 424242
            #: Kept beside ``stdin`` because the handover takes the stream out
            #: of the process object, leaving this the only handle on it.
            self.input = TestAtomicStartupCommit._Input()
            self.stdin: io.BytesIO | None = self.input
            self.stdout = io.BytesIO(
                (
                    f"owner {_HANDSHAKE_NONCE} prepared {instance_id}\n"
                    f"owner {_HANDSHAKE_NONCE} {final}\n"
                ).encode()
            )
            self.returncode: int | None = None
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

    class _Job:
        """The parent's kill-on-close Windows Job Object, as a double.

        The required Windows CI job runs this whole class natively, where
        ``os.name`` is "nt" with nothing patched, so ``_spawn`` creates a Job
        and hands it the gate. A real one cannot serve these cases: ``_Child``
        is not a process and exposes no ``_handle``, so ``assign_popen``
        raises before a single startup assertion runs. Nor may one be created
        here, because a real Job would contain this interpreter's own
        descendants and terminate them.

        So the contract is modelled instead, in the order ``_spawn`` and
        ``_stop_child`` perform it: created by name, assigned the gate before
        the gate is released, terminated or closed once, its process handle
        released after the gate exits, and proved empty before it is done.
        Every step is checked, so a production reordering fails here rather
        than passing quietly.

        Termination kills the assigned child, which is what
        ``TerminateJobObject`` does to every member of the Job. That keeps the
        ``child.killed`` assertions meaning the same thing on both platforms:
        on POSIX the kill arrives through the ``terminate_process_group``
        stand-in, and on Windows through the containment that replaces it.
        """

        def __init__(self, name: str) -> None:
            self.name = name
            self.child: Any = None
            self.steps: list[str] = []
            self.closed = False
            self.drained = False
            #: What the gate had been told when it was assigned. Bytes, so this
            #: is a snapshot rather than a view of a stream that keeps growing.
            self.gate_input_at_assignment: bytes | None = None

        def assign_popen(self, child: Any) -> None:
            assert not self.closed, "a released Job was handed the owner gate"
            assert self.child is None, "the owner gate was assigned twice"
            self.child = child
            self.gate_input_at_assignment = getattr(child.stdin, "written", None)
            self.steps.append("assign")

        def terminate(self) -> None:
            assert not self.closed, "a released Job was asked to terminate"
            assert self.child is not None, "an unassigned Job was terminated"
            self.steps.append("terminate")
            self.child.kill()

        def release_popen_handle(self, child: Any) -> None:
            assert child is self.child, "a foreign process handle was released"
            assert child.returncode is not None, (
                "the gate handle was released before the gate exited"
            )
            self.steps.append("release-handle")

        def wait_until_empty(self, *, timeout: float) -> None:
            assert not self.closed, "a released Job was asked to prove it drained"
            assert self.steps[-1] == "release-handle", (
                "the Job was asked to prove it drained while the parent still "
                "held the gate's process handle"
            )
            assert timeout <= election_module._STOP_CHILD_SECONDS
            self.steps.append("drain")
            self.drained = True
            self.closed = True

        def close(self) -> None:
            if not self.closed:
                self.steps.append("close")
                self.closed = True

    @pytest.fixture(autouse=True)
    def owner_jobs(self, monkeypatch: pytest.MonkeyPatch) -> list[_Job]:
        """Inject the Job double every case needs on native Windows.

        Returns the Jobs ``_spawn`` created, which is empty on POSIX because
        nothing there takes that branch. A case that wants to observe the
        handoff takes this fixture and makes ``os.name`` answer "nt" itself.
        """
        created: list[TestAtomicStartupCommit._Job] = []

        class _Injected(TestAtomicStartupCommit._Job):
            @classmethod
            def named(cls, purpose: str) -> TestAtomicStartupCommit._Job:
                assert purpose == "owner"
                job = cls(f"Local\\linkedin-mcp-{purpose}-double")
                created.append(job)
                return job

        monkeypatch.setattr(election_module, "WindowsJob", _Injected)
        return created

    def _spawn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        validate: object,
        final: str = "committed",
        on_spawned: Callable[[], None] | None = None,
    ) -> tuple[object, _Child, list[str]]:
        profile = _profile(tmp_path)
        config = _config(profile)
        instance_id = new_instance_id()
        child = self._Child(instance_id, final)
        events: list[str] = []

        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_descriptor, "validate_prepared", validate
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "commit_prepared",
            lambda *a, **k: pytest.fail("only the child may commit"),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda *a, **k: events.append("discard"),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = election_module._spawn(
            profile.parent,
            config,
            lock_fd=None,
            timeout=1.0,
            on_spawned=on_spawned,
        )
        return outcome, child, events

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (
                daemon_owner.BOOTSTRAP_CONFIGURATION,
                "rejected its startup configuration",
            ),
            (daemon_owner.BOOTSTRAP_STATE, "could not resolve its profile state"),
            (daemon_owner.BOOTSTRAP_LOG, "daemon log could not be opened"),
            (daemon_owner.BOOTSTRAP_ATTACHED, "inspect the daemon log"),
        ],
    )
    def test_bootstrap_diagnostics_remain_actionable_and_fixed(
        self,
        code: str,
        message: str,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        secret = "proxy-password-must-not-cross"
        stream = io.BytesIO(
            (
                f"arbitrary child output {secret}\n"
                f"{daemon_owner.BOOTSTRAP_PREFIX} {code}\n"
            ).encode("ascii")
        )

        if code == daemon_owner.BOOTSTRAP_ATTACHED:
            monkeypatch.setattr(
                daemon_owner,
                "daemon_log_path",
                lambda root: pytest.fail("failure reporting touched daemon state"),
            )
        election_module._report_child_failure(election_module._BootstrapReport(stream))

        assert message in caplog.text
        assert secret not in caplog.text

    def test_a_blocked_bootstrap_pipe_cannot_pin_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The one bounded call here with somewhere else to fall back to.

        The others take a required timeout, so an implementation that ignores
        it has nothing to use instead and blocks. This one has a module
        constant behind it, ``_FAILURE_VERDICT_SECONDS``, and at its real 0.2s
        an implementation reading that instead of its argument returns well
        inside any ceiling a busy runner can also meet. So the constant is made
        expensive for the length of this test: reading it now costs thirty
        seconds. The signature default is bound at definition and cannot move,
        which is why the constant is the lever and not that.

        The timer covers the other shape, an unbounded wait, by closing the
        write end from outside. It sits above the ceiling, so it ends the test
        rather than rescuing the implementation.
        """
        monkeypatch.setattr(election_module, "_FAILURE_VERDICT_SECONDS", 30.0)
        reader_fd, writer_fd = os.pipe()
        closed = threading.Lock()
        opened = [writer_fd]

        def close_the_write_end() -> None:
            with closed:
                if opened:
                    os.close(opened.pop())

        stream = os.fdopen(reader_fd, "rb", buffering=0)
        report = election_module._BootstrapReport(stream)
        fallback = threading.Timer(_BOUNDED_CALL_SECONDS * 2, close_the_write_end)
        fallback.start()
        started = time.monotonic()
        try:
            assert report.read(timeout=0.01) is None
        finally:
            fallback.cancel()
            close_the_write_end()

        assert time.monotonic() - started < _BOUNDED_CALL_SECONDS

    def test_bootstrap_limit_keeps_draining_the_live_child(self):
        release = threading.Event()

        class _NoisyStream:
            def __init__(self) -> None:
                self.chunks = [b"x" * 512 for _ in range(8)]
                self.closed = False

            def readline(self, size: int = -1) -> bytes:
                if self.chunks:
                    return self.chunks.pop(0)
                release.wait()
                return b""

            def __enter__(self) -> _NoisyStream:
                return self

            def __exit__(self, *_args: object) -> None:
                self.closed = True

        stream = _NoisyStream()
        report = election_module._BootstrapReport(cast(Any, stream))
        try:
            for _ in range(500):
                if not stream.chunks:
                    break
                time.sleep(0.001)
            assert not stream.chunks
            assert report.read(timeout=0.01) is None
            assert not stream.closed
        finally:
            release.set()

    @pytest.mark.skipif(
        os.name == "nt", reason="the group this stops exists only on POSIX"
    )
    def test_production_stop_makes_a_real_unresponsive_child_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.process_tree import (
            terminate_process_group as stop_group,
        )

        monkeypatch.setattr(election_module, "terminate_process_group", stop_group)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            election_module._stop_child(child)

            assert child.poll() is not None, "the stopped child remained alive"
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)

    def test_broken_configuration_pipe_stops_the_precommit_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        child = self._Child(new_instance_id())
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = election_module._spawn(
            profile.parent, config, lock_fd=None, timeout=1.0
        )

        assert outcome is election_module._Started.NO
        assert child.killed

    @pytest.mark.parametrize("failure", [TimeoutError(), BrokenPipeError()])
    def test_configuration_handoff_failure_cleans_a_prepared_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Exception,
    ):
        profile = _profile(tmp_path)
        instance_id = new_instance_id()
        child = self._Child(instance_id)
        discarded: list[str] = []
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(failure),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda root, candidate: discarded.append(candidate),
        )

        outcome = election_module._spawn(
            profile.parent, _config(profile), lock_fd=None, timeout=1.0
        )

        assert outcome is election_module._Started.NO
        assert child.killed
        assert discarded == [instance_id]

    def test_configuration_failure_consumes_a_released_child_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        child = self._Child(new_instance_id())
        child.stdout = io.BytesIO(f"owner {_HANDSHAKE_NONCE} retry\n".encode())
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = election_module._spawn(
            profile.parent, _config(profile), lock_fd=None, timeout=1.0
        )

        assert outcome is election_module._Started.RETRY
        assert child.killed

    def test_interrupt_before_prepared_stops_the_lock_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        child = self._Child(new_instance_id())
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module,
            "_await_prepared",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        with pytest.raises(KeyboardInterrupt):
            election_module._spawn(profile.parent, config, lock_fd=None, timeout=1.0)

        assert child.killed
        # Closed by the writer thread that took it, and gone from ``Popen``
        # so nothing on the frontend's deadline path can close it a second
        # time behind that thread's back.
        assert child.stdin is None
        assert child.input.closed

    def test_validation_failure_stops_the_precommit_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def reject(*args: object, **kwargs: object) -> None:
            raise ValueError("bad pending state")

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=reject,
        )

        assert outcome is election_module._Started.ABORTED
        assert child.killed
        assert events == ["discard"]

    def test_prepared_read_timeout_stops_without_waiting_for_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            election_module,
            "_validate_prepared_until",
            lambda *a, **k: (_ for _ in ()).throw(TimeoutError()),
        )
        monkeypatch.setattr(
            election_module,
            "_await_committed",
            lambda *a, **k: pytest.fail("an unauthorized child cannot commit"),
        )

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
        )

        assert outcome is election_module._Started.NO
        assert child.killed
        assert b"commit\n" not in child.input.written
        assert events == ["discard"]

    def test_missing_prepared_state_consumes_the_child_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def missing(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError("prepared generation expired")

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=missing,
            final="retry",
        )

        assert outcome is election_module._Started.RETRY
        assert child.killed
        assert events == ["discard"]

    def test_only_the_lock_holding_child_commits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        control_peers: list[Any],
    ):
        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
        )

        assert outcome is election_module._Started.YES
        # On the control channel, and only there. The configuration pipe carries
        # its one record and ends, so an owner that frames by end of file is
        # released rather than left waiting behind an authorization it has never
        # heard of.
        assert control_peers[-1].readline() == f"owner {_HANDSHAKE_NONCE} commit\n"
        assert child.input.written.endswith(b"}\n")
        assert b"commit" not in child.input.written
        assert not child.killed
        assert events == []

    def test_parent_relinquishes_its_lock_copies_before_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        events: list[str] = []

        def validate(*args: object, **kwargs: object) -> None:
            assert events == ["released"]

        outcome, child, _ = self._spawn(
            tmp_path,
            monkeypatch,
            validate=validate,
            on_spawned=lambda: events.append("released"),
        )

        assert outcome is election_module._Started.YES
        assert not child.killed

    def test_uncertain_child_verdict_keeps_the_lock_holder_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "read",
            lambda root: pytest.fail("an explicit uncertain verdict needs no reread"),
        )
        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
            final="uncertain",
        )

        assert outcome is election_module._Started.UNCERTAIN
        assert not child.killed
        assert events == []

    def test_child_retry_verdict_releases_this_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
            final="retry",
        )

        assert outcome is election_module._Started.RETRY
        assert child.killed
        assert events == []

    def test_child_commit_failure_is_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
            final="aborted",
        )

        assert outcome is election_module._Started.ABORTED
        assert child.killed
        assert events == []

    def test_parent_delegates_lock_and_log_io_to_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        calls: list[tuple[Path, int | None]] = []

        def spawn(
            auth_root: Path,
            config: AppConfig,
            *,
            lock_fd: int | None,
            timeout: float,
            on_spawned: Callable[[], None] | None = None,
            inspector: object,
        ) -> object:
            calls.append((auth_root, lock_fd))
            return election_module._Started.YES

        monkeypatch.setattr(election_module, "_spawn", spawn)
        monkeypatch.setattr(
            election_module,
            "DaemonLock",
            lambda root: pytest.fail("the parent constructed the daemon lock"),
            raising=False,
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: pytest.fail("the parent derived the daemon log path"),
        )

        outcome = election_module._start_owner(
            profile.parent, profile, _config(profile), timeout=1.0
        )

        assert outcome is election_module._Attempt.STARTED
        assert calls == [(profile.parent, None)]

    @_POSIX_ONLY
    @pytest.mark.real_control_peer
    def test_planted_child_log_fifo_is_refused_without_blocking(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        profile = _profile(tmp_path)
        auth_root = profile.parent
        log_path = daemon_owner.daemon_log_path(auth_root)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(log_path)
        bootstrap = tmp_path / "blocked_log_owner.py"
        marker = tmp_path / "opening-log"
        home = daemon_descriptor_module._account_home()
        bootstrap.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from linkedin_mcp_server import daemon_descriptor, daemon_owner\n"
            "daemon_descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "attach = daemon_owner._attach_daemon_log\n"
            "def marked(root):\n"
            "    Path(sys.argv[2]).write_text('opening')\n"
            "    return attach(root)\n"
            "daemon_owner._attach_daemon_log = marked\n"
            "raise SystemExit(daemon_owner.main([]))\n"
        )
        children: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def capture(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[-2:] == ["-m", "linkedin_mcp_server.daemon_owner"]:
                command = [command[0], str(bootstrap), str(home), str(marker)]
            child = real(command, **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", capture)
        try:
            outcome = election_module._start_owner(
                auth_root, profile, _config(profile), timeout=2.0
            )

            assert outcome is election_module._Attempt.ABORTED
            assert marker.read_text() == "opening"
            assert all(child.returncode is not None for child in children)
            assert log_path.is_fifo(), "the child replaced the planted log entry"
            assert "daemon log could not be opened" in caplog.text
            assert "diagnostic log became available" not in caplog.text
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=30)
            log_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                log_path.parent.rmdir()

    def test_retryable_windows_winner_continues_the_election(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            election_module,
            "_spawn",
            lambda *a, **k: election_module._Started.RETRY,
        )

        outcome = election_module._start_contending_for_the_lock(
            tmp_path, _config(_profile(tmp_path)), timeout=1.0
        )

        assert outcome is election_module._Attempt.CONTENDED

    def test_aborted_windows_winner_is_a_terminal_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        monkeypatch.setattr(
            election_module,
            "_spawn",
            lambda *a, **k: election_module._Started.ABORTED,
        )

        assert (
            election_module._start_contending_for_the_lock(
                profile.parent, config, timeout=1.0
            )
            is election_module._Attempt.ABORTED
        )

    def test_aborted_owner_start_does_not_retry_for_the_election_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        starts: list[bool] = []
        monkeypatch.setattr(
            election_module,
            "_start_owner",
            lambda *args, **kwargs: (
                starts.append(True),
                election_module._Attempt.ABORTED,
            )[1],
        )
        monkeypatch.setattr(
            election_module,
            "_live_lookup",
            lambda *args, **kwargs: (OwnerLookup(OwnerState.ABSENT), False),
        )

        outcome = obtain_owner(profile.parent, profile, config, deadline_seconds=90.0)

        assert not outcome.worth_connecting
        assert starts == [True]

    def test_commit_request_failure_leaves_a_live_child_to_settle_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            election_module,
            "_send_commit",
            lambda *a, **k: (_ for _ in ()).throw(OSError("broken control pipe")),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor, "read", lambda root: None
        )

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
        )

        assert outcome is election_module._Started.UNCERTAIN
        assert not child.killed
        # Closed by the writer thread that took it, and gone from ``Popen``
        # so nothing on the frontend's deadline path can close it a second
        # time behind that thread's back.
        assert child.stdin is None
        assert child.input.closed
        assert events == []

    def test_interrupt_after_commit_request_does_not_kill_the_child(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        control_peers: list[Any],
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        child = self._Child(new_instance_id())
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "validate_prepared",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            election_module,
            "_await_committed",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        with pytest.raises(KeyboardInterrupt):
            election_module._spawn(profile.parent, config, lock_fd=None, timeout=1.0)

        assert control_peers[-1].readline() == f"owner {_HANDSHAKE_NONCE} commit\n"
        assert not child.killed
        # Closed by the writer thread that took it, and gone from ``Popen``
        # so nothing on the frontend's deadline path can close it a second
        # time behind that thread's back.
        assert child.stdin is None
        assert child.input.closed

    def test_missing_commit_verdict_settles_from_canonical_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        instance_id = new_instance_id()
        child = self._Child(instance_id, final="")
        canonical = type("Canonical", (), {"instance_id": instance_id})()
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "validate_prepared",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor, "read", lambda root: canonical
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = election_module._spawn(
            profile.parent, config, lock_fd=None, timeout=1.0
        )

        assert outcome is election_module._Started.YES
        assert not child.killed

    def test_dead_child_with_stale_canonical_state_stays_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        instance_id = new_instance_id()
        child = self._Child(instance_id, final="")
        child.returncode = 1
        monkeypatch.setattr(
            election_module.daemon_descriptor, "read", lambda root: None
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda *a, **k: pytest.fail("a potentially committed token was deleted"),
        )

        outcome = election_module._settle_commit_result(
            tmp_path, instance_id, cast(Any, child), timeout=1.0
        )

        assert outcome is election_module._Started.UNCERTAIN

    def test_unreadable_canonical_state_keeps_a_dead_generation_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        instance_id = new_instance_id()
        child = self._Child(instance_id, final="")
        child.returncode = 1
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "read",
            lambda root: (_ for _ in ()).throw(
                PermissionError("temporarily unreadable")
            ),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda *a, **k: pytest.fail("uncertain state was deleted"),
        )

        outcome = election_module._settle_commit_result(
            tmp_path, instance_id, cast(Any, child), timeout=1.0
        )

        assert outcome is election_module._Started.UNCERTAIN

    def test_canonical_settlement_read_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        blocked = threading.Event()
        release = threading.Event()

        def read(root: Path) -> None:
            blocked.set()
            release.wait()
            return None

        monkeypatch.setattr(election_module.daemon_descriptor, "read", read)
        # So a bound that never fires fails this test instead of hanging the
        # run: the release below is only reached once the call returns, which
        # is exactly what an unbounded one never does. Above the ceiling, so it
        # cannot rescue the implementation it is there to catch.
        fallback = threading.Timer(_BOUNDED_CALL_SECONDS * 2, release.set)
        fallback.start()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="could not be read in time"):
                election_module._read_canonical_until(tmp_path, timeout=0.01)
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert time.monotonic() - started < _BOUNDED_CALL_SECONDS

    def test_prepared_state_validation_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        blocked = threading.Event()
        release = threading.Event()

        def validate(*args: object, **kwargs: object) -> None:
            blocked.set()
            release.wait()

        monkeypatch.setattr(
            election_module.daemon_descriptor, "validate_prepared", validate
        )
        # For the reason the read above has one: without it an unbounded call
        # never returns, the release never runs, and the failure arrives as a
        # hung suite rather than as a failing test.
        fallback = threading.Timer(_BOUNDED_CALL_SECONDS * 2, release.set)
        fallback.start()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="could not be read in time"):
                election_module._validate_prepared_until(
                    tmp_path,
                    new_instance_id(),
                    profile=_profile(tmp_path),
                    config=_config(_profile(tmp_path)),
                    timeout=0.01,
                )
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert time.monotonic() - started < _BOUNDED_CALL_SECONDS

    def test_profile_resolution_is_bounded(self, tmp_path: Path):
        blocked = threading.Event()
        release = threading.Event()

        class _BlockedProfile:
            def expanduser(self) -> _BlockedProfile:
                return self

            def resolve(self) -> Path:
                blocked.set()
                release.wait()
                return tmp_path / "resolved-profile"

        # Above the ceiling asserted below on purpose: it exists only so a bound
        # that never fires cannot hang the run, and releasing the block sooner
        # would let an unbounded call return inside the ceiling and pass.
        fallback = threading.Timer(_BOUNDED_CALL_SECONDS * 2, release.set)
        fallback.start()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="could not be resolved in time"):
                election_module._resolve_profile_until(
                    cast(Path, _BlockedProfile()), timeout=0.01
                )
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert time.monotonic() - started < _BOUNDED_CALL_SECONDS

    def test_late_prepared_cleanup_cannot_touch_a_new_generation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        auth_root = _profile(tmp_path).parent
        abandoned = new_instance_id()
        replacement = new_instance_id()
        abandoned_paths = (
            daemon_descriptor_module.pending_descriptor_path(auth_root, abandoned),
            daemon_descriptor_module.token_path(auth_root, abandoned),
        )
        replacement_paths = (
            daemon_descriptor_module.pending_descriptor_path(auth_root, replacement),
            daemon_descriptor_module.token_path(auth_root, replacement),
        )
        # Made the way production makes it, not with a bare mkdir: the
        # application namespace above this is state prepare_daemon_state
        # creates and then verifies as its own, and a planted one is foreign
        # content to that check.
        daemon_descriptor_module.prepare_daemon_state(auth_root)
        for path in abandoned_paths:
            path.write_text("abandoned")

        blocked = threading.Event()
        release = threading.Event()
        discard = daemon_descriptor_module.discard_prepared

        def delayed(root: Path, instance_id: str) -> None:
            blocked.set()
            release.wait()
            discard(root, instance_id)

        monkeypatch.setattr(
            election_module.daemon_descriptor, "discard_prepared", delayed
        )
        # Above the ceiling asserted below on purpose: it exists only so a bound
        # that never fires cannot hang the run, and releasing the block sooner
        # would let an unbounded call return inside the ceiling and pass.
        fallback = threading.Timer(_BOUNDED_CALL_SECONDS * 2, release.set)
        fallback.start()
        started = time.monotonic()
        bounded_elapsed = float("inf")
        try:
            with pytest.raises(TimeoutError, match="could not be removed in time"):
                election_module._discard_prepared_until(
                    auth_root, abandoned, timeout=0.01
                )
            bounded_elapsed = time.monotonic() - started
            for path in replacement_paths:
                path.write_text("replacement")
            release.set()
            for _ in range(100):
                if not any(path.exists() for path in abandoned_paths):
                    break
                time.sleep(0.01)
        finally:
            release.set()
            fallback.cancel()

        assert blocked.is_set()
        assert bounded_elapsed < 0.1
        assert not any(path.exists() for path in abandoned_paths)
        assert all(path.read_text() == "replacement" for path in replacement_paths)

    def test_the_configuration_pipe_ends_on_its_record(self, tmp_path: Path):
        # The rollback fix, at its narrowest. Every owner older than the commit
        # boundary frames its configuration by reading to end of file, so a pipe
        # held open past the record leaves such a child blocked in its first
        # read while this process waits for a verdict it will never send.
        class _Flushed(io.BytesIO):
            flushes = 0
            content = b""

            def flush(self) -> None:
                self.flushes += 1
                super().flush()

            def close(self) -> None:
                self.content = self.getvalue()
                super().close()

        stream = _Flushed()
        child = self._Child(new_instance_id())
        child.stdin = stream
        control = election_module.ControlListener.open()

        try:
            election_module._hand_over_config(
                cast(Any, child),
                _config(_profile(tmp_path)),
                handshake_nonce=_HANDSHAKE_NONCE,
                control=control,
                timeout=1.0,
            )
        finally:
            control.close()

        assert stream.flushes == 1
        assert stream.closed, "the configuration pipe was left open past its record"
        # Flushed before the close rather than by it, so a child reading the
        # record and a child reading to end of file both see the same bytes.
        assert stream.content.endswith(b"\n")
        # And the rendezvous the commit record will arrive on travelled with it.
        record = json.loads(stream.content)
        assert record["startup_protocol"] == daemon_config.STARTUP_PROTOCOL_VERSION
        assert (record["control_host"], record["control_port"]) == (
            control.host,
            control.port,
        )

    class _StalledPipe:
        """A configuration pipe whose ``close`` needs the lock its ``write`` holds.

        What ``BufferedWriter`` does against a child that never reads, without a
        real pipe or a platform's buffer size: the write parks holding the
        buffer's own lock, and every other operation on that object waits for it
        to finish. ``_close_handshake_stream`` documents the measured read-side
        twin, 29.27 of thirty seconds.
        """

        def __init__(self) -> None:
            self._lock = threading.Lock()
            #: Set by the test to stand for the read end going away, which is
            #: the only thing that ends a parked write.
            self.read_end_died = threading.Event()
            self.writing = threading.Event()
            #: Every closer, by thread name, so both halves are observable: how
            #: many closes there were and which thread made them.
            self.closed_by: list[str] = []

        def write(self, data: bytes) -> int:
            with self._lock:
                self.writing.set()
                assert self.read_end_died.wait(timeout=30), (
                    "the parked write was never released"
                )
                raise BrokenPipeError("the read end is gone")

        def flush(self) -> None:  # pragma: no cover - the write never returns
            with self._lock:
                pass

        def close(self) -> None:
            with self._lock:
                self.closed_by.append(threading.current_thread().name)

    class _StalledChild:
        """A child that stalls before its first read and survives the hard stop."""

        def __init__(self, stdin: object) -> None:
            self.pid = 424242
            self.stdin: object | None = stdin
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.kills = 0

        def kill(self) -> None:
            # Signalled, and still alive: a process stuck in the kernel is
            # collected whenever the kernel is done with it, not when asked.
            self.kills += 1

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("owner", timeout or 0.0)

        def poll(self) -> int | None:
            return self.returncode

    def test_a_stalled_configuration_write_never_pins_the_frontend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The deadlock this ownership transfer exists to prevent. The child
        # stalls before reading, so the writer thread parks in `write` holding
        # the lock a `close` would need; the hard stop is bounded and the child
        # outlives it, so nothing breaks that write. A `child.stdin.close()`
        # from `_spawn`'s cleanup or from `_stop_child` would then wait on the
        # writer with no deadline of its own, and the frontend would never
        # return at all.
        #
        # Every wait here is bounded, so the mutation of leaving `child.stdin`
        # attached fails this case rather than hanging the suite.
        monkeypatch.setattr(
            election_module, "os", SimpleNamespace(name="posix", environ=os.environ)
        )
        profile = _profile(tmp_path)
        pipe = self._StalledPipe()
        child = self._StalledChild(pipe)
        monkeypatch.setattr(election_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        finished: list[object] = []

        def run() -> None:
            try:
                finished.append(
                    election_module._spawn(
                        profile.parent, _config(profile), lock_fd=None, timeout=0.5
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - reported by the assertions
                finished.append(exc)

        spawner = threading.Thread(target=run, name="spawn-under-test")
        spawner.start()
        try:
            assert pipe.writing.wait(timeout=10), "the writer never reached its write"
            spawner.join(timeout=20)
            assert not spawner.is_alive(), (
                "the frontend was pinned by a configuration write that never returned"
            )
            assert finished == [election_module._Started.NO]
            # The stop ran, was bounded, and left the child alive: exactly the
            # state in which nothing may close that stream from here.
            assert child.kills == 1
            assert child.poll() is None
            # Handed to the writer, so neither `_stop_child` nor the cleanup in
            # `_spawn` can reach it.
            assert child.stdin is None
            assert pipe.closed_by == [], (
                "the configuration pipe was closed while the write still held it"
            )
        finally:
            pipe.read_end_died.set()

        # And the writer closes it once the read end is gone, from its own
        # thread and exactly once.
        for _ in range(1000):
            if pipe.closed_by:
                break
            time.sleep(0.01)
        for thread in threading.enumerate():
            if thread.name == "daemon-config":
                thread.join(timeout=5)
        assert pipe.closed_by == ["daemon-config"]

    def test_a_generic_case_is_given_a_job_before_the_owner_starts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        owner_jobs: list[_Job],
    ):
        # What the required Windows CI job does to every case in this class:
        # there `os.name` is "nt" already, so `_spawn` creates a Job, names it
        # in the gate's command line and assigns the gate to it before letting
        # any owner code run. This case takes that branch on any platform while
        # asking for nothing beyond the defaults, so a double dropped from the
        # fixture fails here rather than only on Windows, where it would fail
        # seventeen cases at once.
        monkeypatch.setattr(
            election_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        commands: list[list[str]] = []
        build = election_module._spawn_command
        monkeypatch.setattr(
            election_module,
            "_spawn_command",
            lambda **kwargs: commands.append(build(**kwargs)) or commands[-1],
        )

        outcome, child, events = self._spawn(
            tmp_path, monkeypatch, validate=lambda *a, **k: None
        )

        assert outcome is election_module._Started.YES
        assert events == []
        assert len(owner_jobs) == 1
        job = owner_jobs[0]
        assert job.child is child, "the gate was never assigned to the owner Job"
        # The name the parent created travels to the child, which is the only
        # way the owner can find the Job it has to adopt.
        assert commands[-1][-2:] == ["--job-name", job.name]
        # Assignment comes first and the gate is still waiting, so no owner code
        # exists until the parent owns the Job.
        assert job.steps[0] == "assign"
        assert job.gate_input_at_assignment == b"", (
            "the gate was released before the parent owned its Job"
        )
        assert child.input.written.startswith(b"release ")
        # A committed owner is left running, so the parent lets its handle go
        # without terminating or draining anything.
        assert job.steps == ["assign", "close"]
        assert not child.killed

    def test_a_generic_case_never_touches_a_real_job_object(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        owner_jobs: list[_Job],
    ):
        # The same branch on the fixture's own defaults. The genuine
        # `WindowsJob` cannot serve this class at all: `_Child` is not a process
        # and exposes no `_handle`, so the real `assign_popen` refuses it before
        # any startup assertion runs. Asserted against the production method
        # rather than a story about it, and without a Job Object: the handle
        # lookup fails on the argument, so this never reaches `self`. The real
        # Job path is covered separately in `tests/test_process_tree.py`.
        from linkedin_mcp_server import process_tree as process_tree_module

        monkeypatch.setattr(
            election_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )

        outcome, child, _ = self._spawn(
            tmp_path, monkeypatch, validate=lambda *a, **k: None
        )

        assert outcome is election_module._Started.YES
        assert len(owner_jobs) == 1
        assert not isinstance(owner_jobs[0], process_tree_module.WindowsJob)
        with pytest.raises(
            process_tree_module.ProcessTreeError, match="no process handle"
        ):
            process_tree_module.WindowsJob.assign_popen(
                cast(Any, None), cast(Any, child)
            )


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
        # lock alive after the owner died. Every recovery afterwards would
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
        # the code released it or not. Mutation verifies this: with the release
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
    def test_a_child_that_dies_after_taking_the_lock_leaves_it_free(
        self, real_state_root: Path, tmp_path: Path
    ):
        child = tmp_path / "lock_taking_child.py"
        marker = tmp_path / "locked"
        child.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from linkedin_mcp_server.daemon_lock import DaemonLock\n"
            "sys.stdin.readline()\n"
            "lock = DaemonLock(Path(sys.argv[1]))\n"
            "assert lock.try_acquire()\n"
            "Path(sys.argv[2]).write_text('locked')\n"
            "sys.stdout.write('failed\\n')\n"
            "sys.stdout.flush()\n"
            "raise SystemExit(7)\n"
        )

        profile = real_state_root
        auth_root = profile.parent
        frontend = (
            "import sys\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon_election as election\n"
            "real = election.subprocess.Popen\n"
            "def substitute(command, **kwargs):\n"
            "    if command[-2:] != ['-m', 'linkedin_mcp_server.daemon_owner']:\n"
            "        return real(command, **kwargs)\n"
            "    return real([command[0], sys.argv[2], sys.argv[3], sys.argv[4]], **kwargs)\n"
            "election.subprocess.Popen = substitute\n"
            "from linkedin_mcp_server.config.schema import AppConfig\n"
            "profile = Path(sys.argv[1])\n"
            "config = AppConfig()\n"
            "config.browser.user_data_dir = str(profile)\n"
            "election.obtain_owner(profile.parent, profile, config, deadline_seconds=15)\n"
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
        assert marker.exists(), "the child never took the lock"

        probe = DaemonLock(auth_root)
        assert probe.try_acquire(), "the dead child left the daemon lock held"
        probe.release()

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
        owners: set[object] = set()
        try:
            for frontend in running:
                out, err = frontend.communicate(timeout=300)
                assert frontend.returncode == 0, err[-2000:]
                result = json.loads(out.strip().splitlines()[-1])
                results.append(result)
                owners.add(result["pid"])

            assert None not in owners, "a client ended up with no owner"
            assert len(owners) == 1, f"more than one owner was elected: {owners}"
            # Exactly one of them did the starting; the rest attached to it.
            assert sum(1 for r in results if r["started"]) == 1, results
            assert all(r["state"] == OwnerState.ATTACHABLE.value for r in results)
            # And none of them kept the lock they may have taken on the way.
            assert not any(r["frontend_holds_lock"] for r in results)
        finally:
            for frontend in running:
                if frontend.poll() is None:
                    frontend.kill()
                    frontend.wait(timeout=30)
            for pid in owners:
                _stop(pid)
            with contextlib.suppress(Exception):
                published = daemon_descriptor_module.read(profile.parent)
                if published is not None:
                    _stop(published.pid)

    @_POSIX_ONLY
    def test_the_owner_outlives_the_client_that_started_it(self, real_state_root: Path):
        # The premise of the whole feature. An owner that died with its first
        # client would give every later client a cold start plus a fresh
        # ``/feed/`` validation, which is the traffic this exists to remove.
        #
        # Killed by process *group*, not by pid, because that is how a client
        # shutdown reaches a server it spawned, while a child that merely had its
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
        # is what happened here twice while this was being built: the owner
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

    def test_current_and_predecessor_tokens_stay_on_disk(self, real_state_root: Path):
        # Cleanup runs before replacement, so the currently canonical token must
        # remain attachable if this startup fails. A successful replacement leaves
        # that predecessor beside the new generation and removes anything older.
        profile = real_state_root
        auth_root = profile.parent

        first = _run_frontend(profile)
        predecessor = daemon_descriptor_module.read(auth_root)
        assert predecessor is not None
        _stop(first["pid"])
        for _ in range(300):
            if not daemon_is_running(auth_root):
                break
            time.sleep(0.01)
        directory = daemon_descriptor_module.daemon_dir(auth_root)
        abandoned_id = new_instance_id()
        abandoned = daemon_descriptor_module.pending_descriptor_path(
            auth_root, abandoned_id
        )
        abandoned.write_text("abandoned")
        daemon_descriptor_module.token_path(auth_root, abandoned_id).write_text("old")
        second = _run_frontend(profile)

        try:
            current = daemon_descriptor_module.read(auth_root)
            assert current is not None
            tokens = sorted(p.name for p in directory.glob("token-*"))
            pending = sorted(p.name for p in directory.glob("pending-*"))
            assert tokens == sorted(
                [
                    f"token-{predecessor.instance_id}",
                    f"token-{current.instance_id}",
                ]
            )
            assert pending == []
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
                proxy_backend=_proxy_backend_for(lookup.attachment),
            )

            async def served() -> set[str]:
                async with Client(proxy) as client:
                    return {tool.name for tool in await client.list_tools()}

            names = asyncio.run(served())

            # The owner registers the full local set, so the proxy must show it
            # all, including `close_session`, the one defined inline rather than
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
                proxy_backend=_proxy_backend_for(lookup.attachment),
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

            # Retried until the owner has something to say about *auth*.
            #
            # A real owner answers whichever gate it reaches first, and browser
            # setup runs in the background: arrive before it finishes and the
            # answer is "still installing", which carries no auth marker and is
            # a correct answer to a different question. Measured at roughly one
            # run in eight, which is frequent enough to cost CI runs and rare
            # enough to look like something else.
            #
            # A client does exactly this, so the retry is not a workaround so
            # much as the shape of the thing being tested.
            deadline = time.monotonic() + 60
            answered = asyncio.run(call_it())
            while (answered.meta or {}).get(
                MARKER_KEY
            ) is None and time.monotonic() < deadline:
                time.sleep(0.5)
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
                tool_timeout=30.0,
                role=ServerRole.PROXY,
                proxy_backend=_proxy_backend_for(wrong),
            )

            async def served() -> None:
                async with Client(proxy) as client:
                    await client.list_tools()

            # Matched on the status rather than catching anything at all. A bare
            # `Exception` passed on any failure whatsoever, including one raised
            # before a request ever left this process, so the test would have
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
        # middleware. Measured on 3.4.4, an unauthenticated POST to a custom
        # route is served, so it checks the token itself. Without that, any
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
        # 127.0.0.1 unless NO_PROXY happens to say otherwise. This was reproduced
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

    @pytest.mark.parametrize(
        ("parent_flags", "user_site_started"),
        [(["-I"], False), (["-s"], False), ([], True)],
    )
    def test_the_owner_follows_the_parent_user_site_mode(
        self, tmp_path: Path, parent_flags: list[str], user_site_started: bool
    ):
        """Run the real command from interpreters with explicit startup modes."""
        import json

        base_executable = getattr(sys, "_base_executable", sys.executable)
        environment = os.environ.copy()
        environment.pop("PYTHONNOUSERSITE", None)
        environment["PYTHONUSERBASE"] = str(tmp_path / "user-base")

        user_site_result = subprocess.run(
            [
                base_executable,
                "-c",
                "import site; print(site.getusersitepackages())",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        assert user_site_result.returncode == 0, user_site_result.stderr
        user_site = Path(user_site_result.stdout.strip())
        user_site.mkdir(parents=True)
        (user_site / "sitecustomize.py").write_text(
            "import builtins\nbuiltins.OWNER_USER_SITE_STARTED = True\n"
        )

        startup_probe = (
            "import builtins; "
            "print(getattr(builtins, 'OWNER_USER_SITE_STARTED', False))"
        )
        control = subprocess.run(
            [base_executable, "-c", startup_probe],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
            timeout=60,
        )
        assert control.returncode == 0, control.stderr
        assert control.stdout.strip() == "True", "the user-site probe was not armed"

        import_paths = [
            str(_REPO_ROOT),
            *(entry for entry in sys.path if "site-packages" in entry),
        ]
        inspect_command = (
            "import json, sys\n"
            f"sys.path[:0] = {import_paths!r}\n"
            "from linkedin_mcp_server.daemon_election import _spawn_command\n"
            "print(json.dumps(_spawn_command(lock_fd=None)))\n"
        )
        parent = subprocess.run(
            [base_executable, *parent_flags, "-c", inspect_command],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
            timeout=60,
        )
        assert parent.returncode == 0, parent.stderr[-1000:]
        command = json.loads(parent.stdout.strip().splitlines()[-1])
        assert all(flag in command for flag in parent_flags), command
        assert "-P" in command, command
        assert command[-2:] == ["-m", "linkedin_mcp_server.daemon_owner"]

        child = subprocess.run(
            [*command[:-2], "-c", startup_probe],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=environment,
            timeout=60,
        )
        assert child.returncode == 0, child.stderr[-1000:]
        assert child.stdout.strip() == str(user_site_started), (
            "the owner command changed the parent interpreter's user-site mode"
        )

    def test_the_owner_preserves_parent_ignore_environment_mode(self):
        import json

        base_executable = getattr(sys, "_base_executable", sys.executable)
        import_paths = [
            str(_REPO_ROOT),
            *(entry for entry in sys.path if "site-packages" in entry),
        ]
        inspect_command = (
            "import json, sys\n"
            f"sys.path[:0] = {import_paths!r}\n"
            "from linkedin_mcp_server.daemon_election import _spawn_command\n"
            "print(json.dumps(_spawn_command(lock_fd=None)))\n"
        )

        parent = subprocess.run(
            [base_executable, "-E", "-c", inspect_command],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert parent.returncode == 0, parent.stderr[-1000:]
        command = json.loads(parent.stdout.strip().splitlines()[-1])
        assert "-E" in command, command
        assert "-P" in command, command

    def test_the_owner_is_not_started_from_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # `python -m` puts the inherited working directory first on sys.path, so
        # a workspace containing linkedin_mcp_server/daemon_owner.py is imported
        # in preference to the installed package. That code would receive the
        # inherited lock descriptor and the whole configuration on standard
        # input, proxy_password included, merely because an MCP client happened
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
        # which file that module resolved to. Everything before it, including the
        # interpreter and its flags, is exactly what production uses.
        command = election_module._spawn_command(lock_fd=None)
        assert command[-2:] == ["-m", "linkedin_mcp_server.daemon_owner"], command
        monkeypatch.setenv("PYTHONPATH", ".")
        environment = election_module._owner_environment()
        assert "PYTHONPATH" not in environment
        result = subprocess.run(
            [
                *command[:-2],
                "-c",
                "import linkedin_mcp_server.daemon_owner as m; print(m.__file__)",
            ],
            capture_output=True,
            text=True,
            cwd=workspace,
            env=environment,
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

        owner_allowance = (
            daemon_owner._STARTUP_PROBE_SECONDS + daemon_owner._COMMIT_AUTH_SECONDS
        )
        assert owner_allowance < election_module.DEFAULT_ELECTION_SECONDS, (
            "an owner can now outlast the frontend that is waiting for it"
        )

        # With room for configuration handover and lock attempts before the
        # owner's clocks start.
        assert owner_allowance <= election_module.DEFAULT_ELECTION_SECONDS * 2 / 3


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
                daemon_owner._serve_until_stopped(server, serving, [], lock=None)
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

    def test_a_completed_shutdown_hard_exits_when_chromium_is_unconfirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        from linkedin_mcp_server import daemon_owner

        exited: list[bool] = []
        monkeypatch.setattr(daemon_owner, "hard_exit_required", lambda: True)
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda _lock: exited.append(True)
        )

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(0))
            await daemon_owner._stop_within(serving, 1, lock=None)

        asyncio.run(exercise())

        assert exited == [True]

    def test_an_independently_completed_server_hard_exits_when_chromium_is_unconfirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        from linkedin_mcp_server import daemon_owner

        exited: list[bool] = []
        monkeypatch.setattr(daemon_owner, "hard_exit_required", lambda: True)
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda _lock: exited.append(True)
        )

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(0))
            await serving
            await daemon_owner._serve_until_stopped(object(), serving, [], lock=None)

        asyncio.run(exercise())

        assert exited == [True]

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
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda _lock: exited.append(True)
        )

        async def exercise() -> float:
            server = NeverStops()
            serving = asyncio.create_task(server.serve())
            began = time.monotonic()
            try:
                await daemon_owner._serve_until_stopped(
                    server, serving, ["asked"], lock=None
                )
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
        # process ends, which is precisely what an in-process test cannot see.
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
            "    await daemon_owner._serve_until_stopped("
            "server, serving, ['asked'], lock=lock)\n"
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


class _ProcessEnded(BaseException):
    """The process ending inside ``hard_exit_process_tree``.

    Not an ``Exception``: ``_stop_within`` catches those around its own wait.
    """


class TestTheHardExitFreesTheElectionFirst:
    """The drain is unbounded on both platforms and holds the profile until the
    browser is provably gone. Spending the election on that wait too leaves a
    profile no later election can replace."""

    def test_the_drain_begins_with_the_election_already_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Driven through the whole owner rather than through ``_exit_hard``, so
        # the lock reaching it is the production wiring and not the test's.
        # Only the state operations are stood in for; `_serve_until_stopped`,
        # which is what hands the lock down, stays the real one.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        nonce = "0123456789abcdef" * 4
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        lock = DaemonLock(auth_root)
        assert lock.try_acquire(), "the test could not take the lock it means to free"
        held_at_the_drain: list[bool] = []

        def drain(status: int) -> None:
            held_at_the_drain.append(lock.held)
            raise _ProcessEnded

        async def probe(url: str, token: str) -> None:
            return None

        class _StoppedServer:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        class _SilentHandshake:
            def prepared(self, instance_id: str) -> None: ...

            def ready(self) -> None: ...

            def committed(self) -> None: ...

            def fail(self) -> None: ...

            def abort(self) -> None: ...

            def retry(self) -> None: ...

            def uncertain(self) -> None: ...

            def close(self) -> None: ...

        class _CommittingParent:
            def readline(self) -> str:
                return f"owner {nonce} commit\n"

        monkeypatch.setattr(daemon_owner, "hard_exit_process_tree", drain)
        monkeypatch.setattr(daemon_owner, "hard_exit_required", lambda: True)
        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )
        monkeypatch.setattr(
            daemon_owner, "create_owner_server", lambda **kwargs: _StoppedServer()
        )
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor, "prepare", lambda *a, **k: None
        )
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor, "commit_prepared", lambda *a, **k: None
        )
        # Native Windows takes the adoption branch for real, and a genuine
        # adoption would keep a handle in a module global the later Job tests
        # then fail on. A stand-in keeps this case identical on both platforms.
        monkeypatch.setattr(
            daemon_owner.WindowsJob,
            "adopt_current_process",
            staticmethod(lambda name: None),
        )

        served: list[int] = []
        # The stand-in raises where the real call ends the process, so reaching
        # the line after would be the owner resuming an event loop it had left.
        with pytest.raises(_ProcessEnded):
            served.append(
                asyncio.run(
                    daemon_owner._serve(
                        lock=lock,
                        auth_root=auth_root,
                        profile=profile,
                        config=config,
                        log_path=auth_root / "daemon.log",
                        handshake=_SilentHandshake(),
                        handshake_nonce=nonce,
                        control=cast(Any, _CommittingParent()),
                        job_name=_OWNER_JOB_NAME,
                    )
                )
            )

        assert served == [], "the hard exit returned to its caller"
        assert held_at_the_drain == [False], (
            "the drain began while the election lock was still held"
        )

    @_POSIX_ONLY
    def test_another_process_elects_while_the_drain_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The kernel half, which no in-process check can see: ``lock.held`` is
        # bookkeeping, and only a different process asking proves the release.
        home = tmp_path / "drain-home"
        home.mkdir()
        auth_root = tmp_path / "drain-auth"
        auth_root.mkdir()
        draining = tmp_path / "drain-began"

        script = tmp_path / "draining_owner.py"
        script.write_text(
            "import sys, time\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon_descriptor as descriptor\n"
            "descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "from linkedin_mcp_server import daemon_owner, process_tree\n"
            "from linkedin_mcp_server.daemon_lock import DaemonLock\n"
            "lock = DaemonLock(Path(sys.argv[2]))\n"
            "assert lock.try_acquire()\n"
            "def never_drains(groups, *, markers=None, deadline=None):\n"
            "    Path(sys.argv[3]).touch()\n"
            "    while True:\n"
            "        time.sleep(0.05)\n"
            "process_tree._wait_for_process_groups = never_drains\n"
            "daemon_owner._exit_hard(lock)\n"
        )
        process = subprocess.Popen(
            [sys.executable, str(script), str(home), str(auth_root), str(draining)],
            cwd=_REPO_ROOT,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(500):
                if draining.exists() or process.poll() is not None:
                    break
                time.sleep(0.01)
            assert draining.exists(), "the owner never reached its drain"
            assert process.poll() is None, "the owner exited before its drain"

            monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)
            probe = DaemonLock(auth_root)
            assert probe.try_acquire(), (
                "the election stayed occupied for the whole browser drain"
            )
            probe.release()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)


#: The Job Object handoff every generic ``_run_serve`` case carries. On
#: Windows ``_serve`` refuses to publish without one, and the required Windows
#: CI job runs this whole class natively, where ``os.name`` is "nt" with
#: nothing patched. A case that means to exercise adoption names its own Job.
_OWNER_JOB_NAME = "test-owner-job"


class TestPublishingLast:
    """The child proves and prepares; only canonical state lets it serve."""

    def test_prepublication_cleanup_preserves_the_attachable_predecessor(
        self, tmp_path: Path
    ):
        profile = _profile(tmp_path)
        auth_root = profile.parent
        config = _config(profile)
        predecessor_id = _publish_stale_owner(auth_root, profile, config)
        stale_id = new_instance_id()
        stale_token = daemon_descriptor_module.token_path(auth_root, stale_id)
        stale_pending = daemon_descriptor_module.pending_descriptor_path(
            auth_root, stale_id
        )
        stale_token.write_text("superseded")
        stale_pending.write_text("abandoned")

        daemon_owner._forget_superseded_tokens(auth_root)

        predecessor = daemon_descriptor_module.read(auth_root)
        assert predecessor is not None
        assert predecessor.instance_id == predecessor_id
        assert daemon_descriptor_module.read_token(auth_root, predecessor)
        assert not stale_token.exists()
        assert not stale_pending.exists()

        replacement_id = new_instance_id()
        replacement_token = new_token()
        replacement = daemon_descriptor_module.build(
            instance_id=replacement_id,
            package_version=__version__,
            runtime_id=_RUNTIME,
            profile=profile,
            host="127.0.0.1",
            port=49153,
            path="/mcp",
            token=replacement_token,
            config=config,
            log_path=auth_root / "daemon.log",
        )
        daemon_descriptor_module.prepare(auth_root, replacement, replacement_token)
        daemon_descriptor_module.discard_prepared(auth_root, replacement_id)

        lookup = daemon_module.look_up_owner(auth_root, profile, config)
        assert lookup.state is OwnerState.ATTACHABLE
        assert lookup.attachment is not None
        assert lookup.attachment.descriptor.instance_id == predecessor_id

    def _run_serve(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        order: list[str],
        *,
        commit: bool = True,
        commit_error: BaseException | None = None,
        commit_failures: list[BaseException] | None = None,
        canonical_after_reads: int | None = None,
        control: object | None = None,
        after_probe: Callable[[], None] | None = None,
        idle_timeout: float | None = None,
        track_cleanup: bool = False,
        server_factory: Callable[[Callable[[], None]], object] | None = None,
        job_name: str = _OWNER_JOB_NAME,
        adopt: Callable[[str], None] | None = None,
        startup_protocol: int = daemon_config.STARTUP_PROTOCOL_VERSION,
        commit_implementation: Callable[[Path, str], object] | None = None,
    ) -> int:
        import asyncio

        profile = _profile(tmp_path)
        config = _config(profile)
        if idle_timeout is not None:
            config.browser.browser_idle_timeout_seconds = idle_timeout
        auth_root = profile.parent
        prepared: list[object] = []

        async def probe(url: str, token: str) -> None:
            order.append("probe")
            if after_probe is not None:
                after_probe()

        def prepare(root: Path, descriptor: object, token: str) -> None:
            order.append("prepare")
            prepared.append(descriptor)

        class _RecordingHandshake:
            def prepared(self, instance_id: str) -> None:
                order.append("prepared")

            def ready(self) -> None:
                order.append("ready")

            def committed(self) -> None:
                order.append("committed")

            def fail(self) -> None:  # pragma: no cover - not reached here
                order.append("failed")

            def abort(self) -> None:
                order.append("aborted")

            def retry(self) -> None:
                order.append("retry")

            def uncertain(self) -> None:
                order.append("uncertain")

            def close(self) -> None:
                return None

        class _Control:
            def readline(self) -> str:
                order.append("control")
                return f"owner {'0123456789abcdef' * 4} commit\n" if commit else ""

        class _StoppedServer:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                import asyncio

                while not self.should_exit:
                    await asyncio.sleep(0)

        default_server = _StoppedServer()
        servers: list[object] = []

        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(daemon_owner.daemon_descriptor, "prepare", prepare)

        def no_adoption(name: str) -> None:
            """Take the Windows branch without touching a real Job Object."""
            return None

        # Native Windows enters that branch for real, so every case here needs
        # the adoption isolated as well as the handoff supplied. The genuine
        # `adopt_current_process` opens a Job by name and keeps the handle in a
        # module global that nothing resets: a failure would end the case as an
        # abort, and a success would be worse, because the global then refuses
        # the adoption the real Job tests perform later in the same process.
        # This records nothing, so the order a case asserts reads identically
        # on both platforms; a case about adoption passes its own `adopt`.
        monkeypatch.setattr(
            daemon_owner.WindowsJob,
            "adopt_current_process",
            staticmethod(no_adoption if adopt is None else adopt),
        )

        def commit_prepared(root: Path, instance_id: str) -> None:
            order.append("commit")
            if commit_failures:
                raise commit_failures.pop(0)
            if commit_error is not None:
                raise commit_error

        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "commit_prepared",
            commit_prepared if commit_implementation is None else commit_implementation,
        )
        if canonical_after_reads is not None:
            reads = 0

            def read(root: Path) -> object | None:
                nonlocal reads
                reads += 1
                if reads > canonical_after_reads:
                    order.append("canonical")
                    return prepared[0]
                return None

            monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", read)
        monkeypatch.setattr(
            daemon_owner,
            "_forget_superseded_tokens",
            lambda *a, **k: order.append("cleanup") if track_cleanup else None,
        )
        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )

        def create_server(**kwargs: object) -> object:
            stand_down = cast(Callable[[], None], kwargs["stand_down"])
            server = (
                default_server if server_factory is None else server_factory(stand_down)
            )
            servers.append(server)
            return server

        monkeypatch.setattr(daemon_owner, "create_owner_server", create_server)

        async def serve_until_stopped(
            server: object,
            serving: object,
            turnover: object,
            idle_timeout: float,
            *,
            lock: DaemonLock | None,
        ) -> None:
            import asyncio

            cast(Any, server).should_exit = True
            await asyncio.sleep(0)
            await cast(Any, serving)

        monkeypatch.setattr(daemon_owner, "_serve_until_stopped", serve_until_stopped)

        def endpoint_is_live() -> None:
            order.append("live")
            cast(Any, servers[0]).should_exit = True

        monkeypatch.setattr(
            daemon_owner.get_liveness(),
            "the_endpoint_is_live",
            endpoint_is_live,
        )

        return asyncio.run(
            daemon_owner._serve(
                lock=DaemonLock(auth_root),
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=auth_root / "daemon.log",
                handshake=_RecordingHandshake(),
                handshake_nonce="0123456789abcdef" * 4,
                control=cast(Any, control if control is not None else _Control()),
                startup_protocol=startup_protocol,
                job_name=job_name,
            )
        )

    def test_predecessor_frontend_receives_ready_without_commit_control(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []

        self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            startup_protocol=1,
        )

        assert "prepared" not in order
        assert "control" not in order
        assert order.index("ready") < order.index("commit")
        assert "committed" not in order

    @pytest.mark.parametrize("startup_protocol", [2, 3])
    def test_every_authorizing_frontend_is_waited_for(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        startup_protocol: int,
    ):
        # Both versions that can authorize a commit are read the same way, and
        # the owner never learns which channel carried the record: the frontend
        # decides that when it chooses what to hand over. Protocol 2 is the
        # rollback case in the other direction, a frontend that still holds the
        # configuration pipe open for its own record.
        order: list[str] = []

        self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            startup_protocol=startup_protocol,
        )

        assert "ready" not in order
        assert order.index("prepared") < order.index("control")
        assert order.index("control") < order.index("commit")
        assert order[-1] == "committed"

    def test_a_descriptor_is_prepared_only_after_the_endpoint_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        self._run_serve(tmp_path, monkeypatch, order)
        assert order.index("probe") < order.index("prepare"), order

    def test_token_cleanup_finishes_before_preparing_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.logger,
            "info",
            lambda *args, **kwargs: pytest.fail("publication performed log I/O"),
        )
        self._run_serve(tmp_path, monkeypatch, order, track_cleanup=True)

        assert order.index("probe") < order.index("cleanup")
        assert order.index("cleanup") < order.index("prepare")
        assert order[-1] == "committed", "state I/O followed the startup verdict"

    def test_commit_gets_a_fresh_window_after_slow_endpoint_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        clock = [100.0]
        monkeypatch.setattr(daemon_owner.time, "monotonic", lambda: clock[0])

        self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            after_probe=lambda: clock.__setitem__(0, clock[0] + 30.1),
        )

        assert order[-3:] == ["commit", "live", "committed"]

    def test_windows_adopts_after_commit_control_and_before_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(daemon_owner, "os", SimpleNamespace(name="nt"))

        self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            job_name="named-owner-job",
            adopt=lambda name: order.append(f"adopt:{name}"),
        )

        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "adopt:named-owner-job",
            "commit",
            "live",
            "committed",
        ]

    def test_windows_adoption_failure_aborts_before_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(daemon_owner, "os", SimpleNamespace(name="nt"))

        def reject(name: str) -> None:
            order.append(f"adopt:{name}")
            raise RuntimeError("Job adoption failed")

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            job_name="named-owner-job",
            adopt=reject,
        )

        assert outcome == 1
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "adopt:named-owner-job",
            "aborted",
        ]
        assert "commit" not in order
        assert "committed" not in order
        assert "uncertain" not in order

    def test_a_generic_case_hands_the_owner_the_job_it_needs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # What the required Windows CI job does to every case in this class:
        # there `os.name` is "nt" already, so `_serve` reaches its Job branch
        # and refuses to publish without a handoff. This case takes that branch
        # on any platform while asking for nothing beyond the defaults, so a
        # handoff dropped from `_run_serve` fails here rather than only on
        # Windows, where it would fail twenty-one cases at once.
        order: list[str] = []
        adopted: list[str] = []
        monkeypatch.setattr(daemon_owner, "os", SimpleNamespace(name="nt"))

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            adopt=adopted.append,
        )

        assert outcome == 0
        assert adopted == [_OWNER_JOB_NAME]
        assert order[-1] == "committed"

    def test_a_generic_case_never_performs_a_real_job_adoption(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The same branch on the helper's own defaults, adoption included. The
        # genuine `adopt_current_process` fails here for the platform and on
        # Windows for the name, and both end this case as an abort rather than
        # a publication, so the stub is what keeps it green.
        order: list[str] = []
        monkeypatch.setattr(daemon_owner, "os", SimpleNamespace(name="nt"))

        outcome = self._run_serve(tmp_path, monkeypatch, order)

        assert outcome == 0
        assert "aborted" not in order
        assert order[-1] == "committed"

    def test_parent_eof_before_commit_aborts_the_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.logger, "info", lambda *args, **kwargs: order.append("logged")
        )
        self._run_serve(tmp_path, monkeypatch, order, commit=False)
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "logged",
            "aborted",
        ]

    @pytest.mark.parametrize(
        "record",
        ["commit\n", f"owner {'f' * 64} commit\n"],
        ids=["plain", "wrong-nonce"],
    )
    def test_commit_authorization_requires_the_handed_over_nonce(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        record: str,
    ):
        order: list[str] = []

        class _UnauthenticatedParent:
            def readline(self) -> str:
                order.append("control")
                return record

        monkeypatch.setattr(
            daemon_owner.logger, "info", lambda *args, **kwargs: order.append("logged")
        )

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            control=_UnauthenticatedParent(),
        )

        assert outcome == 0
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "logged",
            "aborted",
        ]
        assert "commit" not in order

    def test_commit_authorization_expires_while_parent_pipe_stays_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        release = threading.Event()
        order: list[str] = []

        class _SuspendedParent:
            def readline(self) -> str:
                order.append("control")
                release.wait()
                return "commit\n"

        monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.1)
        monkeypatch.setattr(
            daemon_owner.logger,
            "warning",
            lambda *args, **kwargs: order.append("logged"),
        )
        try:
            outcome = self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                control=_SuspendedParent(),
            )
        finally:
            release.set()

        assert outcome == 1
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "logged",
            "retry",
        ]

    @pytest.mark.parametrize("failure", ["abort", "retry", "definitive"])
    def test_failed_start_does_not_reenter_blocked_cleanup_storage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
    ):
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        release_control = threading.Event()
        finished = threading.Event()
        order: list[str] = []
        outcomes: list[int] = []
        errors: list[BaseException] = []

        def discard(root: Path, instance_id: str) -> None:
            cleanup_started.set()
            release_cleanup.wait()

        class _SuspendedParent:
            def readline(self) -> str:
                order.append("control")
                release_control.wait()
                return "commit\n"

        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "discard_prepared",
            discard,
        )
        if failure == "retry":
            monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.05)
        if failure == "definitive":
            monkeypatch.setattr(
                daemon_owner.logger,
                "exception",
                lambda *args, **kwargs: order.append("logged"),
            )

        def run() -> None:
            try:
                if failure == "abort":
                    outcomes.append(
                        self._run_serve(
                            tmp_path,
                            monkeypatch,
                            order,
                            commit=False,
                        )
                    )
                elif failure == "retry":
                    outcomes.append(
                        self._run_serve(
                            tmp_path,
                            monkeypatch,
                            order,
                            control=_SuspendedParent(),
                        )
                    )
                else:
                    outcomes.append(
                        self._run_serve(
                            tmp_path,
                            monkeypatch,
                            order,
                            commit_error=CommitPreflightError("definitive"),
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - asserted by this test
                errors.append(exc)
            finally:
                finished.set()

        child = threading.Thread(target=run, daemon=True)
        child.start()
        try:
            assert finished.wait(1), "failed-start cleanup pinned the owner child"
            assert not cleanup_started.is_set(), (
                "failed start re-entered cleanup storage"
            )
            assert ("retry" if failure == "retry" else "aborted") in order
        finally:
            release_control.set()
            release_cleanup.set()
            child.join(timeout=5)

        assert not child.is_alive()
        if failure == "definitive":
            assert outcomes == []
            assert len(errors) == 1
            assert isinstance(errors[0], CommitPreflightError)
        else:
            assert errors == []
            assert outcomes == [1 if failure == "retry" else 0]

    def test_windows_replace_classifier_accepts_only_sharing_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        class _Windows:
            name = "nt"

        sharing_violation = OSError("sharing")
        setattr(sharing_violation, "winerror", 32)
        unrelated = OSError("missing")
        setattr(unrelated, "winerror", 2)
        monkeypatch.setattr(daemon_owner, "os", _Windows())

        assert daemon_owner._retryable_windows_replace(sharing_violation)
        assert not daemon_owner._retryable_windows_replace(unrelated)

    def test_windows_sharing_violation_is_retried_before_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        sharing_violation = OSError("descriptor is still open")
        setattr(sharing_violation, "winerror", 32)
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner,
            "_retryable_windows_replace",
            lambda exc: getattr(exc, "winerror", None) in {5, 32, 33},
        )

        self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            commit_failures=[sharing_violation],
        )

        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "commit",
            "live",
            "committed",
        ]

    @pytest.mark.parametrize("winerror", [5, 32])
    def test_persistent_windows_replace_failure_aborts_as_unpublished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
    ):
        blocked = OSError("descriptor stays open")
        setattr(blocked, "winerror", winerror)
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner,
            "_retryable_windows_replace",
            lambda exc: getattr(exc, "winerror", None) in {5, 32, 33},
        )
        # Above one Windows timer tick of about 15.6 ms. At 0.02 the startup
        # handshake this helper drives timed out before the replace under test
        # was ever attempted, and the failure read as a missing exception.
        monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.25)
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: order.append("logged"),
        )

        async def reconcile(*_args: object, **_kwargs: object) -> None:
            pytest.fail("a failed MoveFileEx attempt entered reconciliation")

        monkeypatch.setattr(daemon_owner, "_reconcile_uncertain_publication", reconcile)

        with pytest.raises(daemon_owner._CommitPublicationUnpublished):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_error=blocked,
            )

        assert order.count("commit") >= 1
        assert order[:4] == ["probe", "prepare", "prepared", "control"]
        assert order[-2:] == ["logged", "aborted"]

    def test_unreadable_state_after_commit_interrupt_is_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: (_ for _ in ()).throw(OSError("cannot read canonical state")),
        )

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            commit_failures=[KeyboardInterrupt()],
        )

        assert outcome == 0
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "commit",
            "live",
            "committed",
        ]

    def test_commit_recovery_read_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        blocked = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def read(root: Path) -> None:
            blocked.set()
            release.wait()
            return None

        monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", read)
        # Above one timer tick, which on Windows is about 15 ms. The same
        # constant also bounds the startup handshake this helper drives, and at
        # 0.01 that half timed out before the recovery read under test was ever
        # reached. The bound is still short enough that an unbounded read shows
        # up as the whole fallback below.
        monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.25)
        # So a read that is never bounded ends the test instead of hanging it.
        fallback = threading.Timer(5.0, release.set)
        fallback.start()
        started = time.monotonic()
        try:
            outcome = self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_failures=[OSError("rename result was lost")],
            )
        finally:
            release.set()
            fallback.cancel()

        elapsed = time.monotonic() - started
        assert outcome == 0
        assert order[-3:] == ["commit", "live", "committed"]
        assert blocked.is_set()
        assert elapsed < 2.0, f"the recovery read was not bounded ({elapsed:.2f}s)"

    def test_first_ambiguous_read_keeps_turnover_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        blocked = threading.Event()
        release = threading.Event()
        turnover_sent = threading.Event()
        exited: list[object] = []
        order: list[str] = []

        def read(root: Path) -> None:
            blocked.set()
            release.wait()
            return None

        class _TurnoverServer:
            started = True
            should_exit = False

            def __init__(self, stand_down: Callable[[], None]) -> None:
                self._stand_down = stand_down

            async def serve(self, sockets: object = None) -> None:
                while not blocked.is_set() and not self.should_exit:
                    await asyncio.sleep(0)
                if not self.should_exit:
                    self._stand_down()
                    turnover_sent.set()
                while not self.should_exit:
                    await asyncio.sleep(0)

        monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", read)
        monkeypatch.setattr(daemon_owner, "_STAND_DOWN_POLL_SECONDS", 0.001)
        monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 1.0)
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda received: exited.append(received)
        )
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="hard exit returned"):
                self._run_serve(
                    tmp_path,
                    monkeypatch,
                    order,
                    commit_failures=[OSError("rename result was lost")],
                    server_factory=_TurnoverServer,
                )
        finally:
            release.set()

        assert blocked.is_set(), "the first canonical read never started"
        assert turnover_sent.is_set(), "the endpoint could not request turnover"
        # Driven through the real `_serve`, so what `_exit_hard` receives here is
        # the owner's own lock and not a value this case handed to the helper.
        # A reconciliation exit that skipped it would leave the election held for
        # an unbounded browser drain.
        assert exited and all(
            isinstance(candidate, DaemonLock) for candidate in exited
        ), "the uncertain-publication exit did not free the election first"
        assert order.count("commit") == 1, "publication retried after turnover"
        assert time.monotonic() - started < 0.5

    def test_preflight_after_windows_retry_aborts_as_unpublished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        sharing_violation = OSError("descriptor is still open")
        setattr(sharing_violation, "winerror", 32)
        preflight = daemon_owner.daemon_descriptor.CommitPreflightError(
            "state became unreadable"
        )
        monkeypatch.setattr(
            daemon_owner,
            "_retryable_windows_replace",
            lambda exc: getattr(exc, "winerror", None) in {5, 32, 33},
        )
        monkeypatch.setattr(daemon_owner, "_WINDOWS_REPLACE_RETRY_SECONDS", 0)
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: order.append("logged"),
        )

        with pytest.raises(daemon_owner.daemon_descriptor.CommitPreflightError):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_failures=[sharing_violation, preflight],
            )

        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "commit",
            "logged",
            "aborted",
        ]

    def test_delayed_publication_is_proved_before_listening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.logger,
            "warning",
            lambda *args, **kwargs: order.append("diagnostic"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "info",
            lambda *args, **kwargs: pytest.fail("publication proof performed log I/O"),
        )

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            commit_error=OSError("rename reply was lost"),
            canonical_after_reads=0,
            idle_timeout=0,
        )

        assert outcome == 0
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "diagnostic",
            "canonical",
            "live",
            "committed",
        ]

    def test_attempted_rename_failure_is_reconciled_before_listening(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", lambda root: None)
        monkeypatch.setattr(
            daemon_owner.logger,
            "warning",
            lambda *args, **kwargs: order.append("diagnostic"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "info",
            lambda *args, **kwargs: pytest.fail("publication proof performed log I/O"),
        )

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            commit_failures=[OSError("rename reply was lost")],
            idle_timeout=0,
        )

        assert outcome == 0
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "diagnostic",
            "commit",
            "live",
            "committed",
        ]

    def test_endpoint_death_during_reconciliation_hard_exits_before_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        class _Server:
            should_exit = False

        commits: list[str] = []
        exited: list[object] = []
        lock = DaemonLock(tmp_path)

        async def exercise() -> None:
            stopped = asyncio.Event()

            async def serve() -> None:
                await stopped.wait()
                raise RuntimeError("endpoint failed")

            serving = asyncio.create_task(serve())

            async def read(root: Path, deadline: float) -> None:
                stopped.set()
                with contextlib.suppress(RuntimeError):
                    await serving
                return None

            async def commit(
                root: Path,
                instance_id: str,
                deadline: float,
                *,
                maintenance: Callable[[], None] | None = None,
            ) -> None:
                commits.append(instance_id)

            monkeypatch.setattr(
                daemon_owner,
                "_start_canonical_read",
                lambda root: asyncio.create_task(read(root, 0)),
            )
            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )

            with pytest.raises(RuntimeError, match="hard exit returned"):
                await daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=_Server(),
                    serving=serving,
                    lock=lock,
                )

        asyncio.run(exercise())

        assert exited == [lock], "the hard exit did not receive the owner lock"
        assert commits == [], "a dead endpoint was published during reconciliation"

    @pytest.mark.parametrize(
        "winerror,retryable",
        [(5, True), (32, True), (33, True), (2, False), (183, False)],
    )
    def test_the_windows_replace_classifier_keeps_its_own_set(
        self, winerror: int, retryable: bool, monkeypatch: pytest.MonkeyPatch
    ):
        """Which replacement refusals are a reader that has not let go yet.

        Losing one of the three turns an ordinary transient refusal into a
        terminal publication failure, and every publication test that supplies
        its own classifier stays green through that.
        """
        monkeypatch.setattr(daemon_owner.os, "name", "nt")
        refusal = OSError("the descriptor could not be replaced")
        # winerror exists on OSError only under a Windows build, and the
        # classifier reads it with getattr for exactly that reason.
        setattr(refusal, "winerror", winerror)  # noqa: B010

        assert daemon_owner._retryable_windows_replace(refusal) is retryable

    def test_a_read_that_cannot_start_does_not_end_the_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A thread that will not start has not disproved publication.

        Leaving here stops an endpoint the canonical descriptor may already
        name and releases the election behind it, which is the one outcome this
        loop exists to prevent.
        """
        import asyncio

        class _Server:
            should_exit = False

        instance_id = new_instance_id()
        starts = 0
        exited: list[object] = []
        lock = DaemonLock(tmp_path)

        async def exercise() -> None:
            async def serve() -> None:
                await asyncio.Event().wait()

            serving = asyncio.create_task(serve())

            def start(root: Path) -> Any:
                nonlocal starts
                starts += 1
                if starts == 1:
                    raise RuntimeError("can't start new thread")
                answer = asyncio.get_running_loop().create_future()
                answer.set_result(SimpleNamespace(instance_id=instance_id))
                return answer

            async def commit(
                root: Path,
                identifier: str,
                deadline: float,
                *,
                maintenance: Callable[[], None] | None = None,
            ) -> None:
                raise OSError("the replacement is still ambiguous")

            monkeypatch.setattr(daemon_owner, "_start_canonical_read", start)
            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            monkeypatch.setattr(
                daemon_owner, "_UNCERTAIN_PUBLICATION_RETRY_SECONDS", 0.0
            )
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )

            await daemon_owner._reconcile_uncertain_publication(
                tmp_path,
                instance_id,
                server=_Server(),
                serving=serving,
                lock=lock,
            )
            serving.cancel()

        asyncio.run(exercise())

        assert starts == 2, "the failed start was retried rather than raised"
        assert exited == [], "and nothing ended the process over it"

    def test_endpoint_death_after_replacement_hard_exits_before_live(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        class _Server:
            should_exit = False

        exited: list[object] = []
        lock = DaemonLock(tmp_path)

        async def exercise() -> None:
            stopped = asyncio.Event()

            async def serve() -> None:
                await stopped.wait()

            serving = asyncio.create_task(serve())

            async def read(root: Path, deadline: float) -> None:
                return None

            async def commit(
                root: Path,
                instance_id: str,
                deadline: float,
                *,
                maintenance: Callable[[], None] | None = None,
            ) -> None:
                stopped.set()

            monkeypatch.setattr(
                daemon_owner,
                "_start_canonical_read",
                lambda root: asyncio.create_task(read(root, 0)),
            )
            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )

            with pytest.raises(RuntimeError, match="hard exit returned"):
                await daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=_Server(),
                    serving=serving,
                    lock=lock,
                )

        asyncio.run(exercise())

        assert exited == [lock], "the hard exit did not receive the owner lock"

    def test_reconciliation_cancels_a_call_after_heartbeats_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        import linkedin_mcp_server.daemon_liveness as daemon_liveness_module

        class _Server:
            should_exit = False

        exited: list[object] = []
        lock = DaemonLock(tmp_path)

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(3600))
            canonical_read = asyncio.get_running_loop().create_future()
            abandoned = asyncio.create_task(asyncio.sleep(3600))
            marker = daemon_liveness_module.new_call_id()
            daemon_owner.get_liveness().watch(marker, abandoned)
            # All three above one timer tick, which on Windows is about 15 ms
            # against well under a millisecond here. Below it, the poll that
            # drives the expiry scan does not run at the cadence these numbers
            # describe, and the test measures the platform instead of the code.
            monkeypatch.setattr(daemon_liveness_module, "EXPIRY_SECONDS", 0.05)
            monkeypatch.setattr(daemon_owner, "_STAND_DOWN_POLL_SECONDS", 0.02)
            # Long, on purpose: the canonical read has to stay held for the
            # whole window below, so reaching the commit stub is a real failure
            # rather than the deadline arriving first.
            monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 30.0)
            monkeypatch.setattr(daemon_owner, "stand_down_reason", lambda: None)
            monkeypatch.setattr(
                daemon_owner, "_start_canonical_read", lambda root: canonical_read
            )
            monkeypatch.setattr(
                daemon_owner,
                "_commit_prepared_until",
                lambda *args, **kwargs: pytest.fail(
                    "reconciliation left its held canonical read"
                ),
            )
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )
            reconciling = asyncio.create_task(
                daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=_Server(),
                    serving=serving,
                    lock=lock,
                )
            )
            try:
                # A wall-clock budget rather than an iteration count. One
                # iteration is a whole timer tick on Windows, so a fixed count
                # is a different amount of real time on each platform.
                waited = await _until(lambda: abandoned.cancelled(), seconds=10.0)
                if reconciling.done():
                    pytest.fail(f"reconciliation ended early: {_outcome(reconciling)}")
                assert abandoned.cancelled(), (
                    f"the abandoned call kept running for {waited:.2f}s"
                )
                reconciling.cancel()
                with pytest.raises(RuntimeError, match="hard exit returned"):
                    await reconciling
            finally:
                if not reconciling.done():
                    reconciling.cancel()
                    with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                        await reconciling
                canonical_read.cancel()
                abandoned.cancel()
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving
                with contextlib.suppress(asyncio.CancelledError):
                    await abandoned

        asyncio.run(exercise())

        assert exited == [lock], "the hard exit did not receive the owner lock"

    def test_stand_down_remains_active_during_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        class _Server:
            should_exit = False

        server = _Server()
        turnover: list[str] = []
        exited: list[object] = []
        lock = DaemonLock(tmp_path)
        commits: list[str] = []

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(3600))
            canonical_read = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(daemon_owner, "_STAND_DOWN_POLL_SECONDS", 0.001)
            monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 1.0)
            monkeypatch.setattr(daemon_owner, "stand_down_reason", lambda: None)
            monkeypatch.setattr(
                daemon_owner, "_start_canonical_read", lambda root: canonical_read
            )

            async def commit(*args: object, **kwargs: object) -> None:
                commits.append("attempted")

            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )
            reconciling = asyncio.create_task(
                daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=server,
                    serving=serving,
                    lock=lock,
                    turnover=turnover,
                )
            )
            try:
                await asyncio.sleep(0)
                turnover.append("asked")
                for _ in range(100):
                    if reconciling.done():
                        break
                    await asyncio.sleep(0.001)
                assert reconciling.done(), "the stand-down request was not noticed"
                with pytest.raises(RuntimeError, match="hard exit returned"):
                    await reconciling
            finally:
                if not reconciling.done():
                    reconciling.cancel()
                    with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                        await reconciling
                canonical_read.cancel()
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving

        asyncio.run(exercise())

        # More than one exit is possible only because the stand-in returns where
        # the real one ends the process: the commit retry swallows `Exception`,
        # so the next pass asks again. Every one of them still frees the election.
        assert exited and all(candidate is lock for candidate in exited), (
            "the hard exit did not receive the owner lock"
        )
        assert server.should_exit
        assert commits == []

    def test_persistent_ambiguity_holds_the_lock_until_cancelled_hard_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A bounded release needs local coordination state tracked by #796."""
        import asyncio

        class _Server:
            should_exit = False

        attempts: list[str] = []
        exited: list[object] = []
        lock = DaemonLock(tmp_path)
        recover = threading.Event()

        async def read(root: Path, deadline: float) -> None:
            return None

        async def commit(
            root: Path,
            instance_id: str,
            deadline: float,
            *,
            maintenance: Callable[[], None] | None = None,
        ) -> None:
            attempts.append(instance_id)
            if recover.is_set():
                return
            raise OSError("persistent remote rename failure")

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(3600))
            monkeypatch.setattr(
                daemon_owner,
                "_start_canonical_read",
                lambda root: asyncio.create_task(read(root, 0)),
            )
            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            monkeypatch.setattr(
                daemon_owner, "_exit_hard", lambda received: exited.append(received)
            )
            monkeypatch.setattr(daemon_owner, "_UNCERTAIN_PUBLICATION_RETRY_SECONDS", 0)
            reconciling = asyncio.create_task(
                daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=_Server(),
                    serving=serving,
                    lock=lock,
                )
            )
            try:
                for _ in range(100):
                    if len(attempts) >= 3:
                        break
                    await asyncio.sleep(0)
                assert len(attempts) >= 3
                assert not reconciling.done()
                assert exited == []

                reconciling.cancel()
                for _ in range(100):
                    if reconciling.done():
                        break
                    await asyncio.sleep(0)
                assert reconciling.done(), "cancellation was ignored"
                with pytest.raises(RuntimeError, match="hard exit returned"):
                    await reconciling
            finally:
                recover.set()
                if not reconciling.done():
                    for _ in range(100):
                        if reconciling.done():
                            break
                        await asyncio.sleep(0)
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving

        asyncio.run(exercise())

        assert exited == [lock], "the hard exit did not receive the owner lock"

    def test_reconciliation_reuses_one_blocked_canonical_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        class _Server:
            should_exit = False

        reads: list[Path] = []
        attempts: list[str] = []
        lock = DaemonLock(tmp_path)
        blocked = threading.Event()
        release = threading.Event()

        def read(root: Path) -> None:
            reads.append(root)
            blocked.set()
            release.wait()
            return None

        async def commit(
            root: Path,
            instance_id: str,
            deadline: float,
            *,
            maintenance: Callable[[], None] | None = None,
        ) -> None:
            attempts.append(instance_id)
            raise OSError("persistent remote rename failure")

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(3600))
            monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", read)
            monkeypatch.setattr(daemon_owner, "_commit_prepared_until", commit)
            # Above one timer tick: this bounds each round's wait on the read
            # that never finishes, and below a tick a round takes whatever the
            # platform's timer happens to cost rather than what it asks for.
            monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.05)
            monkeypatch.setattr(daemon_owner, "_UNCERTAIN_PUBLICATION_RETRY_SECONDS", 0)
            monkeypatch.setattr(daemon_owner, "_exit_hard", lambda received: None)
            reconciling = asyncio.create_task(
                daemon_owner._reconcile_uncertain_publication(
                    tmp_path,
                    new_instance_id(),
                    server=_Server(),
                    serving=serving,
                    lock=lock,
                )
            )
            try:
                waited = await _until(
                    lambda: blocked.is_set() and len(attempts) >= 3, seconds=10.0
                )
                if reconciling.done():
                    pytest.fail(f"reconciliation ended early: {_outcome(reconciling)}")
                assert blocked.is_set()
                assert len(attempts) >= 3, (
                    f"only {len(attempts)} commit attempts in {waited:.2f}s"
                )
                assert reads == [tmp_path]
                reconciling.cancel()
                with pytest.raises(RuntimeError, match="hard exit returned"):
                    await reconciling
            finally:
                release.set()
                if not reconciling.done():
                    reconciling.cancel()
                    with contextlib.suppress(RuntimeError, asyncio.CancelledError):
                        await reconciling
                serving.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await serving

        asyncio.run(exercise())

    def test_uncertain_hard_exit_performs_no_diagnostic_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        exited: list[object] = []
        lock = DaemonLock(tmp_path)
        monkeypatch.setattr(
            daemon_owner.logger,
            "error",
            lambda *a, **k: pytest.fail("hard exit attempted a diagnostic write"),
        )
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda received: exited.append(received)
        )

        with pytest.raises(RuntimeError, match="hard exit returned"):
            daemon_owner._exit_uncertain_publication("already logged", lock=lock)

        assert exited == [lock], "the hard exit did not receive the owner lock"

    def test_hard_exit_skips_logging_shutdown(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            daemon_owner.logging,
            "shutdown",
            lambda: pytest.fail("hard exit tried to flush a blocked log"),
        )
        monkeypatch.setattr(
            daemon_owner.os,
            "_exit",
            lambda status: (_ for _ in ()).throw(SystemExit(status)),
        )

        with pytest.raises(SystemExit) as exited:
            daemon_owner._exit_hard(None)

        assert exited.value.code == 1

    def test_permission_error_with_stale_state_remains_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(daemon_owner.daemon_descriptor, "read", lambda root: None)

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            commit_failures=[
                PermissionError("rename retry observed changed permissions")
            ],
        )

        assert outcome == 0
        assert order[-3:] == ["commit", "live", "committed"]
        assert "discard" not in order

    def test_preflight_failure_aborts_without_canonical_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: pytest.fail("a preflight failure triggered reconciliation"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: order.append("logged"),
        )

        failure = daemon_descriptor_module.CommitPreflightError(
            "descriptor destination could not be inspected"
        )
        with pytest.raises(daemon_descriptor_module.CommitPreflightError):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_error=failure,
            )

        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "logged",
            "aborted",
        ]

    def test_initial_state_preparation_failure_aborts_without_reconciliation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        failure = OSError("account home is unavailable")

        def fail_preparation(root: Path) -> Path:
            order.append("commit")
            raise failure

        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "prepare_daemon_state",
            fail_preparation,
        )
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: pytest.fail("state preparation failure was reconciled"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: order.append("logged"),
        )
        implementation = daemon_owner.daemon_descriptor.commit_prepared

        with pytest.raises(CommitPreflightError) as stopped:
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_implementation=implementation,
            )

        assert stopped.value.__cause__ is failure
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "logged",
            "aborted",
        ]

    def test_definite_commit_failure_is_reported_as_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: (_ for _ in ()).throw(OSError("canonical path is unreadable")),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: order.append("logged"),
        )
        with pytest.raises(IsADirectoryError):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_error=IsADirectoryError("canonical path is a directory"),
            )
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "logged",
            "aborted",
        ]

    def test_the_child_serves_only_after_its_generation_is_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        self._run_serve(tmp_path, monkeypatch, order)
        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "live",
            "committed",
        ]


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


class TestTheHandshakeFrameIsNotPlatformTranslated:
    """The frontend authenticates this frame by comparing exact bytes."""

    def test_the_stream_is_opened_without_newline_translation(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Checked at the argument, because only Windows would translate.

        There is no way to observe the difference on a POSIX host: the default
        writes LF here too. What decides the Windows outcome is the argument
        this passes, so that is what this asserts.
        """
        from linkedin_mcp_server import daemon_owner

        opened: dict[str, object] = {}

        class _Stream:
            def close(self) -> None:
                return None

        def fdopen(_descriptor: int, mode: str, **kwargs: object) -> object:
            opened.update(kwargs)
            opened["mode"] = mode
            return _Stream()

        monkeypatch.setattr(daemon_owner.sys.stdout, "flush", lambda: None)
        monkeypatch.setattr(daemon_owner.os, "dup", lambda _fd: 99)
        monkeypatch.setattr(daemon_owner.os, "dup2", lambda _src, _dst: None)
        monkeypatch.setattr(daemon_owner.os, "fdopen", fdopen)

        stream = daemon_owner._claim_handshake_stream()

        assert stream is not None
        assert opened["newline"] == "\n", (
            "a translated line ending makes a genuine READY unrecognisable"
        )

    def test_a_carriage_return_is_not_a_verdict(self):
        """And the reader stays strict, so the writer has to be exact."""
        from linkedin_mcp_server import daemon_owner

        nonce = "0123456789abcdef" * 4
        frame = f"{daemon_owner.HANDSHAKE} {nonce} {daemon_owner.READY}"

        assert election_module._reported_owner_verdict(
            f"{frame}\n".encode(), nonce
        ) == (daemon_owner.READY, None)
        assert (
            election_module._reported_owner_verdict(f"{frame}\r\n".encode(), nonce)
            is None
        ), "the carriage return stays part of the payload and matches no verdict"
