"""Getting one owner started, and never two.

The properties here are about process lifetime, file descriptor inheritance and
the kernel's arbitration, so most of these spawn real processes. A test that
stayed inside one interpreter could show the code takes the branches it means to
and would not show the thing that matters: that exactly one browser-owning
process exists, and that the lock dies with it.

Four of these pin defects that were live in this code and found by running it:
a failed child read as "somebody is starting" and cost a full deadline, a child
that reported failure kept the lock, a crashed owner's descriptor was handed
back as attachable, and a parent holding the lock past the owner would have
wedged every recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import io
import json
import logging
import math
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
from typing import Any, NoReturn, cast

import pytest
from mcp import MCPError

import linkedin_mcp_server.daemon.discovery as daemon_module
import linkedin_mcp_server.daemon.config as daemon_config
import linkedin_mcp_server.daemon.descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon.election as election_module
import linkedin_mcp_server.daemon.spawn as spawn_module
from linkedin_mcp_server.daemon.core import election as election_core
import linkedin_mcp_server.daemon.owner as daemon_owner
from linkedin_mcp_server import __version__
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon.discovery import Attachment, OwnerLookup, OwnerState
from linkedin_mcp_server.daemon.core.spawn import Attempt, Started
from linkedin_mcp_server.daemon.election import (
    ElectionOutcome,
    Reach,
    obtain_owner,
)
from linkedin_mcp_server.daemon.descriptor import (
    CommitPreflightError,
    build,
    commit_prepared,
    new_instance_id,
    new_token,
    prepare,
)
from linkedin_mcp_server.daemon.lock import DaemonLock, DaemonLockError
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.daemon import version as daemon_version
from linkedin_mcp_server.private_state import harden_directory
from linkedin_mcp_server.process_tree import ProcessTreeError

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = "test-runtime"
_HANDSHAKE_NONCE = "0123456789abcdef" * 4


def _owner_says(nonce: str, *frames: tuple[str, dict[str, object]]) -> bytes:
    """What an owner of this build writes: its sync line and ACK, then *frames*."""
    ack = startup.message(
        startup.ACK,
        nonce,
        protocol=startup.PROTOCOL,
        build=daemon_version.build_identity(),
    )
    return (
        startup.sync_line(nonce)
        + startup.encode(ack)
        + b"".join(
            startup.encode(startup.message(kind, nonce, **fields))
            for kind, fields in frames
        )
    )


def _frames_in(written: bytes) -> list[dict[str, object]]:
    """Every frame a frontend wrote, in order, after any gate release line."""
    if written.startswith(b"release "):
        written = written.split(b"\n", 1)[1]
    stream = io.BytesIO(written)
    frames: list[dict[str, object]] = []
    while True:
        try:
            frames.append(startup.read_frame(stream.read))
        except EOFError:
            return frames


#: A stand-in owner's first lines, run as ``python -c``: the production owner
#: channel, greeted the way the owner greets, so the frontend's ACK check passes
#: and the case reaches the stage it is about.
_GREETED_CHILD = (
    "from linkedin_mcp_server.daemon import startup, version\n"
    "channel = startup.OwnerChannel.claim()\n"
    "nonce, _config = channel.accept(build=version.build_identity())\n"
)

#: What a call given a 0.01s bound may take before the bound is not the thing
#: deciding. A hundred times the argument, and still two orders below the
#: multi-second defaults these paths would otherwise use, so it fails an
#: implementation that ignores its timeout without failing a busy machine. Ten
#: times was too tight: a Windows runner spent 0.141s on thread scheduling
#: alone and failed a bound that had worked correctly.
_BOUNDED_CALL_SECONDS = 1.0

#: How long a test may wait for another thread to *enter* a blocked read under
#: ``-n auto`` CI load. Distinct from ``_BOUNDED_CALL_SECONDS``, which bounds
#: how long a *fast* path may take: stretching that constant would hide
#: regressions. Measured on Windows daemon jobs for #874 / #845.
_THREAD_START_SECONDS = 5.0

#: A bound on every wait below, so a regression fails an assertion instead of
#: hanging the suite.
_PARKED = 5.0

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="drives POSIX process groups, signals, FIFOs or an ungated spawn",
)

#: Set by the manual election soak workflow, which arms ``DontShowUI`` before
#: clearing ``SEM_NOGPFAULTERRORBOX`` in the diagnostic frontend below. Product
#: CI runs the same stress without changing the inherited error mode.
_ELECTION_SOAK = "LINKEDIN_MCP_ELECTION_SOAK"
# Captured before the autouse fixture removes every LINKEDIN* variable.
_ELECTION_SOAK_ENABLED = bool(os.environ.get(_ELECTION_SOAK))


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
    """Give in-process tests their own account home and a fixed runtime.

    Skipped for the tests that spawn real owners. These patches reach only this
    interpreter, and those tests must agree with their children, which take
    their home from ``isolate_daemon_state`` and their runtime from the build.
    They take the ``owner_profile`` fixture instead.
    """
    if "owner_profile" in request.fixturenames:
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


def _lock_is_held(auth_root: Path) -> bool:
    """Whether a contender is refused the daemon lock, as an election would be."""
    contender = DaemonLock(auth_root)
    try:
        return not contender.try_acquire()
    finally:
        contender.release()


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
    prepare(auth_root, descriptor, token)
    commit_prepared(auth_root, descriptor.instance_id)
    return descriptor.instance_id


def _proxy_backend_for(attachment: Attachment):
    """The state object a proxy is built from, around a proved attachment.

    The election's inputs travel with the answer so that a later change can find
    a replacement. These tests only need the answer, and the profile they pass is
    the one the attachment already describes.
    """
    from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend

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


@contextlib.contextmanager
def _a_healthy_owner(profile: Path, config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    """The owner's real HTTP server, on loopback, inside this process.

    Built by ``daemon.owner.create_owner_server``, so the endpoint, its bearer
    check and the MCP server behind it are production's, and it speaks whatever
    protocol era the installed FastMCP does. Only the browser lifespan is stood
    in, so nothing is installed, launched or closed. Yields the attachment a
    frontend would read for it.
    """
    import linkedin_mcp_server.server as server_module

    for name in (
        "initialize_bootstrap",
        "get_runtime_policy",
        "report_retained_browser_revisions_if_ready",
    ):
        monkeypatch.setattr(server_module, name, lambda *_a, **_k: None)

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    for name in (
        "start_background_browser_setup_if_needed",
        "watch_for_handoff_requests",
        "stop_background_browser_setup",
        "close_browser",
    ):
        monkeypatch.setattr(server_module, name, nothing)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    attachment = _attachment_for(profile, config, listener.getsockname()[1])
    server = daemon_owner.create_owner_server(
        config=config,
        token=attachment.token,
        host="127.0.0.1",
        port=attachment.descriptor.port,
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive(), "the owner's server stopped while starting"
            assert time.monotonic() < deadline, "the owner's server never started"
            time.sleep(0.01)
        yield attachment
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


class TestThePublicationProbe:
    """What an owner proves about itself before it publishes a descriptor."""

    def test_the_owner_proves_its_own_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An authenticated round trip against the owner's real server.

        Asked with a ping on the era a default client negotiates, a healthy
        owner fails its own probe and never publishes. The wrong token is the
        other half: a probe that passed whatever it was told would publish an
        endpoint nobody can use.
        """
        profile = _profile(tmp_path)
        config = _config(profile)

        with _a_healthy_owner(profile, config, monkeypatch) as attachment:
            url = attachment.descriptor.url
            asyncio.run(daemon_owner.prove_hop(url, attachment.token))
            with pytest.raises(ExceptionGroup) as refused:
                asyncio.run(daemon_owner.prove_hop(url, new_token()))
            assert refused.group_contains(
                MCPError, match="Server returned an error response", depth=None
            )


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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.REFUSED,
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.REFUSED,
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.ANSWERED,
        )

        assert outcome.worth_connecting
        assert not outcome.started_owner

    def test_a_corpse_is_not_probed_over_and_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The descriptor stays on disk until a live owner overwrites it, so a
        # loop that re-read it would probe the same dead endpoint every pass.
        # Each probe is a network timeout, which is how a fail-fast path turns
        # into a stall.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)
        # A real start would key its state under the account's own home, out of
        # this test's reach. Failing it leaves the dead endpoint the only one.
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )

        probes: list[Attachment] = []

        def never_answers(attachment: Attachment, timeout: float) -> Reach:
            probes.append(attachment)
            # REFUSED, which is what a corpse really produces: its port is
            # closed, so the kernel turns the connection away rather than
            # leaving the probe to time out. A SILENT stub here would be
            # testing a different animal, and the count below would rightly
            # fail.
            return Reach.REFUSED

        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            settlement_seconds=0,
            connect=never_answers,
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
            _attachment_for(profile, config, dead_port),
            election_module._REACHABLE_SECONDS,
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
                _attachment_for(profile, config, frozen.getsockname()[1]),
                election_module._REACHABLE_SECONDS,
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

    def test_a_healthy_owner_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The third answer, from the owner's own server, unstubbed.

        A default FastMCP 4 client negotiates the 2026-07-28 era with this
        owner, and that era has no ping: asked with one, a healthy owner came
        back as ``REFUSED``, which the election reads as a descriptor to bury.
        The two sockets above cannot show that, because neither ever answers.
        A token this owner does not accept still reads as a refusal.
        """
        profile = _profile(tmp_path)
        config = _config(profile)

        with _a_healthy_owner(profile, config, monkeypatch) as attachment:
            answered = election_module._reachable(
                attachment, election_module._REACHABLE_SECONDS
            )
            stranger = election_module._reachable(
                Attachment(descriptor=attachment.descriptor, token=new_token()),
                election_module._REACHABLE_SECONDS,
            )

        assert answered is Reach.ANSWERED, f"a healthy owner read as {answered}"
        assert stranger is Reach.REFUSED, f"a rejected token read as {stranger}"

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

        def slow_to_answer(attachment: Attachment, timeout: float) -> Reach:
            probes.append(attachment)
            return answers[min(len(probes) - 1, len(answers) - 1)]

        # The stalled owner holds the daemon lock. The child owns lock acquisition,
        # so model its ordinary contention verdict directly rather than starting a
        # real subprocess from this isolated in-process state root.
        monkeypatch.setattr(
            spawn_module,
            "_start_owner",
            lambda *a, **k: Attempt.CONTENDED,
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.SILENT,
        )
        refused = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.REFUSED,
        )

        assert silent.attachment_lookup.reason != refused.attachment_lookup.reason
        assert not silent.worth_connecting

    def test_a_refusal_still_buries_on_the_first_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
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
        # A real start would key its state under the account's own home, out of
        # this test's reach. Failing it leaves the buried endpoint the only one.
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )

        probes: list[Attachment] = []

        def refuses_then_would_answer(attachment: Attachment, timeout: float) -> Reach:
            probes.append(attachment)
            return Reach.REFUSED if len(probes) == 1 else Reach.ANSWERED

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            settlement_seconds=0,
            connect=refuses_then_would_answer,
        )

        assert len(probes) == 1, "a refused instance was probed again"
        assert not outcome.worth_connecting

    def test_an_owner_that_never_answers_does_not_outlast_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        # What a real child reports against that lock. A real one would look
        # for it under the account's own home, find none, and leave its state
        # there instead.
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.CONTENDED
        )
        try:
            began = time.monotonic()
            outcome = obtain_owner(
                auth_root,
                profile,
                config,
                deadline_seconds=1.0,
                settlement_seconds=0,
                connect=lambda attachment, timeout: Reach.SILENT,
            )
            elapsed = time.monotonic() - began
        finally:
            held.release()

        assert not outcome.worth_connecting
        # Generous, because the budget is spent inside probes and lock attempts
        # that are not interrupted mid-flight. What it catches is an unbounded
        # loop, which is the only real risk of never burying anything.
        assert elapsed < 15, f"the election ran {elapsed:.1f}s past a 1.0s budget"


@contextlib.contextmanager
def _a_published_owner_on_loopback(
    profile: Path, config: AppConfig, *, serves_the_pin: bool
):
    """A published owner of this build, answering by hand on a loopback socket.

    The answers are a FastMCP owner's, trimmed, on the hop's era and on the
    handshake era. Without *serves_the_pin* it refuses ``server/discover`` at
    the hop's version as an owner whose MCP no longer serves it would, and
    still serves the handshake, which a client negotiating by the SDK's
    default falls back to. Yields the published attachment and every
    JSON-RPC method that reached the owner, in order.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from linkedin_mcp_server.daemon.forwarder import HOP_PROTOCOL_VERSION
    from linkedin_mcp_server.daemon.liveness import HEARTBEAT_PATH

    token = new_token()
    methods: list[str] = []

    class Owner(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def respond(self, status: int, body: object = None) -> None:
            encoded = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self.respond(405)

        def do_DELETE(self) -> None:
            self.respond(405)

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.respond(401)
                return
            if self.path == HEARTBEAT_PATH:
                self.respond(200, {"watched": False})
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "id" not in body:
                self.respond(202)
                return
            method = body["method"]
            methods.append(method)
            modern: dict[str, object] = {
                "ttlMs": 0,
                "cacheScope": "private",
                "resultType": "complete",
            }
            if method == "server/discover" and not serves_the_pin:
                self.respond(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": -32022,
                            "message": "Unsupported protocol version",
                            "data": {
                                "supported": ["2025-11-25"],
                                "requested": HOP_PROTOCOL_VERSION,
                            },
                        },
                    },
                )
                return
            if method == "server/discover":
                result: dict[str, object] = {
                    **modern,
                    "supportedVersions": [HOP_PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
                }
            elif method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "owner", "version": __version__},
                }
            elif method == "tools/list":
                tool = {"name": "probe_echo", "inputSchema": {"type": "object"}}
                result = {"tools": [tool], **(modern if serves_the_pin else {})}
            elif method == "tools/call":
                result = {
                    "content": [{"type": "text", "text": "probe ran"}],
                    "isError": False,
                    "resultType": "complete",
                }
            else:
                self.respond(
                    404,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    },
                )
                return
            self.respond(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Owner)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        auth_root = profile.parent
        attachment = Attachment(
            descriptor=build(
                instance_id=new_instance_id(),
                package_version=__version__,
                runtime_id=_RUNTIME,
                profile=profile,
                host="127.0.0.1",
                port=server.server_port,
                path="/mcp",
                token=token,
                config=config,
                log_path=auth_root / "daemon.log",
            ),
            token=token,
        )
        prepare(auth_root, attachment.descriptor, token)
        commit_prepared(auth_root, attachment.descriptor.instance_id)
        yield attachment, methods
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_PARKED)


async def _call_through_a_frontend(backend: Any) -> Any:
    """One tool call from an external client, through a proxy around *backend*."""
    from fastmcp import Client

    from linkedin_mcp_server.server import create_mcp_server
    from linkedin_mcp_server.server_role import ServerRole

    frontend = create_mcp_server(
        role=ServerRole.PROXY, proxy_backend=backend, tool_timeout=5.0
    )
    async with Client(frontend) as client:
        return await client.call_tool("probe_echo", {}, raise_on_error=False)


class TestTheProbeSpeaksTheHopsVersion:
    """An owner is attached to only if it serves the version every call proposes.

    The probe once negotiated by the SDK's default, which falls back to the
    handshake era when the pinned one is refused, while every forwarded call
    proposes only the pin. An owner of this package version whose MCP served
    only an older era was selected, failed each call before the tool request
    left, and was selected again by the recovery, so every call came back as
    a lost owner. Same package version, different dependencies: a ``uvx``
    resolve beside a locked install.
    """

    def test_an_owner_without_the_pinned_version_is_not_attached_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.forwarder import OwnerFailure

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        # Burying the owner sends the election to start one. The lock is the
        # owner's in production, so a start contends; failing it here keeps
        # the published owner the only one.
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )
        # The recovery's election, real but on a budget a test can wait for.
        real = election_module.obtain_owner

        def briefly(*args: Any, **kwargs: Any) -> ElectionOutcome:
            return real(*args, deadline_seconds=0.5, settlement_seconds=0, **kwargs)

        monkeypatch.setattr(election_module, "obtain_owner", briefly)

        with _a_published_owner_on_loopback(profile, config, serves_the_pin=False) as (
            attachment,
            methods,
        ):
            outcome = briefly(auth_root, profile, config)
            # What a proxy already attached to this generation does when a
            # call fails at entry: an owner error, which buries nothing.
            backend = _proxy_backend_for(attachment)
            recovered = asyncio.run(
                backend.recover(
                    attachment.descriptor.instance_id,
                    classification=OwnerFailure.OWNER_ERROR,
                )
            )
            answer = asyncio.run(_call_through_a_frontend(backend))
            # The owner asks itself the same before publishing, so one whose own
            # dependencies lack the pin never advertises an unusable endpoint.
            with pytest.raises(ExceptionGroup) as refused:
                asyncio.run(
                    daemon_owner.prove_hop(attachment.descriptor.url, attachment.token)
                )
            assert refused.group_contains(
                MCPError, match="Unsupported protocol version", depth=None
            )

        assert not outcome.worth_connecting, outcome.attachment_lookup.reason
        assert recovered.attachment is None, "recovery chose the same owner again"
        assert answer.is_error
        assert "server/discover" in methods, "the owner was never asked"
        assert "tools/call" not in methods
        assert daemon_descriptor_module.read(auth_root) == attachment.descriptor

    def test_the_same_owner_serving_the_pin_is_attached_to_and_runs_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        def never(*_a: Any, **_k: Any) -> NoReturn:
            raise AssertionError("an answering owner was replaced")

        monkeypatch.setattr(spawn_module, "_start_owner", never)

        with _a_published_owner_on_loopback(profile, config, serves_the_pin=True) as (
            attachment,
            methods,
        ):
            outcome = obtain_owner(
                auth_root, profile, config, deadline_seconds=5.0, settlement_seconds=0
            )
            found = outcome.attachment_lookup.attachment
            assert outcome.worth_connecting, outcome.attachment_lookup.reason
            assert found is not None
            answer = asyncio.run(_call_through_a_frontend(_proxy_backend_for(found)))

        assert found.descriptor == attachment.descriptor
        assert not answer.is_error, answer
        assert [c.text for c in answer.content] == ["probe ran"]
        assert methods.count("tools/call") == 1


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

        real_live_lookup = election_module._live_lookup
        passes = 0

        def live_lookup(*args: object, **kwargs: object):
            nonlocal passes
            passes += 1
            return real_live_lookup(*cast(Any, args), **cast(Any, kwargs))

        probes = 0

        def refuse(_attachment: Attachment, timeout: float) -> Reach:
            nonlocal probes
            probes += 1
            return Reach.REFUSED

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(election_module, "_live_lookup", live_lookup)
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )
        # One start, then nothing but backoff for the rest of the budget. That
        # window is where the spin lived: no start is due, so every pass through
        # the loop was pure observation.
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 30.0)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 30.0)

        began = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0.6,
            settlement_seconds=0,
            connect=refuse,
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

        paced = threading.Event()
        abandoned = threading.Event()
        published = threading.Event()
        publishers: list[threading.Thread] = []
        fresh: list[str] = []

        def paced_sleep(seconds: float) -> None:
            paced.set()
            time.sleep(seconds)

        def replace_after_burial(*_args: object, **_kwargs: object) -> Attempt:
            # Stands in for the owner a real start would have produced: the
            # descriptor is replaced once the loop is inside its paced wait, so
            # the wait's first read cannot be what finds it.
            def publish_later() -> None:
                if not paced.wait(_PARKED) or abandoned.is_set():
                    return
                fresh.append(_publish_stale_owner(auth_root, profile, config))
                published.set()

            paced.clear()
            publisher = threading.Thread(target=publish_later, daemon=True)
            publishers.append(publisher)
            publisher.start()
            return Attempt.FAILED

        seen: list[str] = []

        def reach(attachment: Attachment, timeout: float) -> Reach:
            seen.append(attachment.descriptor.instance_id)
            return (
                Reach.REFUSED
                if attachment.descriptor.instance_id == stale
                else (Reach.ANSWERED)
            )

        monkeypatch.setattr(spawn_module, "_start_owner", replace_after_burial)
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 30.0)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 30.0)
        # A long paced wait, so that the answer has to come from *inside* it.
        # At the production 0.2s the loop would come round again on its own and
        # a wait that simply slept would look identical to one that watched.
        monkeypatch.setattr(election_module, "_RETRY_SECONDS", 4.0)
        monkeypatch.setattr(
            election_module,
            "time",
            SimpleNamespace(monotonic=time.monotonic, sleep=paced_sleep),
        )

        began = time.monotonic()
        try:
            outcome = obtain_owner(
                auth_root, profile, config, deadline_seconds=8.0, connect=reach
            )
            # Before the publisher is waited for: the descriptor is visible
            # before its thread returns, and that return is not what is timed.
            elapsed = time.monotonic() - began
            assert published.wait(2.0), "the replacement was never published"
        finally:
            abandoned.set()
            paced.set()
            for publisher in publishers:
                publisher.join(timeout=2.0)
        assert not any(publisher.is_alive() for publisher in publishers)

        assert outcome.worth_connecting, "the replacement generation was never seen"
        assert seen == [stale, fresh[0]]
        # One poll interval past publication, not the whole of the wait it landed
        # in. Sleeping the wait out instead of polling it lands at 4s here.
        assert elapsed < 2.0, f"the replacement took {elapsed:.2f}s to be noticed"


