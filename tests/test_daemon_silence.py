"""What a caller is told when the shared owner is there and does not answer.

An owner can be alive and silent: stopped, its event loop blocked, or inside a
native call that holds the interpreter. It keeps the daemon lock, so "restart
your MCP client" does nothing: the owner is detached. These tests pin the
words a caller reads once that silence was observed, and that the observation
changes nothing else: no burial, no extra dispatch, no repeat.

The observation comes only from the control surface, a heartbeat or preflight
that ran out its read timeout or an election probe that ran out its budget,
and every case here gets it through the production pipeline rather than by
handing the formatter a value.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from owner_hop import reach_owners_in_process
from test_daemon_proxy import _attachment, _backend
from test_profile_lease_integration import _hold_profile

from linkedin_mcp_server import __version__
from linkedin_mcp_server.daemon import election as election_module
from linkedin_mcp_server.daemon import spawn as spawn_module
from linkedin_mcp_server.daemon import proxy as proxy_module
from linkedin_mcp_server.daemon.core.lookup import Reach
from linkedin_mcp_server.daemon.core.spawn import Attempt
from linkedin_mcp_server.daemon.discovery import (
    Attachment,
    DirectFallback,
    Mismatch,
    OwnerLookup,
    OwnerState,
    Unanswered,
)
from linkedin_mcp_server.daemon.forwarder import OwnerUnreachableError
from linkedin_mcp_server.daemon.proxy import (
    FrontendCallHeartbeatMiddleware,
    FrontendCallMiddleware,
)

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="stops the owner with SIGSTOP, which Windows lacks"
)

#: Today's text, for an owner that is gone or that nothing found silent.
_GONE = (
    "This server lost the shared browser process and could not reach a new one. "
    "Reconnect or restart your MCP client to start a new one."
)


def _silent(pid: int) -> str:
    """The text for an owner found silent, as a listing and a sent call read it."""
    return (
        f"The shared browser process (pid {pid}) did not respond. It may still "
        "hold the browser, so restarting only your MCP client may not release it."
    )


def _silent_and_unsent(pid: int) -> str:
    return _silent(pid) + " Nothing was sent."


def _published(tmp_path: Path, *, pid: int, control_only: bool = False) -> Attachment:
    """An owner of this build, as a completed descriptor read names it."""
    attachment = _attachment(tmp_path)
    descriptor = dataclasses.replace(
        attachment.descriptor, package_version=__version__, pid=pid
    )
    return dataclasses.replace(
        attachment, descriptor=descriptor, control_only=control_only
    )


def _found(attachment: Attachment) -> OwnerLookup:
    if attachment.control_only:
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            attachment=attachment,
            mismatch=Mismatch.CONFIGURATION,
        )
    return OwnerLookup(state=OwnerState.ATTACHABLE, attachment=attachment)


class _ClockedElection:
    """The production election against a scripted descriptor and probe.

    Only the edges are stood in: the descriptor read, the probe's verdict, the
    clock and the child start. A silent probe spends its whole budget, which is
    what silence does to ``_reachable``'s outer wait; anything else costs a
    hundredth of a second. Every lookup, finding and decision is production's.

    *reads* are what each completed read returns, with what it costs, the last
    one repeating. *verdicts* are per instance, the last one repeating.
    """

    def __init__(
        self,
        reads: list[tuple[Attachment, float]],
        verdicts: dict[str, list[Reach]],
        *,
        hold: threading.Event | None = None,
    ) -> None:
        self.now = 0.0
        self._reads = reads
        self._verdicts = {name: list(script) for name, script in verdicts.items()}
        self._hold = hold
        self.probes: list[tuple[str, Reach]] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    def inspect_until(self, *, timeout: float) -> OwnerLookup:
        del timeout
        attachment, cost = (
            self._reads[0] if len(self._reads) == 1 else self._reads.pop(0)
        )
        self.sleep(cost)
        return _found(attachment)

    def require_fresh_inspection(self) -> None:
        pass

    def reach(self, attachment: Attachment, budget: float) -> Reach:
        if self._hold is not None:
            self._hold.wait(timeout=30)
        script = self._verdicts[attachment.descriptor.instance_id]
        verdict = script[0] if len(script) == 1 else script.pop(0)
        self.probes.append((attachment.descriptor.instance_id, verdict))
        self.sleep(budget if verdict is Reach.SILENT else 0.01)
        return verdict

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every ``obtain_owner`` in this test run against the script."""
        monkeypatch.setattr(
            election_module,
            "time",
            SimpleNamespace(monotonic=self.monotonic, sleep=self.sleep),
        )
        monkeypatch.setattr(
            election_module, "_DescriptorInspector", lambda *a, **k: self
        )
        monkeypatch.setattr(election_module, "_reachable", self.reach)
        # Nothing to start: the published owner holds the lock in every case.
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )
        real = election_module.obtain_owner
        monkeypatch.setattr(
            election_module,
            "obtain_owner",
            lambda *args, **kwargs: real(
                *args,
                deadline_seconds=self.deadline,
                settlement_seconds=self.settlement,
                **kwargs,
            ),
        )

    deadline = 1.0
    settlement = 0.0