class TestFailingFast:
    def test_owner_start_backoff_begins_after_the_initial_burst(self):
        pacing = election_module._pacing()
        delay = pacing.first_delay
        delays: list[float] = []

        for starts in range(1, 7):
            delay = election_core.start_delay_after(starts, delay, pacing)
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

        def fail(*args: object, **kwargs: object) -> Attempt:
            nonlocal attempts
            attempts += 1
            return Attempt.FAILED

        monkeypatch.setattr(spawn_module, "_start_owner", fail)
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 0.05)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 0.2)

        started = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0.7,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.REFUSED,
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

        def refuse(*args: object, **kwargs: object) -> Attempt:
            nonlocal attempts
            attempts += 1
            raise refusal

        monkeypatch.setattr(spawn_module, "_start_owner", refuse)
        # Paced as the neighbouring test paces it, so a version that retried
        # would get several attempts inside this budget rather than one.
        monkeypatch.setattr(election_module, "_OWNER_START_BURST", 1)
        monkeypatch.setattr(election_module, "_OWNER_START_RETRY_SECONDS", 0.05)
        monkeypatch.setattr(election_module, "_MAX_OWNER_START_RETRY_SECONDS", 0.2)

        started = time.monotonic()
        # Far past the handback bound, so the start is always reached; a slow
        # first descriptor read could spend a 10ms budget before it. The bound
        # below, not the budget, is what this asserts.
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=5.0,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.REFUSED,
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
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _config(profile),
                deadline_seconds=0.08,
                connect=lambda attachment, timeout: Reach.REFUSED,
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

    def test_aborted_start_discards_an_inspection_begun_before_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        first_read_started = threading.Event()
        release_first_read = threading.Event()
        first_read_returned = threading.Event()
        real_inspect = daemon_module._inspect
        inspections = 0

        def inspect(*args: object) -> OwnerLookup:
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                first_read_started.set()
                release_first_read.wait()
                try:
                    return OwnerLookup(state=OwnerState.ABSENT)
                finally:
                    first_read_returned.set()
            return real_inspect(*cast(Any, args))

        def start(
            auth_root: Path, started_config: AppConfig, **_kwargs: object
        ) -> Attempt:
            assert first_read_started.is_set()
            _publish_stale_owner(auth_root, profile, started_config)
            release_first_read.set()
            assert first_read_returned.wait(timeout=1.0)
            return Attempt.ABORTED

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.01)
        monkeypatch.setattr(spawn_module, "_start_owner", start)
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                config,
                deadline_seconds=1.0,
                connect=lambda attachment, timeout: Reach.ANSWERED,
            )
        finally:
            release_first_read.set()

        assert outcome.attachment_lookup.state is OwnerState.ATTACHABLE
        assert inspections == 2

    def test_successful_commit_gets_a_fresh_descriptor_inspection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        blocked = threading.Event()
        published = threading.Event()
        release = threading.Event()
        reader_lock = threading.Lock()
        real_inspect = daemon_module._inspect
        real_inspect_until = daemon_module._DescriptorInspector.inspect_until
        inspections = 0
        active_readers = 0
        max_active_readers = 0
        first_inspector: daemon_module._DescriptorInspector | None = None

        def inspect(*args: object) -> OwnerLookup:
            nonlocal inspections, active_readers, max_active_readers
            with reader_lock:
                inspections += 1
                inspection = inspections
                active_readers += 1
                max_active_readers = max(max_active_readers, active_readers)
                if active_readers > 1:
                    release.set()
            try:
                if inspection == 1:
                    blocked.set()
                    release.wait()
                    return OwnerLookup(state=OwnerState.ABSENT)
                return real_inspect(*cast(Any, args))
            finally:
                with reader_lock:
                    active_readers -= 1

        # The retained inspector releases its old reader before the post-publish
        # lookup. A replacement instead starts a second native reader, whose entry
        # above releases the first and records the overlap rather than deadlocking.
        def inspect_until(
            self: daemon_module._DescriptorInspector, *, timeout: float
        ) -> OwnerLookup:
            nonlocal first_inspector
            if first_inspector is None:
                first_inspector = self
            elif published.is_set() and self is first_inspector:
                release.set()
            return real_inspect_until(self, timeout=timeout)

        def start(
            auth_root: Path, started_config: AppConfig, **_kwargs: object
        ) -> Attempt:
            _publish_stale_owner(auth_root, profile, started_config)
            published.set()
            return Attempt.STARTED

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(
            daemon_module._DescriptorInspector, "inspect_until", inspect_until
        )
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.01)
        monkeypatch.setattr(spawn_module, "_start_owner", start)
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                config,
                deadline_seconds=1.0,
                connect=lambda attachment, timeout: Reach.ANSWERED,
            )
        finally:
            release.set()

        assert blocked.is_set()
        assert outcome.worth_connecting
        assert inspections == 2
        assert max_active_readers == 1

    def test_a_frontend_that_lost_the_race_takes_over_when_the_winner_dies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The recovery this loop exists for, and the one a plain "wait for a
        # descriptor" cannot provide. A's child wins the lock and then dies
        # without ever publishing; B lost the race and is waiting.
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
            auth_root: Path, config: AppConfig, *, timeout: float, inspector: object
        ) -> Attempt:
            contender = DaemonLock(auth_root)
            if not contender.try_acquire():
                return Attempt.CONTENDED
            took_over.append(time.monotonic())
            _publish_stale_owner(auth_root, profile, config)
            contender.release()
            return Attempt.STARTED

        monkeypatch.setattr(spawn_module, "_start_owner", contend)

        began = time.monotonic()
        try:
            outcome = obtain_owner(
                auth_root,
                profile,
                config,
                deadline_seconds=2,
                connect=lambda attachment, timeout: Reach.ANSWERED,
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
        monkeypatch.setattr(spawn_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            spawn_module, "terminate_process_group", exhaust_group_budget
        )

        spawn_module._stop_child(process)

        assert waits == [0.0]

    @_POSIX_ONLY
    def test_a_child_that_never_reads_its_input_does_not_block_the_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The handshake timeout is reached only if the spawn gets that far. A
        # write to the child's pipe comes first, and a child that neither reads
        # nor exits once blocked such a write indefinitely, before any budget
        # applied, with both processes holding the lock: reproduced with a
        # 10 MiB bypass list and a child that only sleeps, the outer process
        # timeout fired and the wait was never entered. Every startup write is
        # now bounded by the caller's wait, and frames by the protocol's size
        # limit; this is the child that never reads at all.
        profile = _profile(tmp_path)
        config = _config(profile)
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

        monkeypatch.setattr(spawn_module.subprocess, "Popen", deaf)

        # Timed where it happens. The total cannot carry this: see the bound
        # below for what it costs and what it buys.
        stopping: list[float] = []
        stop = spawn_module._stop_child

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

        monkeypatch.setattr(spawn_module, "_stop_child", timed)

        began = time.monotonic()
        attempt = spawn_module._start_owner(
            auth_root,
            config,
            timeout=0.5,
            inspector=daemon_module._DescriptorInspector(auth_root, profile, config),
        )
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
            # the total also contains forking an interpreter, which is not this
            # code's to bound.
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
            # never took its configuration is not coming up, and it may already
            # hold the lock, so it is stopped rather than left alone.
            assert attempt is Attempt.FAILED

            # And the lock is free. This is the half that matters:
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
                if thread.name.startswith("daemon-startup-"):
                    thread.join(timeout=1)
            assert all(
                sleeper.stdin is None or sleeper.stdin.closed for sleeper in sleepers
            ), "the startup pipe was left open"
            assert not any(
                thread.name.startswith("daemon-startup-")
                for thread in threading.enumerate()
            ), "a startup thread outlived the child it was talking to"
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
            if command[1:4] != ["-P", "-m", "linkedin_mcp_server.daemon.owner"]:
                return real(command, **kwargs)
            child = real(
                [
                    command[0],
                    "-c",
                    _GREETED_CHILD + "import time\n"
                    "channel.send(startup.VERDICT, nonce, status=startup.FAILED)\n"
                    "time.sleep(600)\n",
                ],
                **kwargs,
            )
            children.append(child)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", reports_failure)

        try:
            attempt = spawn_module._start_owner(
                auth_root,
                config,
                timeout=5.0,
                inspector=daemon_module._DescriptorInspector(
                    auth_root, profile, config
                ),
            )
            assert attempt is Attempt.FAILED
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
            if command[1:4] != ["-P", "-m", "linkedin_mcp_server.daemon.owner"]:
                return real(command, **kwargs)
            script = (
                _GREETED_CHILD + "import pathlib, subprocess, sys, time\n"
                "grandchild = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(600)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(grandchild.pid))\n"
                "channel.send(startup.VERDICT, nonce, status=startup.FAILED)\n"
                "time.sleep(600)\n"
            )
            child = real([command[0], "-c", script], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", reports_failure)

        try:
            attempt = spawn_module._start_owner(
                auth_root,
                config,
                timeout=5.0,
                inspector=daemon_module._DescriptorInspector(
                    auth_root, profile, config
                ),
            )
            assert attempt is Attempt.FAILED
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
        # bought was a child holding the lock while never serving —
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

        # The greeting fits the pipe, so the write succeeds and the silence
        # happens at the handshake. A child that never reads reaches the same
        # wedge by blocking a write instead, and is covered separately.
        real = subprocess.Popen
        started: list[subprocess.Popen[Any]] = []

        def mute(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            child = real([command[0], "-c", "import time; time.sleep(600)"], **kwargs)
            started.append(child)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", mute)

        began = time.monotonic()
        attempt = spawn_module._start_owner(
            auth_root,
            config,
            timeout=0.5,
            inspector=daemon_module._DescriptorInspector(auth_root, profile, config),
        )
        elapsed = time.monotonic() - began

        try:
            assert attempt is Attempt.FAILED

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
            for thread in threading.enumerate():
                if thread.name.startswith("daemon-startup-"):
                    thread.join(timeout=1)
            assert not any(
                thread.name.startswith("daemon-startup-")
                for thread in threading.enumerate()
            )

            # And on time. The timeout bounds the *wait*, and the cleanup after
            # it used to hand that bound straight back: `child.stdout.close()`
            # waits on the reader thread's I/O lock, so it blocked for as long
            # as the child stayed silent — 29.27s against a 30s sleeper.
            #
            # Five, which is ten times the budget and catches only the 29s
            # cleanup defect above. It is not a check on `_spawn`'s
            # one-budget-across-both-halves rule, though it sits close enough to
            # read as one: measured, handing `_await_prepared` a fresh 0.5s instead
            # of the remainder takes the call to 0.98s and passes this. The
            # budget test below controls that distinction directly.
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

        def contended(*args: object, **kwargs: object) -> Attempt:
            nonlocal attempts
            attempts += 1
            return Attempt.CONTENDED

        monkeypatch.setattr(spawn_module, "_start_owner", contended)

        started = time.monotonic()
        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=1.0,
            connect=lambda attachment, timeout: Reach.REFUSED,
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
import os
import sys
import time
from pathlib import Path

# A frontend that dies natively says nothing otherwise. Measured on a Windows
# runner at the declared floor: one of eight exited 0xC0000005 with an empty
# stderr, and the assertion could only report the number. This turns the next
# one into a stack that names where it happened.
faulthandler.enable()

# A native crash here reaches no debugger while `SEM_NOGPFAULTERRORBOX` is in
# the process error mode: `UnhandledExceptionFilter` returns at once, so there
# is no dump, no `AeDebug` and no event-log entry, and the process just exits
# carrying the 0xC0000005 this test reports. The bit is inherited, not set
# here. Clear only that one and leave the rest, `SEM_FAILCRITICALERRORS`
# included. See docs/windows-crash-dumps.md.
#
# Only under the soak variable, because clearing the bit turns crash reporting
# back on and the no-dialog setting that makes that safe is armed by the soak
# workflow alone. Nineteen ordinary tests run this same program through
# `_run_frontend`, and the Windows CI job that runs several of them does not
# set `DontShowUI`; leaving them on the default keeps a fault there exiting
# rather than waiting for someone to click.
if sys.platform == "win32" and os.environ.get("__ELECTION_SOAK__"):
    import ctypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetErrorMode.restype = ctypes.c_uint
    _kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
    _kernel32.SetErrorMode.restype = ctypes.c_uint
    _SEM_NOGPFAULTERRORBOX = 0x0002
    _kernel32.SetErrorMode(_kernel32.GetErrorMode() & ~_SEM_NOGPFAULTERRORBOX)

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon.election import obtain_owner
from linkedin_mcp_server.daemon.lock import DaemonLock
from linkedin_mcp_server.profile_claim import ensure_profile_claim

profile = Path(sys.argv[1])
# Optional launch barrier: argv[2]=ready_dir, argv[3]=slot,
# argv[4]=timeout_seconds (default 60). All siblings announce ready then wait
# for ``go`` so the election stampede is simultaneous rather than staggered
# (#874 / #881).
if len(sys.argv) >= 4:
    ready_dir = Path(sys.argv[2])
    slot = sys.argv[3]
    barrier_timeout = float(sys.argv[4]) if len(sys.argv) >= 5 else 60.0
    (ready_dir / f"ready-{slot}").write_text("1", encoding="utf-8")
    go = ready_dir / "go"
    barrier_deadline = time.monotonic() + barrier_timeout
    while not go.exists() and time.monotonic() < barrier_deadline:
        time.sleep(0.01)
    # Timed out without ``go``: the parent is failing the barrier and will
    # kill frontends. Entering obtain_owner here can spawn a detached owner
    # that fixture cleanup then misses (#881).
    if not go.exists():
        raise SystemExit("launch barrier timed out waiting for go signal")
auth_root = profile.parent
ensure_profile_claim(profile, claim_anyway=True)
config = AppConfig()
config.browser.user_data_dir = str(profile)

outcome = obtain_owner(auth_root, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
# Asked from *this* process, which is the frontend: it must not hold the lock.
print(json.dumps({
    "state": outcome.attachment_lookup.state.value,
    # Which refusal, not merely that there was one. There are five paths to
    # INCOMPATIBLE across `daemon/discovery.py` and `daemon/election.py`, and a failure
    # recorded without this cannot say which fired: one such failure in a full
    # suite run left nothing to distinguish a runtime mismatch from a profile
    # mismatch, and the state alone made it look like flake either way.
    "reason": outcome.attachment_lookup.reason,
    "started": outcome.started_owner,
    "pid": attachment.descriptor.pid if attachment else None,
    "url": attachment.descriptor.url if attachment else None,
    "frontend_holds_lock": DaemonLock(auth_root).try_acquire(),
}))
""".replace("__ELECTION_SOAK__", _ELECTION_SOAK)


def _inspect_owner_environment() -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    if _ELECTION_SOAK_ENABLED:
        environment[_ELECTION_SOAK] = "1"
    return environment


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
from linkedin_mcp_server.daemon.election import obtain_owner
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
def owner_profile(tmp_path: Path):
    """A profile for the tests whose owner production code starts.

    The owner keeps its state under the temporary account home that
    ``isolate_daemon_state`` hands every process, as do the frontends here.
    """
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    yield profile

    # Owners are detached, so an assertion that fails before its stop() would
    # otherwise leave a server running against the developer's machine.
    try:
        published = daemon_descriptor_module.read(profile.parent)
    except Exception:
        published = None
    if published is not None:
        _stop(published.pid)


def _run_frontend(profile: Path) -> dict[str, object]:
    """Elect an owner from a separate interpreter, as a real client would."""
    import json

    result = subprocess.run(
        [sys.executable, "-c", _INSPECT_OWNER, str(profile)],
        capture_output=True,
        text=True,
        env=_inspect_owner_environment(),
        cwd=_REPO_ROOT,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _both_ends(text: str, *, head: int = 3000, tail: int = 2000) -> str:
    """Keep a long diagnostic's beginning as well as its end.

    A tail slice is the wrong half for a native crash: ``faulthandler`` prints
    the exception and the faulting thread *first*, so #838 has twice been
    reported with everything below it and the fault location cut off.
    """
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n[... {dropped} characters ...]\n{text[-tail:]}"


def _reap_frontends(frontends: list[subprocess.Popen[str]]) -> None:
    """Kill direct frontends together, then reap them without reading pipes."""
    for frontend in frontends:
        if frontend.poll() is None:
            frontend.kill()
    for frontend in frontends:
        frontend.wait(timeout=30)
        # A timed-out Windows communicate() leaves its pipe readers alive. Closing
        # their buffered streams can wait on those readers without a bound.
        stdout_thread = getattr(frontend, "stdout_thread", None)
        if frontend.stdout is not None and (
            stdout_thread is None or not stdout_thread.is_alive()
        ):
            frontend.stdout.close()
        stderr_thread = getattr(frontend, "stderr_thread", None)
        if frontend.stderr is not None and (
            stderr_thread is None or not stderr_thread.is_alive()
        ):
            frontend.stderr.close()


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


def test_a_native_crash_report_keeps_the_faulting_thread():
    # Twice now (#838) a Windows access violation has been reported with the
    # fault cut off, because the assertion kept the last 2000 characters and
    # faulthandler prints the faulting thread before every other one.
    crash = (
        "Windows fatal exception: access violation\n\n"
        "Current thread 0x00001734 (most recent call first):\n"
        '  File "daemon_election.py", line 706 in collect\n'
        + "".join(
            f'  File "threading.py", line {line} in run\n' for line in range(4000)
        )
        + "the last line of the report\n"
    )

    kept = _both_ends(crash)

    assert "Current thread 0x00001734" in kept
    assert "line 706 in collect" in kept
    assert kept.endswith("the last line of the report\n")
    assert len(kept) < len(crash)


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


class TestWindowsAtomicOwnerHandoff:
    def test_owner_requests_breakaway_from_the_host_job(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", True)
        monkeypatch.setattr(
            spawn_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x1, raising=False
        )
        monkeypatch.setattr(
            spawn_module.subprocess, "DETACHED_PROCESS", 0x2, raising=False
        )
        monkeypatch.setattr(
            spawn_module.subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x4, raising=False
        )

        assert spawn_module._detachment_flags() == 0x7

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
        committed: Started,
        validation_error: BaseException | None = None,
        settlement: Started | None = None,
    ) -> tuple[Started, list[str]]:
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
            assert target[-4:-2] == ["-m", "linkedin_mcp_server.daemon.owner"]
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

        class _Channel:
            """The startup channel, stubbed so no thread is started here.

            Only the ordering is recorded. Where the greeting and the frames sit
            relative to the Job is what this class is about; the channel's own
            behaviour is pinned in ``tests/test_daemon_startup.py``.
            """

            def __init__(self, process: object, nonce: str) -> None:
                assert process is child
                assert nonce == handshake_nonce
                self.nonce = nonce
                events.append("channel")

            def close(self) -> None:
                pass

        def greet(channel: object, *, timeout: float) -> None:
            assert isinstance(channel, _Channel)
            assert timeout == 1.0
            events.append("hello")

        def hand_over(channel: object, sent: AppConfig, *, timeout: float) -> None:
            assert isinstance(channel, _Channel)
            assert sent is config
            assert timeout <= 1.0
            events.append("config")

        def prepared(channel: object, *, timeout: float) -> str:
            assert isinstance(channel, _Channel)
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

        def send_commit(channel: object, instance: str, *, timeout: float) -> None:
            assert isinstance(channel, _Channel)
            assert instance == instance_id
            events.append("commit")

        def await_committed(channel: object, *, timeout: float) -> object:
            assert isinstance(channel, _Channel)
            events.append(
                startup.COMMITTED if committed is Started.YES else committed.value
            )
            return committed

        monkeypatch.setattr(
            spawn_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        monkeypatch.setattr(spawn_module, "WindowsJob", _Job)
        monkeypatch.setattr(startup, "FrontendChannel", _Channel)
        monkeypatch.setattr(spawn_module, "release_nonce", lambda: "nonce")
        monkeypatch.setattr(spawn_module, "new_nonce", make_handshake_nonce)
        monkeypatch.setattr(spawn_module, "windows_gate_command", gate)
        monkeypatch.setattr(spawn_module.subprocess, "Popen", popen)
        monkeypatch.setattr(spawn_module, "release_windows_gate", release)
        monkeypatch.setattr(spawn_module, "_detachment_flags", lambda: 0)
        monkeypatch.setattr(spawn_module, "_greet", greet)
        monkeypatch.setattr(spawn_module, "_hand_over_config", hand_over)
        monkeypatch.setattr(spawn_module, "_await_prepared", prepared)
        monkeypatch.setattr(
            spawn_module, "_resolve_profile_until", lambda path, timeout: profile
        )
        monkeypatch.setattr(spawn_module, "_validate_prepared_until", validate)
        monkeypatch.setattr(spawn_module, "_send_commit", send_commit)
        monkeypatch.setattr(spawn_module, "_await_committed", await_committed)
        monkeypatch.setattr(spawn_module, "_report_child_failure", lambda report: None)
        monkeypatch.setattr(spawn_module, "_reap", lambda process: None)
        monkeypatch.setattr(
            spawn_module,
            "_discard_prepared_until",
            lambda *args, **kwargs: events.append("discard"),
        )
        if settlement is not None:
            monkeypatch.setattr(
                spawn_module,
                "_settle_commit_result",
                lambda *args, **kwargs: (events.append("settle"), settlement)[1],
            )

        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )
        return outcome, events

    def test_success_closes_only_after_the_committed_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(tmp_path, monkeypatch, committed=Started.YES)

        assert outcome is Started.YES
        assert events == [
            "create-job",
            "build-gate",
            "start-gate",
            "create-handshake-nonce",
            # The Job is assigned before the gate is released, so no owner code
            # exists until the parent owns the Job, and the channel takes the
            # child's pipes only after the release line has gone through them.
            "assign-gate",
            "release-gate",
            "channel",
            "hello",
            "config",
            "prepared",
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

        spawn_module._stop_child(
            cast(Any, _Child()), windows_job=cast(Any, _Job()), assigned=False
        )

        assert events == [
            "close-gate-stdin",
            "wait-1",
            "kill-gate",
            "close-job",
            "wait-2",
        ]

    def test_an_uncertain_settlement_closes_without_termination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(
            tmp_path,
            monkeypatch,
            committed=Started.STILL_TRYING,
            settlement=Started.UNCERTAIN,
        )

        assert outcome is Started.UNCERTAIN
        assert events[-3:] == ["still_trying", "settle", "close-parent-job"]
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

        spawn_module._stop_child(
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
            spawn_module._stop_child(
                cast(Any, _Child()), windows_job=cast(Any, _Job()), assigned=True
            )

        assert events[-1] == "close-job"

    def test_precommit_failure_releases_gate_handle_before_job_drain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, events = self._run(
            tmp_path,
            monkeypatch,
            committed=Started.NO,
            validation_error=TimeoutError(),
        )

        assert outcome is Started.NO
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
        outcome, events = self._run(tmp_path, monkeypatch, committed=Started.ABORTED)

        assert outcome is Started.ABORTED
        assert events[-5:] == [
            "aborted",
            "terminate-job",
            "wait-child",
            "release-gate-handle",
            "drain-job",
        ]
        assert "close-parent-job" not in events


@_POSIX_ONLY
class TestSpawnCleanupBoundary:
    """Everything after Popen returns is the caller's to clean up."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX child stop path")
    def test_a_diagnosis_that_cannot_start_stops_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A host out of threads must not be left holding a running owner.

        The bootstrap diagnosis starts one, and it is created after the spawn,
        so its failure is the first that can strand a child: nothing would send
        the HELLO it waits its whole handover timeout for.
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

        def refuse(_stream: object, *, handshake_nonce: str) -> object:
            assert handshake_nonce
            raise RuntimeError("can't start new thread")

        def stop(pid: int, **_kwargs: object) -> bool:
            stopped.append(pid)
            return True

        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(spawn_module, "_BootstrapReport", refuse)
        monkeypatch.setattr(spawn_module, "terminate_process_group", stop)

        with pytest.raises(RuntimeError, match="start new thread"):
            spawn_module._spawn(
                profile.parent,
                config,
                timeout=5.0,
                inspector=daemon_module._DescriptorInspector(
                    profile.parent, profile, config
                ),
            )

        assert stopped == [child.pid], "the child nobody could configure was stopped"
        assert child.returncode is not None, "and collected"


def _await_fixture_imports(child: subprocess.Popen[Any], imported: Path) -> None:
    """Hold a fixture owner's spawn until its interpreter has imported the server.

    Startup's bound is for the owner, not for the interpreter loading the
    package under a loaded test run, which alone can spend all five seconds
    before ``main`` runs the behaviour a test is about.
    """
    deadline = time.monotonic() + 30.0
    while not imported.exists():
        assert child.poll() is None, "the fixture owner exited during its imports"
        assert time.monotonic() < deadline, "the fixture owner never finished importing"
        time.sleep(0.01)


class TestAtomicStartupCommit:
    @pytest.fixture(autouse=True)
    def _stop_fake_groups(self, monkeypatch: pytest.MonkeyPatch):
        def stop(*args: object, child: object | None = None, **kwargs: object) -> bool:
            assert child is not None
            cast(Any, child).kill()
            return True

        monkeypatch.setattr(spawn_module, "terminate_process_group", stop)
        monkeypatch.setattr(spawn_module, "new_nonce", lambda: _HANDSHAKE_NONCE)

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
            self.instance_id = instance_id
            #: Kept beside ``stdin`` because the handover takes the stream out
            #: of the process object, leaving this the only handle on it.
            self.input = TestAtomicStartupCommit._Input()
            self.stdin: io.BytesIO | None = self.input
            #: An owner's answers: PREPARED, then *final* as its verdict, or
            #: nothing after PREPARED when *final* is empty.
            frames: list[tuple[str, dict[str, object]]] = [
                (startup.PREPARED, {"instance": instance_id})
            ]
            if final:
                frames.append((startup.VERDICT, {"status": final}))
            self.stdout = io.BytesIO(_owner_says(_HANDSHAKE_NONCE, *frames))
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
            # Strict: production clamps to ``_STOP_CHILD_SECONDS`` before this
            # call (#874). A ULP overshoot must not be absorbed here — that
            # would let the clamp be deleted without failing any test.
            assert timeout <= spawn_module._STOP_CHILD_SECONDS
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

        monkeypatch.setattr(spawn_module, "WindowsJob", _Injected)
        return created

    def _spawn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        validate: object,
        final: str = "committed",
    ) -> tuple[object, _Child, list[str]]:
        profile = _profile(tmp_path)
        config = _config(profile)
        instance_id = new_instance_id()
        child = self._Child(instance_id, final)
        events: list[str] = []

        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
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

        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
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
            (daemon_owner.BOOTSTRAP_LOCK, "could not take ownership"),
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
        spawn_module._report_child_failure(spawn_module._BootstrapReport(stream))

        assert message in caplog.text
        assert secret not in caplog.text

    @pytest.mark.parametrize(
        ("version", "nonce", "path"),
        [
            ("2", _HANDSHAKE_NONCE, "/tmp/daemon.log"),
            (daemon_owner.BOOTSTRAP_LOG_HINT_VERSION, "wrong-nonce", "/tmp/daemon.log"),
            (
                daemon_owner.BOOTSTRAP_LOG_HINT_VERSION,
                _HANDSHAKE_NONCE,
                "relative/daemon.log",
            ),
            (
                daemon_owner.BOOTSTRAP_LOG_HINT_VERSION,
                _HANDSHAKE_NONCE,
                "/tmp/not-daemon.log",
            ),
            (
                daemon_owner.BOOTSTRAP_LOG_HINT_VERSION,
                _HANDSHAKE_NONCE,
                "/tmp/\x1b/daemon.log",
            ),
            (
                daemon_owner.BOOTSTRAP_LOG_HINT_VERSION,
                _HANDSHAKE_NONCE,
                "/"
                + "x" * daemon_owner.BOOTSTRAP_LOG_HINT_MAX_PATH_BYTES
                + "/daemon.log",
            ),
        ],
    )
    def test_invalid_log_hints_fall_back_to_the_generic_diagnosis(
        self,
        version: str,
        nonce: str,
        path: str,
        caplog: pytest.LogCaptureFixture,
    ):
        stream = io.BytesIO(
            (
                f"{daemon_owner.BOOTSTRAP_PREFIX} "
                f"{daemon_owner.BOOTSTRAP_ATTACHED}\n"
                f"{daemon_owner.BOOTSTRAP_LOG_HINT_PREFIX} "
                f"{version} {nonce} {path}\n"
            ).encode()
        )

        spawn_module._report_child_failure(
            spawn_module._BootstrapReport(stream, handshake_nonce=_HANDSHAKE_NONCE)
        )

        assert "The daemon could not start; inspect the daemon log" in caplog.text
        assert "inspect the daemon log at" not in caplog.text

    def test_log_hint_requires_a_complete_bounded_record(
        self, caplog: pytest.LogCaptureFixture
    ):
        path = "/tmp/daemon.log"
        stream = io.BytesIO(
            (
                f"{daemon_owner.BOOTSTRAP_PREFIX} "
                f"{daemon_owner.BOOTSTRAP_ATTACHED}\n"
                f"{daemon_owner.BOOTSTRAP_LOG_HINT_PREFIX} "
                f"{daemon_owner.BOOTSTRAP_LOG_HINT_VERSION} "
                f"{_HANDSHAKE_NONCE} {path}"
            ).encode()
        )

        spawn_module._report_child_failure(
            spawn_module._BootstrapReport(stream, handshake_nonce=_HANDSHAKE_NONCE)
        )

        assert "inspect the daemon log at" not in caplog.text

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
        monkeypatch.setattr(spawn_module, "_FAILURE_VERDICT_SECONDS", 30.0)
        reader_fd, writer_fd = os.pipe()
        closed = threading.Lock()
        opened = [writer_fd]

        def close_the_write_end() -> None:
            with closed:
                if opened:
                    os.close(opened.pop())

        stream = os.fdopen(reader_fd, "rb", buffering=0)
        report = spawn_module._BootstrapReport(stream)
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
        report = spawn_module._BootstrapReport(cast(Any, stream))
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

        monkeypatch.setattr(spawn_module, "terminate_process_group", stop_group)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            spawn_module._stop_child(child)

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
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            spawn_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.NO
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
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            spawn_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(failure),
        )
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda root, candidate: discarded.append(candidate),
        )

        config = _config(profile)
        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.NO
        assert child.killed
        assert discarded == [instance_id]

    def test_configuration_failure_consumes_a_released_child_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        child = self._Child(new_instance_id())
        child.stdout = io.BytesIO(
            _owner_says(_HANDSHAKE_NONCE, (startup.VERDICT, {"status": startup.RETRY}))
        )
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            spawn_module,
            "_hand_over_config",
            lambda *a, **k: (_ for _ in ()).throw(BrokenPipeError()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        config = _config(profile)
        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.RETRY
        assert child.killed

    def test_interrupt_before_prepared_stops_the_lock_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        child = self._Child(new_instance_id())
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            spawn_module,
            "_await_prepared",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        with pytest.raises(KeyboardInterrupt):
            spawn_module._spawn(
                profile.parent,
                config,
                timeout=1.0,
                inspector=daemon_module._DescriptorInspector(
                    profile.parent, profile, config
                ),
            )

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

        assert outcome is Started.ABORTED
        assert child.killed
        assert events == ["discard"]

    def test_drain_budget_tolerates_float_ulp_over_stop_child_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A constant monotonic clock can round the remaining budget upward."""
        seen: list[float] = []

        class _Child:
            pid = 4242
            stdin = None
            returncode: int | None = None

            def wait(self, timeout: float) -> int:
                self.returncode = 1
                return 1

        class _Job:
            def terminate(self) -> None:
                return None

            def release_popen_handle(self, process: object) -> None:
                return None

            def wait_until_empty(self, *, timeout: float) -> None:
                seen.append(timeout)
                assert timeout <= spawn_module._STOP_CHILD_SECONDS

        now = math.nextafter(256.0, 0.0)
        unclamped = (now + spawn_module._STOP_CHILD_SECONDS) - now
        assert unclamped == 2.0000000000000284
        assert unclamped > spawn_module._STOP_CHILD_SECONDS
        monkeypatch.setattr(
            spawn_module, "time", SimpleNamespace(monotonic=lambda: now)
        )

        spawn_module._stop_child(
            cast(Any, _Child()), windows_job=cast(Any, _Job()), assigned=True
        )

        assert seen == [spawn_module._STOP_CHILD_SECONDS]

    @pytest.mark.parametrize(
        ("wait_at", "drain_at", "wait_budget", "drain_budget"),
        [
            (100.5, 101.25, 1.5, 0.75),
            (100.5, 102.25, 1.5, 0.0),
            (102.5, 103.0, 0.0, 0.0),
        ],
    )
    def test_assigned_job_wait_and_drain_share_the_stop_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        wait_at: float,
        drain_at: float,
        wait_budget: float,
        drain_budget: float,
    ):
        child = self._Child("prepared")
        job = self._Job("owner")
        job.assign_popen(child)
        seen: list[tuple[str, float]] = []

        def wait(timeout: float) -> int:
            seen.append(("wait", timeout))
            return self._Child.wait(child, timeout=timeout)

        def drain(*, timeout: float) -> None:
            seen.append(("drain", timeout))
            self._Job.wait_until_empty(job, timeout=timeout)

        times = iter([100.0, wait_at, drain_at])
        monkeypatch.setattr(
            spawn_module, "time", SimpleNamespace(monotonic=lambda: next(times))
        )
        monkeypatch.setattr(child, "wait", wait)
        monkeypatch.setattr(job, "wait_until_empty", drain)

        spawn_module._stop_child(
            cast(Any, child), windows_job=cast(Any, job), assigned=True
        )

        assert seen == [("wait", wait_budget), ("drain", drain_budget)]
        assert job.steps == ["assign", "terminate", "release-handle", "drain"]

    def test_prepared_read_timeout_stops_without_waiting_for_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            spawn_module,
            "_validate_prepared_until",
            lambda *a, **k: (_ for _ in ()).throw(TimeoutError()),
        )
        monkeypatch.setattr(
            spawn_module,
            "_await_committed",
            lambda *a, **k: pytest.fail("an unauthorized child cannot commit"),
        )

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
        )

        assert outcome is Started.NO
        assert child.killed
        assert startup.COMMIT not in [
            frame["type"] for frame in _frames_in(child.input.written)
        ]
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

        assert outcome is Started.RETRY
        assert child.killed
        assert events == ["discard"]

    def test_a_malformed_verdict_after_unusable_state_still_stops_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The child prepared, validation failed, and its verdict is not even a
        # string. No COMMIT went out, so it must not keep the daemon lock.
        def missing(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError("prepared generation expired")

        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=missing,
            final=cast(Any, ["retry"]),
        )

        assert outcome is Started.ABORTED
        assert child.killed
        assert startup.COMMIT not in [
            frame["type"] for frame in _frames_in(child.input.written)
        ]

    def test_only_the_lock_holding_child_commits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        outcome, child, events = self._spawn(
            tmp_path,
            monkeypatch,
            validate=lambda *a, **k: None,
        )

        assert outcome is Started.YES
        # The whole exchange, in order and authenticated: the greeting, the
        # configuration only after the ACK, and the COMMIT naming the generation
        # this child prepared and nothing else.
        written = _frames_in(child.input.written)
        assert [frame["type"] for frame in written] == [
            startup.HELLO,
            startup.CONFIG,
            startup.COMMIT,
        ]
        assert all(frame["nonce"] == _HANDSHAKE_NONCE for frame in written)
        assert written[2]["instance"] == child.instance_id
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

        assert outcome is Started.RETRY
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

        assert outcome is Started.ABORTED
        assert child.killed
        assert events == []

    def test_parent_delegates_lock_and_log_io_to_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        calls: list[Path] = []

        def spawn(
            auth_root: Path,
            config: AppConfig,
            *,
            timeout: float,
            inspector: object,
        ) -> object:
            calls.append(auth_root)
            return Started.YES

        monkeypatch.setattr(spawn_module, "_spawn", spawn)
        monkeypatch.setattr(
            spawn_module,
            "DaemonLock",
            lambda root: pytest.fail("the parent constructed the daemon lock"),
            raising=False,
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: pytest.fail("the parent derived the daemon log path"),
        )

        config = _config(profile)
        outcome = spawn_module._start_owner(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Attempt.STARTED
        assert calls == [profile.parent]

    @_POSIX_ONLY
    def test_attached_child_failure_reports_the_actual_log_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        profile = _profile(tmp_path)
        auth_root = profile.parent
        home = tmp_path / "home"
        monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)
        log_path = daemon_owner.daemon_log_path(auth_root)
        bootstrap = tmp_path / "failing_owner.py"
        imported = tmp_path / "owner-imported"
        bootstrap.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from linkedin_mcp_server.daemon import descriptor as daemon_descriptor\n"
            "from linkedin_mcp_server.daemon import owner as daemon_owner\n"
            "daemon_descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "class Lock:\n"
            "    def release(self):\n"
            "        pass\n"
            "daemon_owner._take_lock = lambda *args, **kwargs: Lock()\n"
            "def fail_logging(**kwargs):\n"
            "    raise RuntimeError('failed after log attachment')\n"
            "daemon_owner.configure_logging = fail_logging\n"
            "Path(sys.argv[2]).write_text('imported')\n"
            "raise SystemExit(daemon_owner.main([]))\n"
        )
        children: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def capture(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[-2:] == ["-m", "linkedin_mcp_server.daemon.owner"]:
                command = [command[0], str(bootstrap), str(home), str(imported)]
            child = real(command, **kwargs)
            children.append(child)
            _await_fixture_imports(child, imported)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", capture)
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda _root: pytest.fail("the parent derived the daemon log path"),
        )
        try:
            config = _config(profile)
            outcome = spawn_module._start_owner(
                auth_root,
                config,
                timeout=5.0,
                inspector=daemon_module._DescriptorInspector(
                    auth_root, profile, config
                ),
            )

            assert outcome is Attempt.FAILED
            assert log_path.is_file(), "the child did not attach its diagnostic log"
            assert repr(str(log_path)) in caplog.text
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=30)

    @_POSIX_ONLY
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
        imported = tmp_path / "owner-imported"
        home = daemon_descriptor_module._account_home()
        bootstrap.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from linkedin_mcp_server.daemon import descriptor as daemon_descriptor\n"
            "from linkedin_mcp_server.daemon import owner as daemon_owner\n"
            "daemon_descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "attach = daemon_owner._attach_daemon_log\n"
            "def marked(root):\n"
            "    Path(sys.argv[2]).write_text('opening')\n"
            "    return attach(root)\n"
            "daemon_owner._attach_daemon_log = marked\n"
            "Path(sys.argv[3]).write_text('imported')\n"
            "raise SystemExit(daemon_owner.main([]))\n"
        )
        children: list[subprocess.Popen[Any]] = []
        real = subprocess.Popen

        def capture(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[-2:] == ["-m", "linkedin_mcp_server.daemon.owner"]:
                command = [
                    command[0],
                    str(bootstrap),
                    str(home),
                    str(marker),
                    str(imported),
                ]
            child = real(command, **kwargs)
            children.append(child)
            _await_fixture_imports(child, imported)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", capture)
        try:
            config = _config(profile)
            outcome = spawn_module._start_owner(
                auth_root,
                config,
                timeout=5.0,
                inspector=daemon_module._DescriptorInspector(
                    auth_root, profile, config
                ),
            )

            assert outcome is Attempt.ABORTED
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
            spawn_module,
            "_spawn",
            lambda *a, **k: Started.RETRY,
        )

        profile = _profile(tmp_path)
        config = _config(profile)
        outcome = spawn_module._start_owner(
            tmp_path,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(tmp_path, profile, config),
        )

        assert outcome is Attempt.CONTENDED

    def test_aborted_windows_winner_is_a_terminal_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        monkeypatch.setattr(
            spawn_module,
            "_spawn",
            lambda *a, **k: Started.ABORTED,
        )

        assert (
            spawn_module._start_owner(
                profile.parent,
                config,
                timeout=1.0,
                inspector=daemon_module._DescriptorInspector(
                    profile.parent, profile, config
                ),
            )
            is Attempt.ABORTED
        )

    def test_aborted_owner_start_does_not_retry_for_the_election_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        starts: list[bool] = []
        # This is the platform-neutral ABORTED branch. Windows exclusion gating
        # is orthogonal and would prevent the stub from reaching that branch.
        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(
            spawn_module,
            "_start_owner",
            lambda *args, **kwargs: (
                starts.append(True),
                Attempt.ABORTED,
            )[1],
        )
        monkeypatch.setattr(
            election_module,
            "_live_lookup",
            lambda *args, **kwargs: (OwnerLookup(OwnerState.ABSENT), False, True),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=0.01,
            settlement_seconds=0,
        )

        assert not outcome.worth_connecting
        assert starts == [True]

    def test_commit_request_failure_leaves_a_live_child_to_settle_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            spawn_module,
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

        assert outcome is Started.UNCERTAIN
        assert not child.killed
        # Closed by the writer thread that took it, and gone from ``Popen``
        # so nothing on the frontend's deadline path can close it a second
        # time behind that thread's back.
        assert child.stdin is None
        assert child.input.closed
        assert events == []

    def test_interrupt_after_commit_request_does_not_kill_the_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        child = self._Child(new_instance_id())
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "validate_prepared",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            spawn_module,
            "_await_committed",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        with pytest.raises(KeyboardInterrupt):
            spawn_module._spawn(
                profile.parent,
                config,
                timeout=1.0,
                inspector=daemon_module._DescriptorInspector(
                    profile.parent, profile, config
                ),
            )

        assert startup.COMMIT in [
            frame["type"] for frame in _frames_in(child.input.written)
        ]
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
        # The generation the child committed before its verdict was lost.
        instance_id = _publish_stale_owner(profile.parent, profile, config)
        child = self._Child(instance_id, final="")
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "validate_prepared",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        outcome = spawn_module._spawn(
            profile.parent,
            config,
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.YES
        assert not child.killed

    def test_stale_canonical_state_stays_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        # The predecessor's generation, still canonical.
        _publish_stale_owner(profile.parent, profile, config)
        monkeypatch.setattr(
            election_module.daemon_descriptor,
            "discard_prepared",
            lambda *a, **k: pytest.fail("a potentially committed token was deleted"),
        )

        outcome = spawn_module._settle_commit_result(
            new_instance_id(),
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.UNCERTAIN

    def test_unreadable_canonical_state_stays_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
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

        outcome = spawn_module._settle_commit_result(
            new_instance_id(),
            timeout=1.0,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert outcome is Started.UNCERTAIN

    def test_canonical_settlement_read_is_bounded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
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
            outcome = spawn_module._settle_commit_result(
                new_instance_id(),
                timeout=0.01,
                inspector=daemon_module._DescriptorInspector(
                    profile.parent, profile, config
                ),
            )
        finally:
            release.set()
            fallback.cancel()

        assert outcome is Started.UNCERTAIN
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
                spawn_module._validate_prepared_until(
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
                spawn_module._resolve_profile_until(
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
                spawn_module._discard_prepared_until(auth_root, abandoned, timeout=0.01)
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

    class _StalledPipe:
        """A startup input pipe whose ``close`` needs the lock its ``write`` holds.

        What ``BufferedWriter`` does against a child that never reads, without a
        real pipe or a platform's buffer size: the write parks holding the
        buffer's own lock, and every other operation on that object waits for it
        to finish. The read side has a measured twin, 29.27 of thirty seconds.
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
            spawn_module, "os", SimpleNamespace(name="posix", environ=os.environ)
        )
        profile = _profile(tmp_path)
        config = _config(profile)
        pipe = self._StalledPipe()
        child = self._StalledChild(pipe)
        monkeypatch.setattr(spawn_module.subprocess, "Popen", lambda *a, **k: child)
        monkeypatch.setattr(
            election_module.daemon_owner,
            "daemon_log_path",
            lambda root: tmp_path / "daemon.log",
        )

        finished: list[object] = []

        def run() -> None:
            try:
                finished.append(
                    spawn_module._spawn(
                        profile.parent,
                        config,
                        timeout=0.5,
                        inspector=daemon_module._DescriptorInspector(
                            profile.parent, profile, config
                        ),
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
                "the frontend was pinned by a startup write that never returned"
            )
            assert finished == [Started.NO]
            # The stop ran, was bounded, and left the child alive: exactly the
            # state in which nothing may close that stream from here.
            assert child.kills == 1
            assert child.poll() is None
            # Handed to the writer, so neither `_stop_child` nor the cleanup in
            # `_spawn` can reach it.
            assert child.stdin is None
            assert pipe.closed_by == [], (
                "the startup pipe was closed while the write still held it"
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
            if thread.name == "daemon-startup-input":
                thread.join(timeout=5)
        assert pipe.closed_by == ["daemon-startup-input"]

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
            spawn_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )
        commands: list[list[str]] = []
        build = spawn_module._spawn_command
        monkeypatch.setattr(
            spawn_module,
            "_spawn_command",
            lambda **kwargs: commands.append(build(**kwargs)) or commands[-1],
        )

        outcome, child, events = self._spawn(
            tmp_path, monkeypatch, validate=lambda *a, **k: None
        )

        assert outcome is Started.YES
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
            spawn_module, "os", SimpleNamespace(name="nt", environ=os.environ)
        )

        outcome, child, _ = self._spawn(
            tmp_path, monkeypatch, validate=lambda *a, **k: None
        )

        assert outcome is Started.YES
        assert len(owner_jobs) == 1
        assert not isinstance(owner_jobs[0], process_tree_module.WindowsJob)
        with pytest.raises(
            process_tree_module.ProcessTreeError, match="no process handle"
        ):
            process_tree_module.WindowsJob.assign_popen(
                cast(Any, None), cast(Any, child)
            )


@pytest.mark.slow
class TestAForwardedCallThroughTheRealOwner:
    """A forwarded call on the hop's pinned version, through the real owner server.

    The owner is ``daemon.owner.create_owner_server`` on loopback in this
    process: its token check, the call admission that wants every call marked,
    the sequential middleware and the profile lease are all production's. What
    is stood in is the browser (lifespan and the bookkeeping around a call) and
    the owner's tools: it gets one synthetic tool, which takes an argument so
    the owner has one to check. The frontend in front of it is the production
    proxy, over a real socket. No browser, no
    profile contents and no account state are used; the profile is an empty
    directory this test claims.
    """

    @staticmethod
    @contextlib.asynccontextmanager
    async def _a_real_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Serve the owner on loopback and yield what a test observes of it.

        The synthetic tool ``hold_the_browser`` records every body that ran,
        and the one called with ``"first"`` holds the call, and with it the
        sequential lock and the profile lease, until *release* is set.
        """
        import linkedin_mcp_server.server as server_module
        from fastmcp.server.dependencies import get_http_headers
        from fastmcp.server.middleware import Middleware

        from linkedin_mcp_server import update_check
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.daemon.liveness import CALL_HEADER, get_liveness
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import _call_being_made
        from linkedin_mcp_server.daemon.proxy import FrontendCallHeartbeatMiddleware
        from linkedin_mcp_server.drivers import browser
        from linkedin_mcp_server.profile_claim import ensure_profile_claim
        from linkedin_mcp_server.profile_lease import get_profile_lease
        from linkedin_mcp_server.server_role import (
            ServerRole,
            reset_process_role_for_testing,
        )

        # Claimed while empty, before anything is created in it: the lease and
        # the owner refuse an auth root that holds files that are not theirs.
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        profile = auth_root / "profile"
        ensure_profile_claim(profile)
        profile.mkdir(exist_ok=True)
        config = _config(profile)
        config.server.tool_timeout_seconds = 10
        set_config(config)

        async def nothing(*_a: Any, **_k: Any) -> None:
            return None

        for name in (
            "initialize_bootstrap",
            "get_runtime_policy",
            "report_retained_browser_revisions_if_ready",
        ):
            monkeypatch.setattr(server_module, name, lambda *_a, **_k: None)
        for name in (
            "start_background_browser_setup_if_needed",
            "watch_for_handoff_requests",
            "stop_background_browser_setup",
            "close_browser",
        ):
            monkeypatch.setattr(server_module, name, nothing)
        monkeypatch.setattr(browser, "note_call_started", lambda: None)
        monkeypatch.setattr(browser, "note_activity", lambda: None)
        monkeypatch.setattr(browser, "release_profile_if_idle_or_requested", nothing)
        monkeypatch.setattr(update_check, "prime_from_cache", lambda: None)
        monkeypatch.setattr(update_check, "refresh_latest_version", nothing)
        monkeypatch.setattr(update_check, "pending_update_notice", lambda: None)
        elections: list[object] = []

        def no_election_expected(*_a: Any, **_k: Any) -> NoReturn:
            # Raised rather than returning a stand-in: the proxy logs a failed
            # election and reports the owner lost, so a call that wrongly
            # recovers ends with that answer instead of a stub's AttributeError.
            elections.append(1)
            raise RuntimeError("this test's owner was taken for lost")

        monkeypatch.setattr(election_module, "obtain_owner", no_election_expected)
        # Why a call was taken for an owner loss, in the captured log of a
        # failing run: the loss with its cause, and a hop's closing failures.
        daemon_logger = logging.getLogger("linkedin_mcp_server.daemon")
        logged_from = daemon_logger.level
        daemon_logger.setLevel(logging.DEBUG)

        held, release = asyncio.Event(), asyncio.Event()
        bodies: list[dict[str, Any]] = []
        listings: list[dict[str, Any]] = []

        class NotesItsListings(Middleware):
            async def on_list_tools(self, context: Any, call_next: Any) -> Any:
                headers = get_http_headers()
                while_held = held.is_set() and not release.is_set()
                listed = await call_next(context)
                # Recorded once the listing has completed, not when it began.
                listings.append(
                    {"bound": CALL_HEADER in headers, "while_held": while_held}
                )
                return listed

        build = server_module.create_mcp_server

        def an_owner_with_a_synthetic_tool(*args: Any, **kwargs: Any) -> Any:
            mcp = build(*args, **kwargs)
            if kwargs.get("role") is ServerRole.OWNER:

                @mcp.tool(
                    name="hold_the_browser", annotations={"destructiveHint": True}
                )
                async def hold_the_browser(value: str) -> dict[str, str]:
                    headers = get_http_headers(include={"mcp-session-id"})
                    bodies.append(
                        {
                            "value": value,
                            "call": headers.get(CALL_HEADER),
                            "version": headers.get("mcp-protocol-version"),
                            "session": headers.get("mcp-session-id"),
                        }
                    )
                    if value == "first":
                        held.set()
                        await release.wait()
                    return {"value": value}

                mcp.add_middleware(NotesItsListings())
            return mcp

        monkeypatch.setattr(
            server_module, "create_mcp_server", an_owner_with_a_synthetic_tool
        )

        # What the frontend marked each call with, at its preflight and as the
        # tool request leaves, with whether the bound call's dispatch record
        # already said it may have left at that moment.
        preflights: list[str] = []
        beat = FrontendCallHeartbeatMiddleware._beat

        async def noting_the_preflight(attachment: Attachment, call_id: str) -> Any:
            preflights.append(call_id)
            return await beat(attachment, call_id)

        monkeypatch.setattr(
            FrontendCallHeartbeatMiddleware, "_beat", staticmethod(noting_the_preflight)
        )
        leaving: list[tuple[str | None, str | None, bool]] = []
        direct = daemon_owner.direct_async_http_client

        async def note_a_tool_request(request: Any) -> None:
            if request.method == "POST" and b'"tools/call"' in request.content:
                bound = _call_being_made.get()
                leaving.append(
                    (
                        request.headers.get(CALL_HEADER),
                        None if bound is None else bound.call_id,
                        bound is not None
                        and bound.dispatch.state is Dispatch.POSSIBLY_SENT,
                    )
                )

        def watched_client(**kwargs: Any) -> Any:
            client = direct(**kwargs)
            client.event_hooks["request"].append(note_a_tool_request)
            return client

        monkeypatch.setattr(daemon_owner, "direct_async_http_client", watched_client)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        attachment = _attachment_for(profile, config, listener.getsockname()[1])
        get_liveness().serving_as(attachment.descriptor.instance_id)
        server = daemon_owner.create_owner_server(
            config=config,
            token=attachment.token,
            host="127.0.0.1",
            port=attachment.descriptor.port,
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        calls: list[asyncio.Task[Any]] = []
        try:
            await daemon_owner._await_started(server, serving)
            # Where `_serve` opens admission: once the endpoint is published.
            get_liveness().the_endpoint_is_live()
            reset_process_role_for_testing()
            yield SimpleNamespace(
                frontend=build(
                    role=ServerRole.PROXY,
                    proxy_backend=_proxy_backend_for(attachment),
                    tool_timeout=10.0,
                ),
                attachment=attachment,
                direct=direct,
                held=held,
                release=release,
                bodies=bodies,
                listings=listings,
                preflights=preflights,
                leaving=leaving,
                elections=elections,
                calls=calls,
                lease=get_profile_lease(),
            )
        finally:
            release.set()
            for call in calls:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
            listener.close()
            daemon_logger.setLevel(logged_from)

    @staticmethod
    async def _held_by(first: asyncio.Task[Any], owner: Any) -> None:
        """Wait until the first call holds the browser, or say how it ended."""
        await _until(lambda: owner.held.is_set() or first.done(), seconds=10)
        assert owner.held.is_set(), f"the first call never ran: {_outcome(first)}"

    async def test_a_forwarded_call_is_routed_admitted_and_kept_in_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        from linkedin_mcp_server.daemon.forwarder import HOP_PROTOCOL_VERSION
        from linkedin_mcp_server.daemon.liveness import (
            REFUSAL_KEY,
            UNMARKED_CALL,
            get_liveness,
        )

        async with self._a_real_owner(tmp_path, monkeypatch) as owner:
            async with Client(owner.frontend) as client:
                first = asyncio.create_task(
                    client.call_tool("hold_the_browser", {"value": "first"})
                )
                owner.calls.append(first)
                await self._held_by(first, owner)
                second = asyncio.create_task(
                    client.call_tool("hold_the_browser", {"value": "second"})
                )
                owner.calls.append(second)
                await _until(
                    lambda: len(get_liveness().state.calls) == 2 or second.done(),
                    seconds=10,
                )
                # The second call was admitted while the first holds the lease,
                # after the listing its own lookup made had completed, and its
                # body waits its turn: a listing never queues behind a call.
                assert not second.done(), _outcome(second)
                assert len(get_liveness().state.calls) == 2
                assert [body["value"] for body in owner.bodies] == ["first"]
                assert {"bound": True, "while_held": True} in owner.listings, (
                    owner.listings
                )
                owner.release.set()
                answers = await asyncio.wait_for(asyncio.gather(first, second), 10)

            # A call without the marker, straight at the owner: refused with the
            # owner's signed marker before the body could run.
            unmarked = Client(
                StreamableHttpTransport(
                    owner.attachment.descriptor.url,
                    auth=owner.attachment.token,
                    httpx_client_factory=owner.direct,
                ),
                timeout=10,
            )
            async with unmarked:
                refused = await unmarked.call_tool(
                    "hold_the_browser", {"value": "unmarked"}, raise_on_error=False
                )

        assert [answer.structured_content for answer in answers] == [
            {"value": "first"},
            {"value": "second"},
        ]
        bodies = owner.bodies
        assert [body["value"] for body in bodies] == ["first", "second"]
        for body in bodies:
            assert body["version"] == HOP_PROTOCOL_VERSION, body
            # A stateless era: no session the owner could hold between calls.
            assert body["session"] is None, body
        # Each body ran under the call id its own preflight named, two calls
        # two ids, and each tool request left carrying it, already claimed.
        first_call, second_call = (body["call"] for body in bodies)
        assert first_call != second_call
        assert first_call == owner.preflights[0]
        assert second_call in owner.preflights
        assert (first_call, first_call, True) in owner.leaving, owner.leaving
        assert (second_call, second_call, True) in owner.leaving, owner.leaving
        assert refused.is_error is True
        assert refused.meta is not None
        assert refused.meta[REFUSAL_KEY]["daemon"] == UNMARKED_CALL
        assert len(get_liveness().state.calls) == 0
        assert owner.elections == [], "a healthy owner was replaced"

    async def test_a_queued_call_given_up_is_withdrawn_from_the_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A caller that gives up while its call waits its turn takes it back.

        The second call is admitted at the owner and queued behind the first,
        which holds the lease. Its caller then cancels. The cancellation has to
        cross the hop: the owner drops the call from admission well before the
        heartbeat expiry could have done it, the lease stays with the first
        call, the cancelled body never runs once the lease is free, and nothing
        on the frontend reads the cancellation as an owner loss to recover
        from or a call to send again.
        """
        from fastmcp import Client

        from linkedin_mcp_server.daemon.core.owner import EXPIRY_SECONDS
        from linkedin_mcp_server.daemon.liveness import get_liveness

        async with self._a_real_owner(tmp_path, monkeypatch) as owner:
            async with Client(owner.frontend) as client:
                first = asyncio.create_task(
                    client.call_tool("hold_the_browser", {"value": "first"})
                )
                owner.calls.append(first)
                await self._held_by(first, owner)
                second = asyncio.create_task(
                    client.call_tool("hold_the_browser", {"value": "second"})
                )
                owner.calls.append(second)
                await _until(
                    lambda: len(get_liveness().state.calls) == 2 or second.done(),
                    seconds=10,
                )
                assert not second.done(), _outcome(second)
                assert len(get_liveness().state.calls) == 2
                assert [body["value"] for body in owner.bodies] == ["first"]
                assert owner.lease.held

                second.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(second, timeout=10)
                # Well inside the expiry, so only a delivered cancellation can
                # have withdrawn it: a caller's heartbeats stop when it gives
                # up, and the owner would otherwise cut the call off later.
                took = await _until(
                    lambda: len(get_liveness().state.calls) == 1,
                    seconds=EXPIRY_SECONDS / 3,
                )
                assert len(get_liveness().state.calls) == 1, (
                    f"the queued call was still admitted after {took:.1f}s"
                )
                assert owner.lease.held, "the first call lost its lease"

                owner.release.set()
                answer = await asyncio.wait_for(first, timeout=10)
                await _until(lambda: len(get_liveness().state.calls) == 0, seconds=10)

        assert answer.structured_content == {"value": "first"}
        assert [body["value"] for body in owner.bodies] == ["first"], (
            "the cancelled call ran once the lease was free"
        )
        assert len(get_liveness().state.calls) == 0
        assert not owner.lease.held
        # Both tool requests left once each, claimed, and nothing was sent again.
        sent = [call for call, _bound, _claimed in owner.leaving]
        assert len(sent) == 2 and len(set(sent)) == 2, owner.leaving
        assert all(claimed for _call, _bound, claimed in owner.leaving)
        assert owner.elections == [], "a cancellation was taken for an owner loss"


class TestRealOwner:
    """The whole thing, with a real detached process on the other end.

    Slow, and worth it: everything these assert is invisible to a test that
    stays in one interpreter. Each spawn starts the full server graph.
    """

    def test_an_owner_is_started_and_answers(self, owner_profile: Path):
        profile = owner_profile

        result = _run_frontend(profile)
        try:
            assert result["state"] == OwnerState.ATTACHABLE.value, result
            assert result["started"] is True
            assert result["pid"] and result["pid"] != os.getpid()
        finally:
            _stop(result.get("pid"))

    @_POSIX_ONLY
    def test_a_paused_owner_is_waited_for_rather_than_written_off(
        self, owner_profile: Path
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
        profile = owner_profile

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

    def test_the_frontend_holds_no_lock_once_an_owner_serves(self, owner_profile: Path):
        # The owner takes the daemon lock itself. A frontend that held it too
        # would keep it alive after the owner died, and every recovery
        # afterwards would be locked out by a process that is not the owner and
        # does not know it holds anything.
        profile = owner_profile

        result = _run_frontend(profile)
        try:
            # Asked inside the frontend, before it exited: the lock is held by
            # the owner, and the frontend could not take it.
            assert result["frontend_holds_lock"] is False, result
        finally:
            _stop(result.get("pid"))

    def test_the_lock_frees_while_the_frontend_is_still_running(
        self, owner_profile: Path
    ):
        # Asked while the frontend is still running, because every other test
        # here lets the frontend exit first, which would close anything it held
        # whether the code released it or not.
        #
        # A frontend that held the daemon lock would keep it after the owner
        # died, so no replacement could ever be elected while that client stayed
        # connected. That is a wedge with no visible cause.
        import json

        profile = owner_profile
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
                "frontend held it"
            )
        finally:
            frontend.kill()
            frontend.wait(timeout=30)

    def test_the_lock_frees_when_the_owner_dies(self, owner_profile: Path):
        # The other half of the same property. The frontend has exited by now,
        # so if it had held the lock it would still be held here even
        # though the owner is gone. Measured at 10 ms on this tree.
        profile = owner_profile
        auth_root = profile.parent

        result = _run_frontend(profile)
        pid = result["pid"]
        assert isinstance(pid, int)
        assert _lock_is_held(auth_root)

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
        self, owner_profile: Path
    ):
        # The point of the whole feature: one browser, however many clients.
        profile = owner_profile

        first = _run_frontend(profile)
        try:
            assert first["started"] is True
            second = _run_frontend(profile)

            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is False, "a second owner was started"
            assert second["pid"] == first["pid"]
        finally:
            _stop(first.get("pid"))

    def test_a_crashed_owner_is_replaced(self, owner_profile: Path):
        # The descriptor survives the process that wrote it, so the next client
        # must prove the endpoint before trusting it and elect a replacement
        # when it does not answer.
        profile = owner_profile

        first = _run_frontend(profile)
        dead = first["pid"]
        assert isinstance(dead, int)
        _stop(dead)
        # The kernel frees the lock at death; wait for it rather than assuming.
        for _ in range(300):
            if not _lock_is_held(profile.parent):
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
        self, owner_profile: Path, tmp_path: Path
    ):
        child = tmp_path / "lock_taking_child.py"
        marker = tmp_path / "locked"
        child.write_text(
            _GREETED_CHILD + "import sys\n"
            "from pathlib import Path\n"
            "from linkedin_mcp_server.daemon.lock import DaemonLock\n"
            "lock = DaemonLock(Path(sys.argv[1]))\n"
            "assert lock.try_acquire()\n"
            "Path(sys.argv[2]).write_text('locked')\n"
            "channel.send(startup.VERDICT, nonce, status=startup.FAILED)\n"
            "raise SystemExit(7)\n"
        )

        profile = owner_profile
        auth_root = profile.parent
        frontend = (
            "import sys\n"
            "from pathlib import Path\n"
            "import linkedin_mcp_server.daemon.election as election\n"
            "import linkedin_mcp_server.daemon.spawn as spawn\n"
            "real = spawn.subprocess.Popen\n"
            "def substitute(command, **kwargs):\n"
            "    if command[-2:] != ['-m', 'linkedin_mcp_server.daemon.owner']:\n"
            "        return real(command, **kwargs)\n"
            "    return real([command[0], sys.argv[2], sys.argv[3], sys.argv[4]], **kwargs)\n"
            "spawn.subprocess.Popen = substitute\n"
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

    def test_launch_barrier_timeout_exits_without_electing(self, owner_profile: Path):
        # Mutation target: drop the ``if not go.exists(): raise SystemExit``
        # in ``_INSPECT_OWNER`` and this fails — the child would call
        # ``obtain_owner`` and print an attachable result instead of exiting.
        profile = owner_profile
        ready_dir = profile.parent / "launch-barrier-timeout"
        ready_dir.mkdir()
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _INSPECT_OWNER,
                str(profile),
                str(ready_dir),
                "0",
                "0.2",
            ],
            capture_output=True,
            text=True,
            env=_inspect_owner_environment(),
            cwd=_REPO_ROOT,
            timeout=30,
        )
        assert child.returncode != 0, child.stdout[-1000:]
        assert "launch barrier timed out" in child.stderr
        # No JSON election result on stdout — we never entered obtain_owner.
        assert not any(
            line.strip().startswith("{") for line in child.stdout.splitlines()
        ), child.stdout
        with contextlib.suppress(Exception):
            published = daemon_descriptor_module.read(profile.parent)
            if published is not None:
                _stop(published.pid)
                pytest.fail(f"barrier timeout still elected owner {published.pid}")

    @pytest.mark.parametrize("failure", ["spawn", "barrier"])
    def test_launch_barrier_failure_collects_started_frontends(
        self, owner_profile: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ):
        popen = subprocess.Popen
        running: list[subprocess.Popen[str]] = []
        waited: list[subprocess.Popen[str]] = []

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
            # The patch is process-wide, and the interpreter spawns children of
            # its own (``ctypes.util`` runs ``/sbin/ldconfig -p`` on Linux).
            # Only a frontend is failed, tracked or wrapped; a ninth frontend
            # still counts.
            command = args[0] if args else kwargs.get("args")
            if not (
                isinstance(command, list)
                and command[:3] == [sys.executable, "-c", _INSPECT_OWNER]
            ):
                return popen(*args, **kwargs)
            if failure == "spawn" and len(running) == 2:
                raise OSError("frontend spawn failed")
            child = popen(*args, **kwargs)
            original_wait = child.wait

            def wait(timeout: float | None = None) -> int:
                returncode = original_wait(timeout=timeout)
                waited.append(child)
                return returncode

            def communicate(*args: Any, **kwargs: Any) -> tuple[str, str]:
                pytest.fail("abnormal frontend cleanup called communicate()")

            monkeypatch.setattr(child, "wait", wait)
            monkeypatch.setattr(child, "communicate", communicate)
            running.append(child)
            return child

        times = iter([0.0, 61.0])
        monkeypatch.setattr(subprocess, "Popen", spawn)
        monkeypatch.setattr(
            sys.modules[__name__],
            "time",
            SimpleNamespace(monotonic=lambda: next(times)),
        )
        error = OSError if failure == "spawn" else pytest.fail.Exception
        message = "frontend spawn failed" if failure == "spawn" else "launch barrier"
        try:
            with pytest.raises(error, match=message):
                self.test_many_clients_starting_at_once_elect_exactly_one_owner(
                    owner_profile
                )

            assert len(running) == (2 if failure == "spawn" else 8)
            assert waited == running
            for child in running:
                assert child.returncode is not None, "frontend was not collected"
                assert child.stdout is not None and child.stdout.closed
                assert child.stderr is not None and child.stderr.closed
            assert not (owner_profile.parent / "launch-barrier" / "go").exists()
        finally:
            _reap_frontends(running)

    def test_timed_out_communicate_preserves_owner_cleanup(
        self, owner_profile: Path, monkeypatch: pytest.MonkeyPatch
    ):
        popen = subprocess.Popen
        stop = _stop
        running: list[subprocess.Popen[str]] = []
        stopped: list[object] = []

        class LiveReader:
            alive = True

            def is_alive(self) -> bool:
                return self.alive

        class GuardedStream:
            def __init__(self, stream: Any, reader: LiveReader):
                self.stream = stream
                self.reader = reader
                self.close_calls = 0

            @property
            def closed(self) -> bool:
                return self.stream.closed

            def close(self) -> None:
                assert not self.reader.is_alive(), "closed under a live pipe reader"
                self.close_calls += 1
                self.stream.close()

        readers: list[LiveReader] = []
        streams: list[GuardedStream] = []

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
            child = popen(*args, **kwargs)
            running.append(child)
            if len(running) != 1:
                return child

            assert child.stdout is not None
            assert child.stderr is not None
            stdout_reader = LiveReader()
            stderr_reader = LiveReader()
            stdout = GuardedStream(child.stdout, stdout_reader)
            stderr = GuardedStream(child.stderr, stderr_reader)
            child.stdout = cast(Any, stdout)
            child.stderr = cast(Any, stderr)
            monkeypatch.setattr(child, "stdout_thread", stdout_reader, raising=False)
            monkeypatch.setattr(child, "stderr_thread", stderr_reader, raising=False)
            readers.extend((stdout_reader, stderr_reader))
            streams.extend((stdout, stderr))

            def communicate(
                input: str | None = None, timeout: float | None = None
            ) -> tuple[str, str]:
                assert input is None
                assert timeout is not None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if daemon_descriptor_module.read(owner_profile.parent) is not None:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail(
                        "the owner was not published before communicate timed out"
                    )
                raise subprocess.TimeoutExpired(child.args, timeout)

            monkeypatch.setattr(child, "communicate", communicate)
            return child

        def track_stop(pid: object) -> None:
            stopped.append(pid)
            stop(pid)

        monkeypatch.setattr(subprocess, "Popen", spawn)
        monkeypatch.setattr(sys.modules[__name__], "_stop", track_stop)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                self.test_many_clients_starting_at_once_elect_exactly_one_owner(
                    owner_profile
                )

            assert len(running) == 8
            assert all(child.returncode is not None for child in running)
            assert all(stream.close_calls == 0 for stream in streams)
            assert stopped, "the published owner was not cleaned up"
        finally:
            for reader in readers:
                reader.alive = False
            _reap_frontends(running)
            for pid in stopped:
                stop(pid)

    def test_many_clients_starting_at_once_elect_exactly_one_owner(
        self, owner_profile: Path
    ):
        # The property the whole feature is for, under the condition that
        # actually produces races: several MCP clients launching together, each
        # spawning its own stdio server, all of them arriving at an empty state
        # directory at the same moment.
        #
        # Two browsers on one profile is the failure this must never have. The
        # single-frontend tests cannot see it, because nothing contends there.
        import json

        profile = owner_profile
        clients = 8
        # Shared start barrier: every frontend announces ready, then all
        # release together. A 50ms stagger would avoid the empty-directory
        # stampede this test exists to exercise (#881).
        ready_dir = profile.parent / "launch-barrier"
        ready_dir.mkdir()

        running = []
        results = []
        owners: set[object] = set()
        try:
            for i in range(clients):
                running.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            _INSPECT_OWNER,
                            str(profile),
                            str(ready_dir),
                            str(i),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=_inspect_owner_environment(),
                        cwd=_REPO_ROOT,
                    )
                )

            barrier_deadline = time.monotonic() + 60.0
            while time.monotonic() < barrier_deadline:
                if len(list(ready_dir.glob("ready-*"))) >= clients:
                    break
                time.sleep(0.01)
            else:
                pytest.fail(
                    f"only {len(list(ready_dir.glob('ready-*')))} of {clients} "
                    "frontends reached the launch barrier"
                )
            (ready_dir / "go").write_text("1", encoding="utf-8")

            for frontend in running:
                out, err = frontend.communicate(timeout=300)
                assert frontend.returncode == 0, _both_ends(err)
                result = json.loads(out.strip().splitlines()[-1])
                results.append(result)
                owners.add(result["pid"])

            assert None not in owners, f"a client ended up with no owner: {results}"
            assert len(owners) == 1, f"more than one owner was elected: {owners}"
            # Exactly one of them did the starting; the rest attached to it.
            assert sum(1 for r in results if r["started"]) == 1, results
            assert all(r["state"] == OwnerState.ATTACHABLE.value for r in results)
            # And none of them kept the lock they may have taken on the way.
            assert not any(r["frontend_holds_lock"] for r in results)
        finally:
            _reap_frontends(running)
            for pid in owners:
                _stop(pid)
            with contextlib.suppress(Exception):
                published = daemon_descriptor_module.read(profile.parent)
                if published is not None:
                    _stop(published.pid)

    @_POSIX_ONLY
    def test_the_owner_outlives_the_client_that_started_it(self, owner_profile: Path):
        # The premise of the whole feature. An owner that died with its first
        # client would give every later client a cold start plus a fresh
        # ``/feed/`` validation, which is the traffic this exists to remove.
        #
        # Killed by process *group*, not by pid, because that is how a client
        # shutdown reaches a server it spawned, while a child that merely had its
        # own pid would still be caught by it. ``start_new_session`` is what puts
        # the owner in a group of its own.
        import json

        profile = owner_profile

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

    def test_an_older_owner_hands_over_to_a_newer_build(self, owner_profile: Path):
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

        profile = owner_profile
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

    def test_the_owner_requires_its_token(self, owner_profile: Path):
        # The endpoint is loopback, which every process on the machine can
        # reach, and a website the user merely visits can reach through their
        # own browser. The token is what stands between that and a logged-in
        # LinkedIn session.
        import asyncio

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        profile = owner_profile
        result = _run_frontend(profile)
        url = result["url"]
        assert isinstance(url, str)

        try:

            async def unauthenticated() -> None:
                async with Client(StreamableHttpTransport(url, auth="wrong")) as client:
                    await client.list_tools()

            # Matched on the answer, because a bare `Exception` also passed on
            # a ping, which the 2026-07-28 era refuses whatever the token.
            with pytest.raises(Exception, match="Server returned an error response"):
                asyncio.run(unauthenticated())
        finally:
            _stop(result.get("pid"))

    def test_current_and_predecessor_tokens_stay_on_disk(self, owner_profile: Path):
        # Cleanup runs before replacement, so the currently canonical token must
        # remain attachable if this startup fails. A successful replacement leaves
        # that predecessor beside the new generation and removes anything older.
        profile = owner_profile
        auth_root = profile.parent

        first = _run_frontend(profile)
        predecessor = daemon_descriptor_module.read(auth_root)
        assert predecessor is not None
        _stop(first["pid"])
        for _ in range(300):
            if not _lock_is_held(auth_root):
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
        self, owner_profile: Path
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
        from fastmcp.client.transports import StreamableHttpTransport

        from linkedin_mcp_server.daemon.discovery import look_up_owner
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        profile = owner_profile
        result = _run_frontend(profile)
        try:
            assert result["state"] == OwnerState.ATTACHABLE.value, result

            # Read the way a frontend does, rather than reusing what the child
            # returned: the descriptor and token on disk are what a proxy is
            # built from.
            lookup = look_up_owner(profile.parent, profile, _config(profile))
            attachment = lookup.attachment
            assert attachment is not None, lookup.reason

            proxy = create_mcp_server(
                tool_timeout=30.0,
                role=ServerRole.PROXY,
                proxy_backend=_proxy_backend_for(attachment),
            )

            async def served() -> tuple[set[str], set[str]]:
                # Read the expectation directly from the same owner so proxy
                # omissions cannot change both sides of the comparison.
                transport = StreamableHttpTransport(
                    attachment.descriptor.url,
                    auth=attachment.token,
                    httpx_client_factory=daemon_owner.direct_async_http_client,
                )
                async with Client(transport, timeout=30.0) as owner_client:
                    owner_names = {
                        tool.name for tool in await owner_client.list_tools()
                    }
                async with Client(proxy) as client:
                    names = {tool.name for tool in await client.list_tools()}
                return owner_names, names

            owner_names, names = asyncio.run(served())

            assert {"get_person_profile", "close_session"} <= owner_names
            assert names == owner_names
        finally:
            _stop(result.get("pid"))

    def test_a_real_owner_asks_the_client_to_sign_in(self, owner_profile: Path):
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

        from linkedin_mcp_server.daemon.discovery import look_up_owner
        from linkedin_mcp_server.daemon.auth_repair import MARKER_KEY, MARKER_VERSION
        from linkedin_mcp_server.daemon.proxy import FrontendCallMiddleware
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        profile = owner_profile
        # Readiness must pass before the auth gate, but nothing here launches a
        # browser. Keep both completion markers and metadata in the temporary root.
        _prepare_browser_cache(profile)

        result = _run_frontend(profile)
        try:
            lookup = look_up_owner(profile.parent, profile, _config(profile))
            assert lookup.attachment is not None, lookup.reason

            proxy = create_mcp_server(
                tool_timeout=30.0,
                role=ServerRole.PROXY,
                proxy_backend=_proxy_backend_for(lookup.attachment),
            )
            # The call middleware, which repairs, is removed rather than never
            # added, because `create_mcp_server` installs it for every proxy and
            # that is correct: a real frontend consumes this marker, opens a
            # login window and waits out the login timeout. Removing it is what
            # lets the marker be observed at all, and it is the reason this test
            # asserts what the owner *sends* rather than what a client finally
            # sees.
            proxy.middleware[:] = [
                m for m in proxy.middleware if not isinstance(m, FrontendCallMiddleware)
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
        self, owner_profile: Path, monkeypatch: pytest.MonkeyPatch
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
        from mcp import MCPError

        from linkedin_mcp_server.daemon import proxy as daemon_proxy
        from linkedin_mcp_server.daemon.discovery import look_up_owner
        from linkedin_mcp_server.server import ServerRole, create_mcp_server

        # What the proxy's recovery was handed, because the client no longer
        # reads it: a failure recognised as the owner's reaches the client as a
        # fixed sentence about a lost owner, whatever the owner answered.
        seen: list[BaseException] = []
        recognise = daemon_proxy.unreachable_owner_in

        def recording(exc: BaseException) -> Any:
            current: BaseException | None = exc
            while current is not None:
                seen.append(current)
                current = current.__cause__
            return recognise(exc)

        monkeypatch.setattr(daemon_proxy, "unreachable_owner_in", recording)

        profile = owner_profile
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
                async with Client(proxy, mode="legacy") as client:
                    await client.list_tools()

            async def asked_directly() -> int:
                from linkedin_mcp_server.daemon import owner as daemon_owner

                async with daemon_owner.direct_async_http_client() as http:
                    response = await http.post(
                        wrong.descriptor.url,
                        headers={"Authorization": f"Bearer {wrong.token}"},
                        json={},
                    )
                return response.status_code

            # Matched on what came back rather than catching anything at all. A
            # bare `Exception` passed on any failure whatsoever, including one
            # raised before a request ever left this process, so the test would
            # have gone green without the owner refusing anything. SDK v2 no
            # longer names the status: an HTTP error without a JSON-RPC body
            # becomes "Server returned an error response", which is written only
            # once a response of 400 or more has arrived. Which status it was is
            # asked of the owner directly, with the same token.
            with pytest.raises(Exception, match="could not reach a new one"):
                asyncio.run(served())
            assert any(
                isinstance(exc, MCPError)
                and exc.message == "Server returned an error response"
                for exc in seen
            ), seen
            assert asyncio.run(asked_directly()) == 401
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            # Would attach if version were not consulted, which is what makes
            # this test about the version rather than about reachability.
            connect=lambda attachment, timeout: Reach.ANSWERED,
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.ANSWERED,
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
            deadline_seconds=0.1,
            settlement_seconds=0,
            connect=lambda attachment, timeout: Reach.ANSWERED,
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

        import httpx2
        import uvicorn

        from linkedin_mcp_server import config as config_module
        from linkedin_mcp_server.daemon import liveness as daemon_liveness
        from linkedin_mcp_server.daemon import owner as daemon_owner

        config = _config(_profile(tmp_path))
        daemon_liveness.get_liveness().serving_as("the-owner")
        # A request the owner would act on, so a refusal is about the token.
        turnover = daemon_owner.stand_down_request(
            "the-owner", mode=daemon_owner.STAND_DOWN_TURNOVER
        )
        # The owner installs its handed-over configuration before it serves, and
        # the lifespan depends on those exact settings rather than defaults.
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
                async with httpx2.AsyncClient() as client:
                    without = await client.post(url, json=turnover)
                    wrong = await client.post(
                        url, headers={"Authorization": "Bearer nope"}, json=turnover
                    )
                    assert not stopped, "an unauthenticated caller stopped the daemon"

                    good = await client.post(
                        url,
                        headers={"Authorization": "Bearer the-token"},
                        json=turnover,
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

    def test_a_turnover_request_retires_a_busy_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The election's own request against the owner's own route. Busy, so
        # only a turnover stands it down: an idle-only request would be refused
        # 409, and one without a body 400, each with nothing changed.
        import asyncio
        import socket

        from linkedin_mcp_server import config as config_module
        from linkedin_mcp_server.daemon import liveness as daemon_liveness
        from linkedin_mcp_server.daemon import owner as daemon_owner

        config = _config(_profile(tmp_path))
        monkeypatch.setattr(config_module, "_config", config)
        from linkedin_mcp_server.daemon.core import owner as lifecycle

        liveness = daemon_liveness.get_liveness()
        liveness.serving_as("the-owner")
        # One admitted call, decided by the owner's own core. No task is
        # started: this test has no event loop until the request below.
        liveness._state, _effects = lifecycle.on_event(
            liveness.state,
            lifecycle.Admit(daemon_liveness.new_call_id(), time.monotonic()),
        )
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
        attachment = SimpleNamespace(
            descriptor=SimpleNamespace(
                host="127.0.0.1", port=port, instance_id="the-owner"
            ),
            token="the-token",
        )

        async def exercise() -> None:
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                await daemon_owner._await_started(server, serving)
                await asyncio.to_thread(
                    election_module._ask_to_stand_down, cast(Any, attachment)
                )
            finally:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(serving, timeout=15)

        asyncio.run(exercise())

        assert stopped == ["asked"]

    def test_the_token_never_goes_through_a_configured_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Every request the daemon makes carries the bearer token for a server
        # driving a logged-in LinkedIn session, and every one is addressed to
        # loopback. httpx2 honours HTTP_PROXY by default and does so even for
        # 127.0.0.1 unless NO_PROXY happens to say otherwise. This was reproduced
        # against a capture proxy, which received the absolute loopback URL and
        # `Authorization: Bearer <token>` in full.
        #
        # A user with a corporate proxy configured is the ordinary case, not an
        # exotic one, and the browser's own proxy setting is deliberately about
        # LinkedIn's traffic rather than the server's.
        import socket
        import threading

        from linkedin_mcp_server.daemon import owner as daemon_owner

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
        hook_module = "_linkedin_mcp_test_user_site_hook"
        (user_site / f"{hook_module}.py").write_text(
            "import builtins\nbuiltins.OWNER_USER_SITE_STARTED = True\n"
        )
        (user_site / "linkedin-mcp-test-user-site.pth").write_text(
            f"import {hook_module}\n"
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
            "from linkedin_mcp_server.daemon.spawn import _spawn_command\n"
            "print(json.dumps(_spawn_command()))\n"
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
        assert command[-2:] == ["-m", "linkedin_mcp_server.daemon.owner"]

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
            "from linkedin_mcp_server.daemon.spawn import _spawn_command\n"
            "print(json.dumps(_spawn_command()))\n"
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
        # a workspace containing linkedin_mcp_server/daemon/owner.py is imported
        # in preference to the installed package. That code would receive the
        # whole configuration on standard input, proxy_password included, merely because an MCP client happened
        # to be started in that directory.
        #
        # Exercised against a real interpreter rather than by inspecting the
        # command, because what matters is which module actually loads.
        workspace = tmp_path / "workspace"
        shadow = workspace / "linkedin_mcp_server"
        (shadow / "daemon").mkdir(parents=True)
        (shadow / "__init__.py").write_text("")
        (shadow / "daemon" / "__init__.py").write_text("")
        (shadow / "daemon" / "owner.py").write_text("print('HIJACKED')\n")

        # The real command, with `-m <module>` swapped for a `-c` that reports
        # which file that module resolved to. Everything before it, including the
        # interpreter and its flags, is exactly what production uses.
        command = spawn_module._spawn_command()
        assert command[-2:] == ["-m", "linkedin_mcp_server.daemon.owner"], command
        monkeypatch.setenv("PYTHONPATH", ".")
        environment = spawn_module._owner_environment()
        assert "PYTHONPATH" not in environment
        result = subprocess.run(
            [
                *command[:-2],
                "-c",
                "import linkedin_mcp_server.daemon.owner as m; print(m.__file__)",
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
            handed = spawn_module._owner_environment()
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
        from linkedin_mcp_server.daemon.owner import _matches_token

        assert not _matches_token("töken", "the-token")
        assert not _matches_token("\udcff", "the-token")
        assert _matches_token("the-token", "the-token")
        assert _matches_token("  the-token  ", "the-token")


class TestBudgets:
    @pytest.mark.parametrize("platform", ["posix", "nt"])
    def test_greeting_handover_and_prepared_wait_share_one_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        budget = 0.5
        greeting_time = 0.1
        handover_time = 0.3
        handshake_nonce = "0123456789abcdef" * 4
        timeouts: dict[str, float] = {}

        class _Clock:
            now = 100.0

            @classmethod
            def monotonic(cls) -> float:
                return cls.now

        class _Child:
            pid = 4242

            def __init__(self) -> None:
                self.stdin: io.BytesIO | None = io.BytesIO()
                self.stdout: io.BytesIO | None = io.BytesIO()
                self.stderr = None
                self.returncode: int | None = None

            def kill(self) -> None:
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("owner", timeout or 0)
                return self.returncode

            def poll(self) -> int | None:
                return self.returncode

        child = _Child()
        spawn_kwargs: dict[str, object] = {}

        class _Job(TestAtomicStartupCommit._Job):
            @classmethod
            def named(cls, purpose: str) -> _Job:
                assert purpose == "owner"
                return cls("Local\\linkedin-mcp-owner-budget-double")

        class _Channel:
            def __init__(self, process: object, nonce: str) -> None:
                assert process is child
                assert nonce == handshake_nonce
                self.nonce = nonce

            def close(self) -> None:
                pass

        def greet(channel: object, *, timeout: float) -> None:
            assert isinstance(channel, _Channel)
            timeouts["greet"] = timeout
            _Clock.now += greeting_time

        def hand_over(
            channel: object, handed_config: AppConfig, *, timeout: float
        ) -> None:
            assert isinstance(channel, _Channel)
            assert handed_config is config
            timeouts["config"] = timeout
            _Clock.now += handover_time

        def await_prepared(channel: object, *, timeout: float) -> Started:
            assert isinstance(channel, _Channel)
            timeouts["prepared"] = timeout
            # A verdict that ends the attempt here, so only the shared budget
            # up to the prepared wait is under test.
            return Started.RETRY

        monkeypatch.setattr(
            spawn_module,
            "os",
            SimpleNamespace(name=platform, environ=os.environ),
        )
        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", platform == "nt")
        if platform == "nt":
            monkeypatch.setattr(
                spawn_module.subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x1,
                raising=False,
            )
            monkeypatch.setattr(
                spawn_module.subprocess, "DETACHED_PROCESS", 0x2, raising=False
            )
            monkeypatch.setattr(
                spawn_module.subprocess,
                "CREATE_BREAKAWAY_FROM_JOB",
                0x4,
                raising=False,
            )
        monkeypatch.setattr(spawn_module, "time", _Clock)
        monkeypatch.setattr(spawn_module, "WindowsJob", _Job)
        monkeypatch.setattr(startup, "FrontendChannel", _Channel)
        monkeypatch.setattr(spawn_module, "new_nonce", lambda: handshake_nonce)

        def popen(*args: object, **kwargs: object) -> _Child:
            spawn_kwargs.update(kwargs)
            return child

        monkeypatch.setattr(spawn_module.subprocess, "Popen", popen)
        monkeypatch.setattr(spawn_module, "_greet", greet)
        monkeypatch.setattr(spawn_module, "_hand_over_config", hand_over)
        monkeypatch.setattr(spawn_module, "_await_prepared", await_prepared)

        verdict = spawn_module._spawn(
            profile.parent,
            config,
            timeout=budget,
            inspector=daemon_module._DescriptorInspector(
                profile.parent, profile, config
            ),
        )

        assert verdict is Started.RETRY
        assert spawn_kwargs["creationflags"] == (0x7 if platform == "nt" else 0)
        assert timeouts == pytest.approx(
            {
                "greet": budget,
                "config": budget - greeting_time,
                "prepared": budget - greeting_time - handover_time,
            }
        )

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
        from linkedin_mcp_server.daemon import owner as daemon_owner

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

        from linkedin_mcp_server.daemon import owner as daemon_owner

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

        from linkedin_mcp_server import server_role
        from linkedin_mcp_server.daemon import owner as daemon_owner

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
                daemon_owner._serve_until_stopped(server, serving, lock=None)
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

        from linkedin_mcp_server.daemon import owner as daemon_owner

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

        from linkedin_mcp_server.daemon import owner as daemon_owner

        exited: list[bool] = []
        monkeypatch.setattr(daemon_owner, "hard_exit_required", lambda: True)
        monkeypatch.setattr(
            daemon_owner, "_exit_hard", lambda _lock: exited.append(True)
        )

        async def exercise() -> None:
            serving = asyncio.create_task(asyncio.sleep(0))
            await serving
            await daemon_owner._serve_until_stopped(object(), serving, lock=None)

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

        from linkedin_mcp_server.daemon import owner as daemon_owner

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
            daemon_owner.get_liveness().turn_over()
            began = time.monotonic()
            try:
                await daemon_owner._serve_until_stopped(server, serving, lock=None)
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
            "import linkedin_mcp_server.daemon.descriptor as descriptor\n"
            "descriptor._account_home = lambda: Path(sys.argv[1])\n"
            "from linkedin_mcp_server.daemon import owner as daemon_owner\n"
            "from linkedin_mcp_server.daemon.lock import DaemonLock\n"
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
            "    daemon_owner.get_liveness().turn_over()\n"
            "    await daemon_owner._serve_until_stopped(server, serving, lock=lock)\n"
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
    """The process ending inside ``os._exit``.

    Not an ``Exception``: ``_stop_within`` catches those around its own wait.
    """


class TestTheHardExitFreesTheElectionFirst:
    """The election is released before the process ends, so nothing that
    delays the exit holds the election with it."""

    def test_the_exit_begins_with_the_election_already_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Driven through the whole owner rather than through ``_exit_hard``, so
        # the lock reaching it is the production wiring and not the test's.
        # Only the state operations are stood in for; `_serve_until_stopped`,
        # which is what hands the lock down, stays the real one.
        import asyncio

        from linkedin_mcp_server.daemon import owner as daemon_owner

        nonce = "0123456789abcdef" * 4
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        lock = DaemonLock(auth_root)
        assert lock.try_acquire(), "the test could not take the lock it means to free"
        held_at_the_exit: list[bool] = []

        def exit_now(status: int) -> None:
            held_at_the_exit.append(lock.held)
            raise _ProcessEnded

        async def probe(url: str, token: str) -> None:
            return None

        class _StoppedServer:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        announced: list[str] = []

        class _SilentHandshake:
            def prepared(self, instance_id: str) -> None:
                announced.append(instance_id)

            def ready(self) -> None: ...

            def committed(self) -> None: ...

            def fail(self) -> None: ...

            def abort(self) -> None: ...

            def retry(self) -> None: ...

            def close(self) -> None: ...

        class _CommittingParent:
            def read(self) -> dict[str, object]:
                return startup.message(startup.COMMIT, nonce, instance=announced[-1])

        monkeypatch.setattr(daemon_owner.os, "_exit", exit_now)
        monkeypatch.setattr(daemon_owner, "hard_exit_required", lambda: True)
        monkeypatch.setattr(daemon_owner, "prove_hop", probe)
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
        assert held_at_the_exit == [False], (
            "the exit began while the election lock was still held"
        )


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
        commit_implementation: Callable[[Path, str], object] | None = None,
        announced: list[str] | None = None,
    ) -> int:
        import asyncio

        #: The generation the owner announced in PREPARED, which is the one a
        #: genuine COMMIT names.
        announced = [] if announced is None else announced
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
                announced.append(instance_id)
                order.append("prepared")

            def committed(self) -> None:
                order.append("committed")

            def fail(self) -> None:
                order.append("failed")

            def abort(self) -> None:
                order.append("aborted")

            def retry(self) -> None:
                order.append("retry")

            def close(self) -> None:
                return None

        class _Control:
            def read(self) -> dict[str, object]:
                order.append("control")
                if not commit:
                    raise EOFError
                return startup.message(
                    startup.COMMIT, "0123456789abcdef" * 4, instance=announced[-1]
                )

        class _StoppedServer:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                import asyncio

                while not self.should_exit:
                    await asyncio.sleep(0)

        default_server = _StoppedServer()
        servers: list[object] = []

        monkeypatch.setattr(daemon_owner, "prove_hop", probe)
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
                job_name=job_name,
            )
        )

    def test_the_frontend_authorizes_the_commit_over_control(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The one startup protocol: prepared, then the frontend's COMMIT read
        # from the startup channel, and only then the canonical descriptor.
        order: list[str] = []

        self._run_serve(tmp_path, monkeypatch, order)

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
        "forge",
        [
            lambda instance: {"type": startup.COMMIT, "instance": instance},
            lambda instance: startup.message(
                startup.COMMIT, "f" * 64, instance=instance
            ),
            lambda instance: startup.message(
                startup.COMMIT, "0123456789abcdef" * 4, instance=new_instance_id()
            ),
        ],
        ids=["plain", "wrong-nonce", "other-generation"],
    )
    def test_commit_authorization_requires_the_handed_over_nonce(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        forge: Callable[[str], dict[str, object]],
    ):
        order: list[str] = []
        announced: list[str] = []

        class _UnauthenticatedParent:
            def read(self) -> dict[str, object]:
                order.append("control")
                return forge(announced[-1])

        monkeypatch.setattr(
            daemon_owner.logger, "info", lambda *args, **kwargs: order.append("logged")
        )

        outcome = self._run_serve(
            tmp_path,
            monkeypatch,
            order,
            control=_UnauthenticatedParent(),
            announced=announced,
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
        control_read = threading.Event()
        finished = threading.Event()
        order: list[str] = []
        outcomes: list[int] = []
        errors: list[BaseException] = []

        class _SuspendedParent:
            def read(self) -> dict[str, object]:
                order.append("control")
                control_read.set()
                release.wait()
                raise EOFError

        def log_timeout(*args: object, **kwargs: object) -> None:
            assert control_read.is_set()
            assert not release.is_set()
            order.append("logged")

        real_thread = threading.Thread

        class _SynchronizedThread(real_thread):
            def start(self) -> None:
                real_thread.start(self)
                if self.name == "daemon-control":
                    assert control_read.wait(_THREAD_START_SECONDS), (
                        "the daemon-control reader never entered the blocking "
                        "parent-pipe read"
                    )

        monkeypatch.setattr(daemon_owner, "_COMMIT_AUTH_SECONDS", 0.1)
        monkeypatch.setattr(
            daemon_owner,
            "threading",
            SimpleNamespace(Thread=_SynchronizedThread),
        )
        monkeypatch.setattr(daemon_owner.logger, "warning", log_timeout)

        def run() -> None:
            try:
                outcomes.append(
                    self._run_serve(
                        tmp_path,
                        monkeypatch,
                        order,
                        control=_SuspendedParent(),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - asserted by this test
                errors.append(exc)
            finally:
                finished.set()

        child = threading.Thread(target=run, daemon=True)
        child.start()
        try:
            assert control_read.wait(_THREAD_START_SECONDS), (
                "the owner never started reading the open parent pipe"
            )
            # After the reader is known to be blocked, authorization expiry is
            # a fast path (0.1s here). Bound completion with the call budget,
            # not the thread-start allowance — otherwise a multi-second cleanup
            # regression would still pass (#881).
            assert finished.wait(_BOUNDED_CALL_SECONDS), (
                "the blocked parent pipe outlived the authorization deadline"
            )
        finally:
            release.set()
            child.join(timeout=5)

        assert not child.is_alive()
        assert errors == []
        assert outcomes == [1]
        # `control` is appended on the reader thread `_read_control_until` starts.
        # Once that read is known to be blocked, the main flow reaches its deadline
        # independently. Only the main flow's sequence is ordered across threads.
        assert order.count("control") == 1
        assert [event for event in order if event != "control"] == [
            "probe",
            "prepare",
            "prepared",
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
            def read(self) -> dict[str, object]:
                order.append("control")
                release_control.wait()
                raise EOFError

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
        assert "failed" not in order

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

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("rename reply was lost"),
            PermissionError("rename retry observed changed permissions"),
            KeyboardInterrupt(),
        ],
        ids=["lost reply", "changed permissions", "interrupted"],
    )
    def test_an_ambiguous_commit_exits_without_serving(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: BaseException,
    ):
        """A rename that may have taken effect ends the owner, unpublished.

        Only the first attempt fails, so an owner that tried again would
        publish and serve. FAILED rather than ABORTED, because ABORTED tells the
        frontend nothing was published; the prepared state and the token stay
        where a successor's election finds them.
        """
        from linkedin_mcp_server.daemon.core.owner import Retirement

        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: pytest.fail("an ambiguous commit was reconciled"),
        )
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "discard_prepared",
            lambda *a: pytest.fail("an ambiguous commit removed its prepared state"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "warning",
            lambda *args, **kwargs: order.append("diagnostic"),
        )
        exited: list[object] = []

        def exit_hard(lock: object) -> NoReturn:
            exited.append(lock)
            raise _ProcessEnded

        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)

        # Even after the endpoint stopped cleanly: the error would otherwise
        # travel on to `main`, which logs it with the lock still held.
        with pytest.raises(_ProcessEnded):
            self._run_serve(tmp_path, monkeypatch, order, commit_failures=[failure])

        assert order == [
            "probe",
            "prepare",
            "prepared",
            "control",
            "commit",
            "failed",
            "diagnostic",
        ]
        liveness = daemon_owner.get_liveness()
        assert liveness.state.retirement is Retirement.UNCERTAIN_PUBLICATION
        assert not liveness.admitting
        assert len(exited) == 1 and isinstance(exited[0], DaemonLock)

    def test_a_call_reaching_an_ambiguous_owner_is_refused_unrun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A frontend that read the descriptor the rename may have published.

        Its call arrives on the first turn the endpoint gets after the failed
        rename, through the real admission middleware. It is refused with the
        owner's signed retiring answer, which the frontend reads as never sent,
        so its call core looks for a successor and retries there, or reports
        the call lost when none is found. Nothing reaches the tool.
        """
        import asyncio
        from unittest.mock import MagicMock

        from linkedin_mcp_server.daemon import proxy
        from linkedin_mcp_server.daemon.core import call as calls
        from linkedin_mcp_server.daemon.liveness import (
            CALL_HEADER,
            OwnerCallLivenessMiddleware,
            new_call_id,
        )

        order: list[str] = []
        announced: list[str] = []
        ran: list[str] = []
        answers: list[object] = []
        marker = new_call_id()
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_http_headers",
            lambda **_kw: {CALL_HEADER: marker},
        )

        class _Server:
            started = True
            should_exit = False

            def __init__(self, stand_down: Callable[[], None]) -> None:
                pass

            async def serve(self, sockets: object = None) -> None:
                while "commit" not in order:
                    await asyncio.sleep(0)

                async def tool(_context: Any) -> str:
                    ran.append("the tool")
                    return "the result"

                context = MagicMock()
                context.message.name = "send_message"
                context.fastmcp_context = None
                middleware = OwnerCallLivenessMiddleware()
                answers.append(await middleware.on_call_tool(context, tool))  # ty: ignore
                while not self.should_exit:
                    await asyncio.sleep(0)

        def exit_hard(lock: object) -> NoReturn:
            raise _ProcessEnded

        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)

        with pytest.raises(_ProcessEnded):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_error=OSError("rename reply was lost"),
                server_factory=_Server,
                announced=announced,
            )

        assert ran == []
        [answer] = answers
        attachment = SimpleNamespace(
            descriptor=SimpleNamespace(instance_id=announced[-1])
        )
        refusal = proxy._owner_refused(answer, cast(Any, attachment))
        assert refusal is proxy.OwnerFailure.RETIRING

        def recovered(found: bool) -> list[calls.Effect]:
            call, [attempt] = calls.arrive(now=0.0, budget=60.0, sign_in_share=0.5)
            assert isinstance(attempt, calls.Attempt)
            lost = calls.OwnerLost(
                attempt.op, 1.0, announced[-1], True, (announced[-1], refusal)
            )
            call, effects = calls.on_event(call, lost)
            [recover] = [e for e in effects if isinstance(e, calls.Recover)]
            return calls.on_event(call, calls.Recovered(recover.op, 2.0, found))[1]

        [retry] = recovered(True)
        assert isinstance(retry, calls.Attempt)
        assert retry.kind is calls.AttemptKind.RETRY
        [lost] = recovered(False)
        assert isinstance(lost, calls.ReportLost)

    def test_an_ambiguous_owner_whose_endpoint_will_not_stop_still_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The failed-startup bound holds, and the hard exit frees the election."""
        import asyncio

        exited: list[object] = []
        # Ignores `should_exit`, but gives up eventually, so an owner that
        # serves on fails this case instead of hanging it.
        gives_up = time.monotonic() + 5.0

        class _Stubborn:
            started = True
            should_exit = False

            def __init__(self, stand_down: Callable[[], None]) -> None:
                pass

            async def serve(self, sockets: object = None) -> None:
                while time.monotonic() < gives_up:
                    await asyncio.sleep(0.001)

        def exit_hard(lock: object) -> None:
            exited.append(lock)
            # The watchdog shares this bound and may fire first, from its own
            # thread, where raising would only print; the owner's exit ends it.
            if threading.current_thread() is threading.main_thread():
                raise _ProcessEnded

        monkeypatch.setattr(daemon_owner, "_FAILED_STARTUP_SHUTDOWN_SECONDS", 0.05)
        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)
        started = time.monotonic()

        with pytest.raises(_ProcessEnded):
            self._run_serve(
                tmp_path,
                monkeypatch,
                [],
                commit_error=OSError("rename reply was lost"),
                server_factory=_Stubborn,
            )

        assert time.monotonic() - started < 2.0
        # The first exit ends a real process; later ones are reached only
        # because the test's exit returns control.
        assert exited and isinstance(exited[0], DaemonLock)

    def test_an_ambiguous_owner_whose_log_stalls_still_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The log sits on the storage that stalled the rename.

        FAILED is already out when the diagnostic blocks, and the hard exit
        arrives from the watchdog, since no await can interrupt the write.
        """
        order: list[str] = []
        exited: list[tuple[threading.Thread, object]] = []
        released = threading.Event()

        def exit_hard(lock: object) -> None:
            exited.append((threading.current_thread(), lock))
            released.set()
            # The watchdog's exit ends a real process from its own thread; here
            # it only releases the log, and the owner's own exit ends the case.
            if threading.current_thread() is threading.main_thread():
                raise _ProcessEnded

        def stalled_log(*args: object, **kwargs: object) -> None:
            order.append("diagnostic")
            # Its own deadline, so a missing watchdog fails instead of hanging.
            released.wait(5.0)

        monkeypatch.setattr(daemon_owner, "_FAILED_STARTUP_SHUTDOWN_SECONDS", 0.05)
        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)
        monkeypatch.setattr(daemon_owner.logger, "warning", stalled_log)
        started = time.monotonic()

        with pytest.raises(_ProcessEnded):
            self._run_serve(
                tmp_path,
                monkeypatch,
                order,
                commit_error=OSError("rename reply was lost"),
            )

        assert time.monotonic() - started < 2.0
        assert order[-2:] == ["failed", "diagnostic"]
        [(thread, lock), *_] = exited
        assert thread is not threading.main_thread()
        assert isinstance(lock, DaemonLock)

    def test_an_ambiguous_owner_cancelled_while_stopping_still_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A shutdown signal that cancels the stop does not skip the hard exit."""
        import asyncio

        exited: list[object] = []
        stops: list[object] = []

        async def cancelled_stop(*args: object, **kwargs: object) -> None:
            # Only the first stop is cancelled; the outer handler's own stop is
            # reached only because the test's exit raises instead of ending.
            stops.append(args)
            if len(stops) == 1:
                raise asyncio.CancelledError

        def exit_hard(lock: object) -> NoReturn:
            exited.append(lock)
            raise _ProcessEnded

        monkeypatch.setattr(daemon_owner, "_stop_within", cancelled_stop)
        monkeypatch.setattr(daemon_owner, "_exit_hard", exit_hard)

        with pytest.raises(_ProcessEnded):
            self._run_serve(
                tmp_path,
                monkeypatch,
                [],
                commit_error=OSError("rename reply was lost"),
            )

        assert len(exited) == 1 and isinstance(exited[0], DaemonLock)

    def test_preflight_failure_aborts_without_reading_canonical_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "read",
            lambda root: pytest.fail("a preflight failure read canonical state"),
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

    def test_initial_state_preparation_failure_aborts_without_reading_state(
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
            lambda root: pytest.fail("state preparation failure read canonical state"),
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


class TestElectionSettlement:
    @pytest.mark.parametrize("failure", [Attempt.ABORTED, OSError("spawn failed")])
    def test_terminal_local_result_observes_a_concurrent_winner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Attempt | OSError,
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        published: list[str] = []
        probed: list[str] = []
        clock = [0.0]

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                return daemon_module._inspect(profile.parent, profile, config)

        def finish(*args: object, **kwargs: object) -> Attempt:
            published.append(_publish_stale_owner(profile.parent, profile, config))
            if isinstance(failure, OSError):
                raise failure
            return failure

        def answer(attachment: Attachment, timeout: float) -> Reach:
            probed.append(attachment.descriptor.instance_id)
            return Reach.ANSWERED

        def monotonic() -> float:
            now = clock[0]
            clock[0] += 0.001
            return now

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(spawn_module, "_start_owner", finish)
        monkeypatch.setattr(election_module.time, "monotonic", monotonic)
        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=0.2,
            settlement_seconds=0.2,
            connect=answer,
        )

        assert outcome.worth_connecting
        assert probed == published

    @pytest.mark.parametrize("failure", [Attempt.ABORTED, OSError("spawn failed")])
    def test_early_terminal_local_result_gets_only_one_settlement_budget(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Attempt | OSError,
    ):
        profile = _profile(tmp_path)
        clock = [0.0]

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                return OwnerLookup(state=OwnerState.ABSENT)

        def finish(*args: object, **kwargs: object) -> Attempt:
            if isinstance(failure, OSError):
                raise failure
            return failure

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(spawn_module, "_start_owner", finish)
        monkeypatch.setattr(election_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=90,
            settlement_seconds=2,
            connect=lambda attachment, timeout: pytest.fail("absent owner was probed"),
        )

        assert not outcome.worth_connecting
        assert 1.9 <= clock[0] <= 2.01, clock[0]

    @pytest.mark.parametrize("failure", [Attempt.ABORTED, OSError("spawn failed")])
    def test_early_terminal_local_result_observes_winner_within_short_window(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Attempt | OSError,
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        _publish_stale_owner(profile.parent, profile, config)
        winner = daemon_module._inspect(profile.parent, profile, config)
        clock = [0.0]

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                if clock[0] >= 1.0:
                    return winner
                return OwnerLookup(state=OwnerState.ABSENT)

        def finish(*args: object, **kwargs: object) -> Attempt:
            if isinstance(failure, OSError):
                raise failure
            return failure

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(spawn_module, "_start_owner", finish)
        monkeypatch.setattr(election_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=90,
            settlement_seconds=2,
            connect=lambda attachment, timeout: Reach.ANSWERED,
        )

        assert outcome.worth_connecting
        assert 1.0 <= clock[0] < 2.0

    def test_active_deadline_exit_keeps_the_global_settlement_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        _publish_stale_owner(profile.parent, profile, config)
        winner = daemon_module._inspect(profile.parent, profile, config)
        clock = [0.0]

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                if clock[0] >= 100.0:
                    return winner
                return OwnerLookup(state=OwnerState.ABSENT)

        def exhaust_active_budget(*args: object, **kwargs: object) -> Attempt:
            clock[0] = 90.0
            return Attempt.CONTENDED

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(spawn_module, "_start_owner", exhaust_active_budget)
        monkeypatch.setattr(election_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=90,
            settlement_seconds=15,
            connect=lambda attachment, timeout: Reach.ANSWERED,
        )

        assert outcome.worth_connecting
        assert 100.0 <= clock[0] < 105.0

    def test_settlement_neither_starts_nor_turns_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        _publish_stale_owner(profile.parent, profile, config, package_version="1.0.0")
        monkeypatch.setattr(
            spawn_module,
            "_start_owner",
            lambda *a, **k: pytest.fail("settlement started an owner"),
        )
        monkeypatch.setattr(
            election_module,
            "_ask_to_stand_down",
            lambda attachment: pytest.fail("settlement requested turnover"),
        )
        probes: list[float] = []

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=0,
            settlement_seconds=0.05,
            connect=lambda attachment, timeout: (
                probes.append(timeout) or Reach.ANSWERED
            ),
        )

        assert not outcome.worth_connecting
        assert not probes, "a stale owner was accepted during settlement"

    def test_descriptor_and_probe_share_the_absolute_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        _publish_stale_owner(profile.parent, profile, config)
        probe_budgets: list[float] = []

        def answer(_attachment: Attachment, timeout: float) -> Reach:
            probe_budgets.append(timeout)
            return Reach.ANSWERED

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=0,
            settlement_seconds=0.05,
            connect=answer,
        )

        assert outcome.worth_connecting
        assert len(probe_budgets) == 1
        assert 0 < probe_budgets[0] <= 0.05 + 1e-12

    def test_zero_total_budget_starts_no_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        monkeypatch.setattr(
            daemon_module,
            "_inspect",
            lambda *a: pytest.fail("zero budget started a descriptor read"),
        )
        began = time.monotonic()

        outcome = obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=0,
            settlement_seconds=0,
            connect=lambda attachment, timeout: pytest.fail("zero budget probed"),
        )

        assert not outcome.worth_connecting
        assert time.monotonic() - began < 0.1

    def test_one_inspector_spans_active_election_and_settlement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        real_inspector = daemon_module._DescriptorInspector
        inspectors: list[object] = []

        def build_inspector(*args: object, **kwargs: object):
            inspector = real_inspector(*cast(Any, args), **cast(Any, kwargs))
            inspectors.append(inspector)
            return inspector

        monkeypatch.setattr(election_module, "_DescriptorInspector", build_inspector)
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.CONTENDED
        )
        obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=0.02,
            settlement_seconds=0.02,
            connect=lambda attachment, timeout: Reach.REFUSED,
        )

        assert len(inspectors) == 1

    def test_read_timeout_does_not_replace_last_concrete_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        release = threading.Event()
        reads = 0

        def inspect(*args: object) -> OwnerLookup:
            nonlocal reads
            reads += 1
            if reads == 1:
                return OwnerLookup(state=OwnerState.ABSENT, reason="observed absent")
            release.wait()
            return OwnerLookup(state=OwnerState.UNTRUSTED, reason="late")

        monkeypatch.setattr(daemon_module, "_inspect", inspect)
        monkeypatch.setattr(daemon_module, "_DESCRIPTOR_READ_SECONDS", 0.005)
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.CONTENDED
        )
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _config(profile),
                deadline_seconds=0.01,
                settlement_seconds=0.02,
                connect=lambda attachment, timeout: Reach.REFUSED,
            )
        finally:
            release.set()

        assert outcome.attachment_lookup.state is OwnerState.ABSENT
        assert outcome.attachment_lookup.reason == "observed absent"

    def test_descriptor_error_is_paced_and_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        clock = [0.0]
        reads = 0

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                nonlocal reads
                reads += 1
                raise daemon_descriptor_module.DescriptorError("broken descriptor")

        def monotonic() -> float:
            now = clock[0]
            clock[0] += 0.001
            return now

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(election_module.time, "monotonic", monotonic)
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=0,
            settlement_seconds=0.5,
            connect=lambda attachment, timeout: pytest.fail(
                "an untrusted descriptor was probed"
            ),
        )

        assert reads < 20, f"descriptor error started {reads} reads"
        assert outcome.attachment_lookup.state is OwnerState.UNTRUSTED
        assert outcome.attachment_lookup.reason == "broken descriptor"

    def test_stale_settlement_owner_is_buried_and_paced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        _publish_stale_owner(profile.parent, profile, config, package_version="1.0.0")
        lookup = daemon_module._inspect(profile.parent, profile, config)
        clock = [0.0]
        reads = 0

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                nonlocal reads
                reads += 1
                return lookup

        def monotonic() -> float:
            now = clock[0]
            clock[0] += 0.001
            return now

        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(election_module.time, "monotonic", monotonic)
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        monkeypatch.setattr(
            election_module,
            "_ask_to_stand_down",
            lambda attachment: pytest.fail("settlement requested turnover"),
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            config,
            deadline_seconds=0,
            settlement_seconds=0.5,
            connect=lambda attachment, timeout: pytest.fail(
                "settlement probed a stale owner"
            ),
        )

        assert 2 <= reads < 20, f"stale owner started {reads} reads"
        assert not outcome.worth_connecting

    @pytest.mark.parametrize("stage", ["loop", "lookup", "start", "paced"])
    def test_each_active_deadline_exit_settles_until_the_global_deadline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stage: str,
    ):
        """Wherever the active budget runs out, the quiet tail follows it.

        Observed through a clock only reads and sleeps move: the tail keeps
        reading after the active deadline, starts nothing, and ends the election
        at the global settlement deadline, neither sooner nor later.
        """
        profile = _profile(tmp_path)
        clock = [1.0 if stage == "loop" else 0.0]
        # The active deadline is one second after entry and the tail ends one
        # second after that. Every stage but "loop" jumps the clock to it.
        active_until = clock[0] + 1.0 if stage == "loop" else 1.0
        reads: list[float] = []
        late_starts: list[float] = []

        class Inspector:
            def __init__(self, *args: object, **kwargs: object) -> None:
                return None

            def require_fresh_inspection(self) -> None:
                return None

            def inspect_until(self, *, timeout: float) -> OwnerLookup:
                reads.append(clock[0])
                if stage == "lookup" and len(reads) == 1:
                    clock[0] = 1.0
                if stage == "paced" and len(reads) == 2:
                    clock[0] = 1.0
                return OwnerLookup(state=OwnerState.ABSENT)

        def start(*args: object, **kwargs: object) -> Attempt:
            if clock[0] >= active_until:
                late_starts.append(clock[0])
            if stage == "start":
                clock[0] = 1.0
            return Attempt.CONTENDED

        monkeypatch.setattr(election_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(
            election_module.time,
            "sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        monkeypatch.setattr(spawn_module, "_IS_WINDOWS", False)
        monkeypatch.setattr(election_module, "_DescriptorInspector", Inspector)
        monkeypatch.setattr(spawn_module, "_start_owner", start)

        outcome = obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=1.0,
            settlement_seconds=1.0,
            connect=lambda attachment, timeout: Reach.REFUSED,
        )

        assert not outcome.worth_connecting
        assert not late_starts, f"started after the active deadline at {late_starts}"
        tail_until = active_until + 1.0
        assert any(active_until <= at < tail_until for at in reads), "no tail reads"
        assert all(at < tail_until for at in reads), f"read past the tail: {reads}"
        assert clock[0] == pytest.approx(tail_until)


class TestOwnerBootstrapOrdering:
    def _prepare(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[list[str], Path]:
        profile = _profile(tmp_path)
        events: list[str] = []

        class Bootstrap:
            def __init__(self, stream: object) -> None:
                return None

            def report(self, code: str) -> None:
                events.append(f"bootstrap:{code}")

            def attached(self, log_path: Path, nonce: str) -> None:
                events.append("bootstrap:attached")

            def close(self) -> None:
                events.append("bootstrap:closed")

        class Handshake:
            def __init__(self, stream: object, nonce: str) -> None:
                return None

            def retry(self) -> None:
                events.append("retry")

            def abort(self) -> None:
                events.append("abort")

            def fail(self) -> None:
                events.append("fail")

            def close(self) -> None:
                events.append("handshake:closed")

        handover = daemon_config.OwnerHandover(_config(profile), _HANDSHAKE_NONCE)
        monkeypatch.setattr(
            daemon_owner.WindowsJob, "verify_current_process", lambda name: None
        )
        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", Bootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", Handshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_startup_channel", lambda: None)
        monkeypatch.setattr(daemon_owner, "_read_handover", lambda *a, **k: handover)
        monkeypatch.setattr(daemon_owner, "set_config", lambda config: None)
        monkeypatch.setattr(daemon_owner, "set_headless", lambda headless: None)
        monkeypatch.setattr(daemon_owner, "set_process_role", lambda role: None)
        monkeypatch.setattr(
            daemon_owner, "auth_root_dir", lambda selected: profile.parent
        )
        return events, profile.parent

    def test_lock_loser_never_touches_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        events, _auth_root = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda *a, **k: events.append("lock") or None,
        )
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda root: pytest.fail("lock loser attached the daemon log"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "configure_logging",
            lambda **kwargs: pytest.fail("lock loser configured logging"),
        )

        assert daemon_owner.main(["--job-name", "test-owner-job"]) == 0
        assert events[:2] == ["lock", "retry"]

    def test_winner_takes_lock_before_log_and_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        events, auth_root = self._prepare(tmp_path, monkeypatch)

        class Lock:
            def release(self) -> None:
                events.append("released")

        async def serve(**kwargs: object) -> int:
            events.append("serve")
            return 0

        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda *a, **k: events.append("lock") or Lock(),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda root: events.append("log") or auth_root / "daemon.log",
        )
        monkeypatch.setattr(
            daemon_owner,
            "configure_logging",
            lambda **kwargs: events.append("logging"),
        )
        monkeypatch.setattr(daemon_owner, "_serve", serve)

        assert daemon_owner.main(["--job-name", "test-owner-job"]) == 0
        assert events.index("lock") < events.index("log")
        assert events.index("log") < events.index("bootstrap:attached")
        assert events.index("bootstrap:attached") < events.index("logging")
        assert events.index("logging") < events.index("serve")
        assert events.count("released") == 1

    def test_log_failure_after_lock_releases_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        events, _auth_root = self._prepare(tmp_path, monkeypatch)

        class Lock:
            def release(self) -> None:
                events.append("released")

        monkeypatch.setattr(daemon_owner, "_take_lock", lambda *a, **k: Lock())
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda root: (_ for _ in ()).throw(OSError("log unavailable")),
        )

        assert daemon_owner.main(["--job-name", "test-owner-job"]) == 1
        assert f"bootstrap:{daemon_owner.BOOTSTRAP_LOG}" in events
        assert "abort" in events
        assert events.count("released") == 1

    def test_lock_failure_uses_bootstrap_diagnostic_before_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        events, _auth_root = self._prepare(tmp_path, monkeypatch)
        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda *a, **k: (_ for _ in ()).throw(DaemonLockError("unusable")),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda root: pytest.fail("lock failure attached the log"),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *a, **k: pytest.fail("lock failure logged before configuration"),
        )

        assert daemon_owner.main(["--job-name", "test-owner-job"]) == 1
        assert events[0] == f"bootstrap:{daemon_owner.BOOTSTRAP_LOCK}"
        assert "abort" in events


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
        from linkedin_mcp_server.daemon.discovery import OwnerLookup

        absent = ElectionOutcome(OwnerLookup(state=OwnerState.ABSENT))

        assert not absent.worth_connecting