def _elect(clock: _ClockedElection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock.install(monkeypatch)
    profile = tmp_path / "profile"
    return election_module.obtain_owner(
        profile.parent, profile, _backend(_attachment(tmp_path), tmp_path).config
    )


class TestWhatTheElectionHeard:
    """The election's own observation, carried beside its decision."""

    @pytest.fixture(autouse=True)
    def _short_probes(self, monkeypatch: pytest.MonkeyPatch):
        # Room for a second probe inside a one-second election.
        monkeypatch.setattr(election_module, "_REACHABLE_SECONDS", 0.1)

    def test_a_silence_survives_a_last_pass_that_never_probed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The measured shape: silent, then out of time before the next probe.

        The election returns the second pass's lookup, which says only that
        nothing was probed. The silence it heard first is what a caller is
        told about.
        """
        owner = _published(tmp_path, pid=4242)
        clock = _ClockedElection(
            [(owner, 0.01), (owner, 0.11)],
            {owner.descriptor.instance_id: [Reach.SILENT]},
        )
        clock.deadline, clock.settlement = 0.1, 0.1
        monkeypatch.setattr(election_module, "_REACHABLE_SECONDS", 5.0)

        outcome = _elect(clock, tmp_path, monkeypatch)

        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.reason == (
            "the published daemon was not probed before the deadline"
        )
        assert outcome.unanswered == Unanswered(owner.descriptor.instance_id, 4242)

    def test_a_read_of_another_generation_forgets_the_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The same shape, except the second read names a newer generation.
        # Nothing was heard from that one, and the silent one is no longer
        # what is published.
        silent = _published(tmp_path, pid=4242)
        newer = _published(tmp_path, pid=4343)
        clock = _ClockedElection(
            [(silent, 0.01), (newer, 0.11)],
            {silent.descriptor.instance_id: [Reach.SILENT]},
        )
        clock.deadline, clock.settlement = 0.1, 0.1
        monkeypatch.setattr(election_module, "_REACHABLE_SECONDS", 5.0)

        outcome = _elect(clock, tmp_path, monkeypatch)

        assert outcome.attachment_lookup.reason == (
            "the published daemon was not probed before the deadline"
        )
        assert outcome.unanswered is None

    @pytest.mark.parametrize("then", [Reach.ANSWERED, Reach.REFUSED])
    def test_an_answer_or_a_refusal_forgets_the_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: Reach
    ):
        owner = _published(tmp_path, pid=4242)
        instance = owner.descriptor.instance_id
        clock = _ClockedElection([(owner, 0.01)], {instance: [Reach.SILENT, then]})

        outcome = _elect(clock, tmp_path, monkeypatch)

        assert [verdict for _, verdict in clock.probes][:2] == [Reach.SILENT, then]
        assert outcome.worth_connecting is (then is Reach.ANSWERED)
        assert outcome.unanswered is None

    def test_a_silent_owner_keeps_its_silence_through_the_whole_election(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _published(tmp_path, pid=4242)
        clock = _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.SILENT]}
        )

        outcome = _elect(clock, tmp_path, monkeypatch)

        assert len(clock.probes) > 1, "the election probed only once"
        assert not outcome.worth_connecting
        assert outcome.unanswered == Unanswered(owner.descriptor.instance_id, 4242)

    @pytest.mark.parametrize(
        ("verdict", "fallback", "silent"),
        [
            (Reach.SILENT, DirectFallback.SILENT_RIVAL, True),
            (Reach.ANSWERED, DirectFallback.LIVE_RIVAL, False),
        ],
    )
    def test_a_rival_left_alone_is_named_only_when_it_was_silent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verdict: Reach,
        fallback: DirectFallback,
        silent: bool,
    ):
        rival = _published(tmp_path, pid=4242, control_only=True)
        instance = rival.descriptor.instance_id
        clock = _ClockedElection([(rival, 0.01)], {instance: [verdict]})

        outcome = _elect(clock, tmp_path, monkeypatch)

        assert outcome.attachment_lookup.fallback is fallback
        assert outcome.unanswered == (Unanswered(instance, 4242) if silent else None)


def _context(*, read_only: bool = False) -> MagicMock:
    tool = MagicMock()
    tool.annotations = MagicMock(read_only_hint=read_only)
    context = MagicMock()
    context.message.name = "send_connection_request"
    context.fastmcp_context.fastmcp.get_tool = AsyncMock(return_value=tool)
    return context


def _beats(*answers: Any) -> Callable[[Attachment, str], Any]:
    """A heartbeat route answering in order, the last answer repeating.

    An exception is a beat that got no answer, as the transport raises it.
    """
    script = list(answers)

    async def beat(_attachment: Attachment, _call_id: str) -> httpx2.Response:
        answer = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return beat


def _watched() -> httpx2.Response:
    return httpx2.Response(200, json={"watched": False})


async def _call(
    backend: Any,
    beat: Callable[[Attachment, str], Any],
    dispatch: Callable[[Any], Any],
    *,
    budget: float = 30.0,
    read_only: bool = False,
) -> Any:
    """One call through both proxy middlewares, with the hop stood in by *dispatch*."""
    heartbeat = FrontendCallHeartbeatMiddleware(backend)
    heartbeat._beat = beat  # ty: ignore[invalid-assignment]

    async def inner(context: Any) -> Any:
        return await heartbeat.on_call_tool(context, dispatch)  # ty: ignore[invalid-argument-type]

    return await FrontendCallMiddleware(backend, budget=budget).on_call_tool(
        _context(read_only=read_only),
        inner,
    )


async def _never_dispatched(_context: Any) -> Any:
    raise AssertionError("a call whose preflight failed was dispatched")


class TestWhatACallIsTold:
    """The consumer: which observation a call's final answer reads."""

    async def test_a_silent_preflight_and_a_silent_election_say_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _published(tmp_path, pid=4242)
        _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.SILENT]}
        ).install(monkeypatch)

        with pytest.raises(ToolError) as told:
            await _call(
                _backend(owner, tmp_path),
                _beats(httpx2.ReadTimeout("silent")),
                _never_dispatched,
            )

        assert str(told.value) == _silent_and_unsent(4242)

    async def test_a_recovery_that_saw_a_refusal_overrules_a_silent_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The election refused the owner and found nobody else. That is newer
        # than the preflight's silence, and says the owner is gone.
        owner = _published(tmp_path, pid=4242)
        _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.REFUSED]}
        ).install(monkeypatch)

        with pytest.raises(ToolError) as told:
            await _call(
                _backend(owner, tmp_path),
                _beats(httpx2.ReadTimeout("silent")),
                _never_dispatched,
            )

        assert str(told.value) == _GONE

    @pytest.mark.parametrize(
        "failure",
        [
            httpx2.ConnectError("refused"),
            httpx2.ConnectTimeout("backlog full"),
            httpx2.WriteTimeout("unwritten"),
            httpx2.PoolTimeout("no connection"),
        ],
        ids=lambda failure: type(failure).__name__,
    )
    async def test_only_a_read_timeout_is_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ):
        # A call cut off while its recovery is still running keeps what the
        # attempt heard, so only the preflight decides the words here.
        owner = _published(tmp_path, pid=4242)
        hold = threading.Event()
        clock = _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.ANSWERED]}, hold=hold
        )
        clock.install(monkeypatch)
        backend = _backend(owner, tmp_path)

        try:
            with pytest.raises(ToolError) as told:
                await _call(backend, _beats(failure), _never_dispatched, budget=0.5)
        finally:
            hold.set()
            if backend._electing is not None:
                await backend._electing.task

        assert str(told.value) == _GONE

    async def test_a_call_cut_off_during_recovery_keeps_what_its_preflight_heard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The positive control for the row above: the same call, the same held
        # recovery, and a preflight that ran out its read timeout.
        owner = _published(tmp_path, pid=4242)
        hold = threading.Event()
        clock = _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.ANSWERED]}, hold=hold
        )
        clock.install(monkeypatch)
        backend = _backend(owner, tmp_path)

        try:
            with pytest.raises(ToolError) as told:
                await _call(
                    backend,
                    _beats(httpx2.ReadTimeout("silent")),
                    _never_dispatched,
                    budget=0.5,
                )
        finally:
            hold.set()
            if backend._electing is not None:
                await backend._electing.task

        assert str(told.value) == _silent_and_unsent(4242)
        assert backend._unusable == {}, "a silent owner was written off"

    @pytest.mark.parametrize("nothing_was_sent", [True, False])
    async def test_nothing_was_sent_is_said_only_of_a_call_that_sent_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nothing_was_sent: bool
    ):
        # A read-only call that may have left still ends without an owner, since
        # it may be repeated. It is told about the silence, not that nothing
        # went out.
        owner = _published(tmp_path, pid=4242)
        _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: [Reach.SILENT]}
        ).install(monkeypatch)

        async def lost(_context: Any) -> Any:
            raise OwnerUnreachableError(
                instance_id=owner.descriptor.instance_id,
                nothing_was_sent=nothing_was_sent,
                cause=httpx2.ReadTimeout("no answer"),
            )

        with pytest.raises(ToolError) as told:
            await _call(
                _backend(owner, tmp_path), _beats(_watched()), lost, read_only=True
            )

        expected = _silent_and_unsent(4242) if nothing_was_sent else _silent(4242)
        assert str(told.value) == expected

    async def test_an_owner_that_answers_again_is_used_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Silent at the preflight, answering by the election: the same
        # generation, resumed. The call runs on it once and nothing is
        # written off.
        owner = _published(tmp_path, pid=4242)
        instance = owner.descriptor.instance_id
        _ClockedElection([(owner, 0.01)], {instance: [Reach.ANSWERED]}).install(
            monkeypatch
        )
        backend = _backend(owner, tmp_path)
        ran: list[str] = []

        async def dispatch(_context: Any) -> str:
            ran.append(backend.attachment.descriptor.instance_id)
            return "the result"

        result = await _call(
            backend, _beats(httpx2.ReadTimeout("paused"), _watched()), dispatch
        )

        assert result == "the result"
        assert ran == [instance]
        assert backend._unusable == {}
        assert backend.attachment.descriptor.instance_id == instance