def _prepare_browser_cache(profile: Path) -> None:
    """Satisfy the readiness gate without borrowing or installing a browser."""
    from test_bootstrap import _materialize_install, _write_metadata

    from linkedin_mcp_server import bootstrap

    targets = bootstrap._patchright_install_targets()
    assert targets is not None
    cache = profile.parent / "patchright-browsers"
    _materialize_install(cache, [f"chromium-{targets['chromium-']}"])
    _write_metadata(
        profile.parent / "browser-install.json",
        cache,
        patchright_version=bootstrap._patchright_pkg_version(),
        installed_targets={"chromium-": True, "chromium_headless_shell-": False},
    )


class TestTheStartupFramesAreNotPlatformTranslated:
    """The frontend reads lengths and bytes, so the owner has to write them exact."""

    def test_a_length_holding_a_line_feed_crosses_the_pipe_unchanged(self):
        """The header is binary, and one of its bytes may be a line feed.

        JSON escapes every control character, so a body never carries a raw LF;
        a length can. 266 bytes is ``00 00 01 0A`` on the wire, and a text-mode
        descriptor on Windows would write that last byte as CR LF, shifting
        every frame after it. The owner sets its descriptors to binary mode for
        that reason; on a POSIX host this pins the round trip it protects.
        """
        nonce = "0123456789abcdef" * 4
        empty = startup.encode(startup.message(startup.PREPARED, nonce, instance=""))
        instance = "x" * (266 - (len(empty) - 4))
        expected = startup.encode(
            startup.message(startup.PREPARED, nonce, instance=instance)
        )
        assert b"\n" in expected[:4]

        inbound, unused = os.pipe()
        os.close(unused)
        read_end, write_end = os.pipe()
        channel = startup.OwnerChannel(inbound, write_end)
        try:
            channel.send(startup.PREPARED, nonce, instance=instance)
            channel.close_output()
            received = b""
            while chunk := os.read(read_end, 4096):
                received += chunk
        finally:
            channel.close_output()
            channel.close()
            os.close(read_end)

        assert received == expected


def _with_published_fields(auth_root: Path, **fields: object) -> None:
    """Rewrite the published descriptor as another build would have written it."""
    path = daemon_descriptor_module.descriptor_path(auth_root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(fields)
    path.write_text(json.dumps(raw), encoding="utf-8")


class TestAnOwnerThisBuildMayOnlyControl:
    """Protocol and configuration mismatches, and the one route that survives them.

    Each election runs with starting an owner stood in for, recorded rather than
    spawned, so what is observed is whether the election contends for the lock,
    and never a real child.
    """

    @staticmethod
    def _elect(
        auth_root: Path,
        profile: Path,
        config: AppConfig,
        monkeypatch: pytest.MonkeyPatch,
        *,
        reach: Reach = Reach.ANSWERED,
        buried: frozenset[str] = frozenset(),
        deadline: float = 0.3,
    ) -> tuple[ElectionOutcome, list[str], list[str], list[str]]:
        asked: list[str] = []
        probed: list[str] = []
        contended: list[str] = []
        monkeypatch.setattr(
            election_module,
            "_ask_to_stand_down",
            lambda attachment: asked.append(attachment.descriptor.instance_id),
        )

        def start(*_args: object, **_kwargs: object) -> Attempt:
            contended.append("the lock")
            return Attempt.CONTENDED

        monkeypatch.setattr(spawn_module, "_start_owner", start)

        def connect(attachment: Attachment, _timeout: float) -> Reach:
            probed.append(attachment.descriptor.instance_id)
            return reach

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=deadline,
            settlement_seconds=0,
            connect=connect,
            buried=buried,
        )
        return outcome, asked, probed, contended

    def test_an_older_protocol_owner_is_turned_over_and_never_attached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # This namespace starts at the control floor, so there is no older
        # protocol yet. A build one protocol later meets this owner like this.
        monkeypatch.setattr(
            daemon_descriptor_module,
            "PROTOCOL_VERSION",
            daemon_descriptor_module.CONTROL_FLOOR_PROTOCOL + 1,
        )
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        instance = _publish_stale_owner(
            auth_root, profile, config, package_version="1.0.0"
        )
        _with_published_fields(
            auth_root, protocol_version=daemon_descriptor_module.CONTROL_FLOOR_PROTOCOL
        )

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert asked == [instance], "the older owner was not asked to stand down"
        assert probed == [], "an owner of another protocol was probed for tools"
        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.attachment is None
        assert contended, "the election did not go on to the lock"

    @pytest.mark.parametrize("package_version", [__version__, "99.0.0"])
    def test_a_same_or_newer_protocol_mismatch_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_version: str
    ):
        # Written off and not asked: the election contends for a lock that owner
        # holds, and while it lives that ends in this client's own browser.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(
            auth_root, profile, config, package_version=package_version
        )
        _with_published_fields(
            auth_root, protocol_version=daemon_descriptor_module.PROTOCOL_VERSION + 1
        )

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert asked == []
        assert probed == []
        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.attachment is None
        assert outcome.attachment_lookup.fallback is None
        assert contended, "the election did not go on to the lock"

    def test_a_live_owner_of_this_version_with_another_configuration_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The contract's immediate fallback: probed once, found alive, and the
        # election returns at once without contending for the lock it holds.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        instance = _publish_stale_owner(auth_root, profile, theirs)

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert probed == [instance]
        assert asked == [], "a same-version owner was asked to stand down"
        assert (
            outcome.attachment_lookup.fallback
            is daemon_module.DirectFallback.LIVE_RIVAL
        )
        assert not outcome.worth_connecting
        assert contended == [], "the election contended for a live rival's lock"

    def test_a_silent_owner_of_this_version_with_another_configuration_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Silence proves nothing about the owner's life, so it authorizes no
        # turnover; it does not make the owner usable either. One probe, then
        # this client's own browser, well inside a budget the old loop would
        # have spent probing the same silent owner again and again.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        instance = _publish_stale_owner(auth_root, profile, theirs)

        began = time.monotonic()
        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch, reach=Reach.SILENT, deadline=5.0
        )
        elapsed = time.monotonic() - began

        assert probed == [instance], "a silent rival was probed more than once"
        assert asked == [], "a silent owner was asked to stand down"
        assert contended == [], "the election contended for a silent rival's lock"
        assert (
            outcome.attachment_lookup.fallback
            is daemon_module.DirectFallback.SILENT_RIVAL
        )
        assert "live" not in outcome.attachment_lookup.reason
        assert not outcome.worth_connecting
        assert elapsed < 2.0, f"the election ran on for {elapsed:.2f}s"

    def test_a_dead_owner_of_another_configuration_is_leftovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # An owner that went idle leaves its descriptor behind on purpose. Read
        # as a live rival, every later client with another configuration would
        # drive its own browser for good.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        instance = _publish_stale_owner(auth_root, profile, theirs)

        outcome, _asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch, reach=Reach.REFUSED
        )

        assert probed == [instance], "a buried leftover was probed more than once"
        assert outcome.attachment_lookup.fallback is None
        assert contended, "the election did not take the lock from a corpse"

    def test_an_older_owner_of_another_configuration_is_turned_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Proved for control, an older owner is asked whatever its
        # configuration. Left running, it kept the browser from every newer
        # frontend whose fingerprint differed from its own.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        instance = _publish_stale_owner(
            auth_root, profile, theirs, package_version="1.0.0"
        )

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert asked == [instance], "the older owner was not asked to stand down"
        assert probed == []
        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.attachment is None
        assert outcome.attachment_lookup.fallback is None
        assert contended, "the election did not go on to the lock"

    @pytest.mark.parametrize("package_version", ["99.0.0", "local-build"])
    def test_a_newer_or_unknown_version_of_another_configuration_is_waited_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_version: str
    ):
        # Neither asked nor probed: only an older owner is turned over and only
        # this version is a rival to leave alone. The election waits on the
        # lock.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        _publish_stale_owner(
            auth_root, profile, theirs, package_version=package_version
        )

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert (asked, probed) == ([], [])
        assert outcome.attachment_lookup.fallback is None
        assert contended

    @pytest.mark.parametrize("unproved", ["runtime", "profile", "token"])
    def test_an_older_owner_not_proved_for_control_is_never_asked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        unproved: str,
    ):
        # Turnover needs the whole proof a control-only pair carries. An owner
        # of another runtime or profile, or one whose token does not match its
        # descriptor, gets no request whatever its version.
        profile = _profile(tmp_path)
        config = _config(profile)
        theirs = _config(profile)
        theirs.browser.headless = not config.browser.headless
        auth_root = profile.parent
        served = profile
        if unproved == "profile":
            served = auth_root / "another-profile"
            served.mkdir()
        instance = _publish_stale_owner(
            auth_root, served, theirs, package_version="1.0.0"
        )
        if unproved == "runtime":
            _with_published_fields(auth_root, runtime_id="another-runtime")
        elif unproved == "token":
            daemon_descriptor_module.token_path(auth_root, instance).write_text(
                new_token(), encoding="utf-8"
            )

        outcome, asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch
        )

        assert (asked, probed) == ([], [])
        assert outcome.attachment_lookup.attachment is None
        assert contended

    def test_a_buried_owner_is_not_returned_even_though_it_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A retiring owner answers the ping this election probes with. The
        # proxy that found it retiring passes it as buried, and it is neither
        # probed nor returned; the control is the same owner not buried.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        instance = _publish_stale_owner(auth_root, profile, config)

        outcome, _asked, probed, contended = self._elect(
            auth_root, profile, config, monkeypatch, buried=frozenset({instance})
        )
        control, *_ = self._elect(auth_root, profile, config, monkeypatch)

        assert probed == []
        assert not outcome.worth_connecting
        assert contended
        assert control.worth_connecting
        assert control.attachment_lookup.attachment is not None
        assert control.attachment_lookup.attachment.descriptor.instance_id == instance