class TestWhatAListingIsTold:
    """A listing sends no heartbeat, so only its recovery can hear silence."""

    @pytest.mark.parametrize(
        ("verdicts", "expected"),
        [([Reach.SILENT], _silent(4242)), ([Reach.SILENT, Reach.ANSWERED], _GONE)],
        ids=["silent", "answered again"],
    )
    async def test_the_listing_reads_what_its_recovery_heard_last(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verdicts: list[Reach],
        expected: str,
    ):
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        monkeypatch.setattr(election_module, "_REACHABLE_SECONDS", 0.1)
        owner = _published(tmp_path, pid=4242)
        _ClockedElection(
            [(owner, 0.01)], {owner.descriptor.instance_id: verdicts}
        ).install(monkeypatch)
        # Nothing serves the hop, so every listing fails as the owner's.
        reach_owners_in_process(monkeypatch, lambda _url: None)
        proxy = create_mcp_server(
            role=ServerRole.PROXY, proxy_backend=_backend(owner, tmp_path)
        )

        async with Client(proxy) as client:
            with pytest.raises(Exception) as failed:
                await client.list_tools()

        assert str(failed.value) == expected


class TestWhatTheHeartbeatsHeard:
    """A forwarded call the deadline cuts off, told by what its heartbeats heard.

    The owner is in process and holds the call; the hop and the forwarder are
    production's, and only the heartbeat route is scripted.
    """

    @pytest.mark.parametrize(
        ("after_dispatch", "owner_clause"),
        [
            ([_watched()], "stopped answering it"),
            ([httpx2.ReadTimeout("missed one"), _watched()], "stopped answering it"),
            ([httpx2.ReadTimeout("silent")], "did not respond (pid 4242)"),
        ],
        ids=["answered", "silent then answered", "silent"],
    )
    async def test_a_cut_off_call_names_silence_only_if_it_lasted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        after_dispatch: list[Any],
        owner_clause: str,
    ):
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole

        running = asyncio.Event()
        owner = FastMCP("owner")

        @owner.tool(name="send_connection_request")
        async def send() -> str:
            running.set()
            await asyncio.Event().wait()
            return "never"

        reach_owners_in_process(monkeypatch, lambda _url: owner)
        monkeypatch.setattr(proxy_module, "HEARTBEAT_SECONDS", 0.05)
        monkeypatch.setattr(proxy_module, "_TIMEOUT_MARGIN_SECONDS", 0.0)
        elections: list[object] = []
        monkeypatch.setattr(
            election_module, "obtain_owner", lambda *a, **k: elections.append(1)
        )
        monkeypatch.setattr(
            FrontendCallHeartbeatMiddleware,
            "_beat",
            staticmethod(_beats(_watched(), *after_dispatch)),
        )
        attached = _published(tmp_path, pid=4242)
        proxy = create_mcp_server(
            role=ServerRole.PROXY,
            proxy_backend=_backend(attached, tmp_path),
            tool_timeout=1.0,
        )

        async with Client(proxy) as client:
            result = await client.call_tool(
                "send_connection_request", {}, raise_on_error=False
            )

        assert running.is_set(), "the request never reached the owner"
        assert result.structured_content is not None
        assert result.structured_content["status"] == "outcome_unknown"
        assert result.structured_content["retry_safe"] is False
        assert result.structured_content["message"].startswith(
            f"send_connection_request was in flight when the shared browser "
            f"process {owner_clause} (it did not answer within this call's "
            "1-second limit)."
        )
        assert elections == [], "a call the deadline cut off looked for an owner"


class TestADirectServerAfterASilentOwner:
    """The busy text a Direct server gives when it fell back from a silent owner."""

    @pytest.fixture
    def lease(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from linkedin_mcp_server.profile_lease import get_profile_lease

        monkeypatch.setenv("BROWSER_WAIT", "0.5")
        lease = get_profile_lease(tmp_path / "profile")
        monkeypatch.setattr(
            "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
            lambda: lease,
        )
        return lease

    @staticmethod
    def _context() -> MagicMock:
        context = MagicMock()
        context.message.name = "close_session"
        context.fastmcp_context = None
        return context

    async def test_a_held_profile_names_the_owner_seen_silent_at_startup(
        self, tmp_path: Path, lease: Any
    ):
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        middleware = SequentialToolExecutionMiddleware(
            silent_owner=Unanswered("owner", 4242)
        )
        call_next = AsyncMock()
        holder = _hold_profile(tmp_path, 30)
        try:
            with pytest.raises(ToolError) as told:
                await middleware.on_call_tool(self._context(), call_next)
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert str(told.value) == (
            "When this server started, the shared browser process (pid 4242 at "
            "startup) did not respond in time. It may still be holding the "
            "browser, so restarting only your MCP client may not release it."
        )
        call_next.assert_not_awaited()

    async def test_once_this_server_had_the_profile_the_owner_is_not_named(
        self, tmp_path: Path, lease: Any
    ):
        from linkedin_mcp_server.sequential_tool_middleware import (
            SequentialToolExecutionMiddleware,
        )

        middleware = SequentialToolExecutionMiddleware(
            silent_owner=Unanswered("owner", 4242)
        )
        ran = AsyncMock(return_value="ran")
        assert await middleware.on_call_tool(self._context(), ran) == "ran"
        assert not lease.held

        holder = _hold_profile(tmp_path, 30)
        try:
            with pytest.raises(ToolError) as told:
                await middleware.on_call_tool(self._context(), AsyncMock())
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert "pid 4242" not in str(told.value)
        assert "currently using the browser" in str(told.value)


# --- A real owner, stopped -----------------------------------------------------------

#: Every budget a real-owner case shortens, passed explicitly. The first
#: election keeps its own, because it starts the whole server graph.
_HEARTBEAT_SECONDS = 1.0
_TOOL_TIMEOUT_SECONDS = 8.0
_ELECTION_SECONDS = 1.5
_SETTLEMENT_SECONDS = 0.5


async def _until(condition: Callable[[], bool], *, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def _is_stopped(pid: int) -> bool:
    import psutil

    return psutil.Process(pid).status() == psutil.STATUS_STOPPED


@contextlib.asynccontextmanager
async def _a_real_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Start a real detached owner on a temporary profile, and a frontend for it.

    The owner is production's ``daemon.owner``, started by a real election with
    its default budgets. The frontend in this process is the production proxy;
    its recovery elections are real too, with the short budgets above. The one
    tool called is ``close_session``, which with no browser open touches
    nothing. Yields what a test observes; always resumes and kills the owner.
    """
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon import owner as daemon_owner
    from linkedin_mcp_server.daemon.core.call import Dispatch
    from linkedin_mcp_server.daemon.forwarder import _call_being_made
    from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend
    from linkedin_mcp_server.profile_claim import ensure_profile_claim
    from linkedin_mcp_server.server import create_mcp_server
    from linkedin_mcp_server.server_role import ServerRole

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    ensure_profile_claim(profile, claim_anyway=True)
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    config.server.daemon_enabled = True
    config.browser.browser_wait_seconds = 1.0

    elect = election_module.obtain_owner
    outcome = await asyncio.to_thread(elect, profile.parent, profile, config)
    attachment = outcome.attachment_lookup.attachment
    pid = None if attachment is None else attachment.descriptor.pid
    try:
        assert outcome.worth_connecting and attachment is not None, outcome
        owner_pid = attachment.descriptor.pid
        elections: list[object] = []

        def a_short_election(*args: Any, **kwargs: Any) -> Any:
            elections.append(1)
            return elect(
                *args,
                deadline_seconds=_ELECTION_SECONDS,
                settlement_seconds=_SETTLEMENT_SECONDS,
                **kwargs,
            )

        monkeypatch.setattr(election_module, "obtain_owner", a_short_election)
        monkeypatch.setattr(proxy_module, "HEARTBEAT_SECONDS", _HEARTBEAT_SECONDS)
        monkeypatch.setattr(proxy_module, "_TIMEOUT_MARGIN_SECONDS", 0.0)

        observed = SimpleNamespace(
            pid=owner_pid,
            attachment=attachment,
            auth_root=profile.parent,
            config=config,
            elections=elections,
            dispatched=[],
            beats=[],
            heartbeat_timed_out=asyncio.Event(),
            stop_on_dispatch=False,
        )

        # Each tool request as it leaves, with whether its dispatch record
        # already said it may have left; the stop lands exactly there.
        direct = daemon_owner.direct_async_http_client

        async def note_a_tool_request(request: Any) -> None:
            if request.method != "POST" or b'"tools/call"' not in request.content:
                return
            bound = _call_being_made.get()
            observed.dispatched.append(
                bound is not None and bound.dispatch.state is Dispatch.POSSIBLY_SENT
            )
            if observed.stop_on_dispatch:
                observed.stop_on_dispatch = False
                os.kill(owner_pid, signal.SIGSTOP)
                await _until(lambda: _is_stopped(owner_pid), seconds=5, what="the stop")

        def watched_client(**kwargs: Any) -> Any:
            client = direct(**kwargs)
            client.event_hooks["request"].append(note_a_tool_request)
            return client

        monkeypatch.setattr(daemon_owner, "direct_async_http_client", watched_client)

        beat = FrontendCallHeartbeatMiddleware._beat

        async def a_watched_beat(attachment: Attachment, call_id: str) -> Any:
            began = time.monotonic()
            try:
                response = await beat(attachment, call_id)
            except Exception as exc:
                observed.beats.append((type(exc).__name__, time.monotonic() - began))
                if isinstance(exc, httpx2.ReadTimeout):
                    observed.heartbeat_timed_out.set()
                raise
            observed.beats.append(("answered", time.monotonic() - began))
            return response

        monkeypatch.setattr(
            FrontendCallHeartbeatMiddleware, "_beat", staticmethod(a_watched_beat)
        )
        observed.backend = DaemonProxyBackend(
            attachment=attachment,
            auth_root=profile.parent,
            profile=profile,
            config=config,
        )
        observed.frontend = create_mcp_server(
            role=ServerRole.PROXY,
            proxy_backend=observed.backend,
            tool_timeout=_TOOL_TIMEOUT_SECONDS,
        )
        yield observed
    finally:
        if pid is not None:
            # Resumed before the kill: a stopped process is never left behind.
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGCONT)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def _published_instance(auth_root: Path) -> str | None:
    from linkedin_mcp_server.daemon import descriptor as daemon_descriptor

    published = daemon_descriptor.read(auth_root)
    return None if published is None else published.instance_id


@_POSIX_ONLY
@pytest.mark.slow
class TestAStoppedRealOwner:
    """A real owner stopped with SIGSTOP: its port open, nobody reading it."""

    async def test_stopped_before_the_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with _a_real_owner(tmp_path, monkeypatch) as owner:
            instance = owner.attachment.descriptor.instance_id
            os.kill(owner.pid, signal.SIGSTOP)
            await _until(lambda: _is_stopped(owner.pid), seconds=5, what="the stop")

            async with Client(owner.frontend) as client:
                with pytest.raises(Exception) as listing:
                    await client.list_tools()
                call = await client.call_tool("close_session", {}, raise_on_error=False)
            elections_while_stopped = len(owner.elections)
            dispatched_while_stopped = list(owner.dispatched)

            os.kill(owner.pid, signal.SIGCONT)
            async with Client(owner.frontend) as client:
                again = await client.call_tool(
                    "close_session", {}, raise_on_error=False
                )

            assert str(listing.value) == _silent(owner.pid)
            assert call.is_error is True
            assert call.content[0].text == _silent_and_unsent(owner.pid)
            assert dispatched_while_stopped == []
            # One recovery for the listing and one for the call, nothing more.
            assert elections_while_stopped == 2
            assert any(
                kind == "ReadTimeout" and took < _HEARTBEAT_SECONDS + 0.5
                for kind, took in owner.beats
            ), owner.beats

            assert again.is_error is False, again
            assert owner.dispatched == [True]
            assert owner.backend.attachment.descriptor.pid == owner.pid
            assert _published_instance(owner.auth_root) == instance

    async def test_stopped_after_the_request_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        async with _a_real_owner(tmp_path, monkeypatch) as owner:
            instance = owner.attachment.descriptor.instance_id
            async with Client(owner.frontend) as client:
                owner.stop_on_dispatch = True
                calling = asyncio.create_task(
                    client.call_tool("close_session", {}, raise_on_error=False)
                )
                await asyncio.wait_for(
                    owner.heartbeat_timed_out.wait(), timeout=_TOOL_TIMEOUT_SECONDS
                )
                # Heard before the deadline, which is the only way the answer
                # below can say it.
                assert not calling.done(), calling
                result = await asyncio.wait_for(
                    calling, timeout=_TOOL_TIMEOUT_SECONDS + 10
                )

                os.kill(owner.pid, signal.SIGCONT)
                again = await client.call_tool(
                    "close_session", {}, raise_on_error=False
                )

            assert result.structured_content is not None
            assert result.structured_content["status"] == "outcome_unknown"
            assert result.structured_content["retry_safe"] is False
            assert result.structured_content["message"].startswith(
                "close_session was in flight when the shared browser process did "
                f"not respond (pid {owner.pid}) (it did not answer within this "
                "call's 8-second limit)."
            )
            timeouts = [took for kind, took in owner.beats if kind == "ReadTimeout"]
            assert timeouts and max(timeouts) < _HEARTBEAT_SECONDS + 0.5, owner.beats
            assert owner.elections == []

            assert again.is_error is False, again
            # The stopped call left once and was never repeated; the next is new.
            assert owner.dispatched == [True, True]
            assert owner.backend.attachment.descriptor.pid == owner.pid
            assert _published_instance(owner.auth_root) == instance

    async def test_a_new_frontend_falls_back_and_names_the_owner_when_busy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import linkedin_mcp_server.server as server_module
        from linkedin_mcp_server import cli_main
        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import reset_process_role_for_testing
        from linkedin_mcp_server.storage_class import Classification, StorageClass

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
        ):
            monkeypatch.setattr(server_module, name, nothing)
        # The temporary directory's file system is not what this is about.
        monkeypatch.setattr(
            "linkedin_mcp_server.storage_class.classify",
            lambda _path: Classification(StorageClass.LOCAL, "test storage"),
        )

        async with _a_real_owner(tmp_path, monkeypatch) as owner:
            set_config(owner.config)
            # The suite points this at its own profile for every test; this
            # frontend starts on the owner's, as `main` would.
            profile = Path(owner.config.browser.user_data_dir)
            monkeypatch.setattr(cli_main, "get_profile_dir", lambda: profile)
            monkeypatch.setattr(
                "linkedin_mcp_server.session_state.get_source_profile_dir",
                lambda: profile,
            )
            os.kill(owner.pid, signal.SIGSTOP)
            await _until(lambda: _is_stopped(owner.pid), seconds=5, what="the stop")

            shared = await asyncio.to_thread(
                cli_main._obtain_shared_owner, owner.config
            )
            assert isinstance(shared, Unanswered)
            assert shared == Unanswered(
                owner.attachment.descriptor.instance_id, owner.pid
            )

            # This process plays a second, new frontend; the fixture's proxy
            # claimed the role for the first one.
            reset_process_role_for_testing()
            direct = create_mcp_server(tool_timeout=10.0, silent_owner=shared)
            holder = _hold_profile(owner.auth_root, 60)
            try:
                async with Client(direct) as client:
                    busy = await client.call_tool(
                        "close_session", {}, raise_on_error=False
                    )
                    holder.kill()
                    holder.wait(timeout=10)
                    ran = await client.call_tool(
                        "close_session", {}, raise_on_error=False
                    )
            finally:
                holder.kill()
                holder.wait(timeout=10)

        assert busy.is_error is True
        assert busy.content[0].text == (
            "When this server started, the shared browser process "
            f"(pid {owner.pid} at startup) did not respond in time. It may still "
            "be holding the browser, so restarting only your MCP client may not "
            "release it."
        )
        assert ran.is_error is False, ran


# --- A blocked loop, on every platform ---------------------------------------------

#: A stand-in owner whose event loop can be blocked on command. Not a stop: the
#: process runs and its loop thread waits on an event with a bound, so nothing
#: on that loop answers until the bound passes. ``BLOCKED`` is said from the
#: loop's own thread just before it blocks, so the parent knows the block holds.
_BLOCKABLE_OWNER = """
import asyncio, json, socket, sys, threading

import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from linkedin_mcp_server.daemon.liveness import HEARTBEAT_PATH
from linkedin_mcp_server.server import _StaticTokenAuth

token, block_seconds = sys.argv[1], float(sys.argv[2])
mcp = FastMCP("owner", auth=_StaticTokenAuth(token))


def say(frame):
    sys.stdout.write(frame + "\\n")
    sys.stdout.flush()


@mcp.tool(annotations={"destructiveHint": True})
async def send_connection_request(message: str) -> dict[str, str]:
    say("EFFECT " + message)
    return {"status": "sent"}


@mcp.custom_route(HEARTBEAT_PATH, methods=["POST"])
async def heartbeat(request):
    if request.headers.get("authorization") != f"Bearer {token}":
        return JSONResponse({}, status_code=401)
    return JSONResponse({"watched": False})


def block():
    say("BLOCKED")
    threading.Event().wait(block_seconds)


async def main():
    loop = asyncio.get_running_loop()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    server = uvicorn.Server(
        uvicorn.Config(mcp.http_app(path="/mcp"), log_level="error", access_log=False)
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("the owner stopped before it started")
        await asyncio.sleep(0.01)

    def on_command():
        for line in sys.stdin:
            if line.strip() == "BLOCK":
                loop.call_soon_threadsafe(block)

    threading.Thread(target=on_command, daemon=True).start()
    say("READY " + json.dumps({"port": listener.getsockname()[1]}))
    await serving


asyncio.run(main())
"""


class _BlockableOwner:
    """A running stand-in owner, its frames, and a way to block its loop."""

    #: The bound on one block. Longer than any case needs; the case kills the
    #: process well before, and the bound keeps a missed kill from lasting.
    BLOCK_SECONDS = 60.0

    def __init__(self, root: Path, token: str) -> None:
        script = root / "owner.py"
        script.write_text(_BLOCKABLE_OWNER, encoding="utf-8")
        self._frames: queue.Queue[str | None] = queue.Queue()
        self._log: list[str] = []
        self.process = subprocess.Popen(
            [sys.executable, str(script), token, str(self.BLOCK_SECONDS)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        for line in stream:
            self._log.append(line)
            if line.endswith("\n"):
                self._frames.put(line[:-1])
        self._frames.put(None)

    def wait_for(self, kind: str, *, seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no {kind} from the owner: {''.join(self._log)}"
            try:
                frame = self._frames.get(timeout=remaining)
            except queue.Empty:
                continue
            assert frame is not None, f"the owner exited: {''.join(self._log)}"
            if frame == kind or frame.startswith(kind + " "):
                return frame[len(kind) + 1 :]

    def block(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write("BLOCK\n")
        stdin.flush()
        self.wait_for("BLOCKED", seconds=15)

    def effects(self) -> list[str]:
        return [line for line in self._log if line.startswith("EFFECT ")]

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self._reader.join(timeout=10)


@contextlib.contextmanager
def _a_blockable_owner(root: Path, token: str) -> Iterator[_BlockableOwner]:
    owner = _BlockableOwner(root, token)
    try:
        yield owner
    finally:
        owner.stop()


class TestABlockedOwnerLoop:
    """A stand-in owner whose event loop is blocked: the portable form of silence.

    Runs on Windows CI, where no process can be stopped. A blocked loop is not
    a stopped process: the kernel still accepts the connection and nothing on
    the loop reads it, which is the same silence to every request.

    Real here: the process, the socket, the published descriptor, the proxy, its
    preflight, its recovery and the election's probe. Stood in: the owner's
    server (no admission, lease or browser) and the child start, which has no
    lock to contend for.
    """

    async def test_a_blocked_owner_is_named_and_sent_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.config.schema import AppConfig
        from linkedin_mcp_server.daemon.descriptor import (
            build,
            commit_prepared,
            new_instance_id,
            new_token,
            prepare,
        )
        from linkedin_mcp_server.daemon.proxy import DaemonProxyBackend
        from linkedin_mcp_server.server import create_mcp_server
        from linkedin_mcp_server.server_role import ServerRole
        from linkedin_mcp_server.session_state import get_runtime_id

        profile = tmp_path / "auth" / "profile"
        profile.mkdir(parents=True)
        config = AppConfig()
        config.browser.user_data_dir = str(profile)
        config.server.daemon_enabled = True
        token = new_token()

        monkeypatch.setattr(proxy_module, "HEARTBEAT_SECONDS", _HEARTBEAT_SECONDS)
        monkeypatch.setattr(proxy_module, "_TIMEOUT_MARGIN_SECONDS", 0.0)
        monkeypatch.setattr(
            spawn_module, "_start_owner", lambda *a, **k: Attempt.FAILED
        )
        elect = election_module.obtain_owner
        monkeypatch.setattr(
            election_module,
            "obtain_owner",
            lambda *args, **kwargs: elect(
                *args,
                deadline_seconds=_ELECTION_SECONDS,
                settlement_seconds=_SETTLEMENT_SECONDS,
                **kwargs,
            ),
        )
        beat = FrontendCallHeartbeatMiddleware._beat
        beats: list[tuple[str, float]] = []

        async def a_watched_beat(attachment: Attachment, call_id: str) -> Any:
            began = time.monotonic()
            try:
                response = await beat(attachment, call_id)
            except Exception as exc:
                beats.append((type(exc).__name__, time.monotonic() - began))
                raise
            beats.append(("answered", time.monotonic() - began))
            return response

        monkeypatch.setattr(
            FrontendCallHeartbeatMiddleware, "_beat", staticmethod(a_watched_beat)
        )

        with _a_blockable_owner(tmp_path, token) as owner:
            port = json.loads(owner.wait_for("READY", seconds=30))["port"]
            descriptor = dataclasses.replace(
                build(
                    instance_id=new_instance_id(),
                    package_version=__version__,
                    runtime_id=get_runtime_id(),
                    profile=profile,
                    host="127.0.0.1",
                    port=port,
                    path="/mcp",
                    token=token,
                    config=config,
                    log_path=tmp_path / "owner.log",
                ),
                pid=owner.process.pid,
            )
            prepare(profile.parent, descriptor, token)
            commit_prepared(profile.parent, descriptor.instance_id)
            proxy = create_mcp_server(
                role=ServerRole.PROXY,
                proxy_backend=DaemonProxyBackend(
                    attachment=Attachment(descriptor=descriptor, token=token),
                    auth_root=profile.parent,
                    profile=profile,
                    config=config,
                ),
                tool_timeout=_TOOL_TIMEOUT_SECONDS,
            )

            owner.block()
            async with Client(proxy) as client:
                result = await client.call_tool(
                    "send_connection_request",
                    {"message": "hello"},
                    raise_on_error=False,
                )
            effects = owner.effects()

        assert result.is_error is True
        assert result.content[0].text == _silent_and_unsent(owner.process.pid)
        assert effects == []
        assert beats and beats[0][0] == "ReadTimeout", beats
        assert beats[0][1] < _HEARTBEAT_SECONDS + 0.5, beats
