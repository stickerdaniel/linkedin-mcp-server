"""The channel that carries an owner's commit authorization.

It exists because the configuration pipe cannot carry both. Every owner older
than the commit boundary frames its configuration by reading standard input to
end of file, so a parent that holds that pipe open for a later commit record
deadlocks against its own child after a package rollback. The record moves here
and the pipe ends on its content.

Two properties matter and neither is about throughput. Only the process that was
handed the post-spawn nonce may be authorized, because anything running as this
account can reach a loopback port. And closing without a record has to read as an
abort, because that is the authority the pipe used to carry: a parent that dies
before it validates must not leave a child that publishes anyway.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server import daemon_election, process_control
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.process_control import ControlListener

_NONCE = "0123456789abcdef" * 4
_OTHER_NONCE = "fedcba9876543210" * 4
_RECORD = f"owner {_NONCE} commit\n"


@pytest.fixture
def listener():
    channel = ControlListener.open()
    try:
        yield channel
    finally:
        channel.close()


def _attach(channel: ControlListener, nonce: str = _NONCE) -> Any:
    return process_control.attach(channel.host, channel.port, nonce=nonce, timeout=5.0)


def _collect(channel: ControlListener, *, timeout: float, nonce: str = _NONCE) -> None:
    """Drain and then take the attachment, the way ``_spawn`` does.

    Two calls rather than one, because the whole point of the split is that they
    happen at different times: the drain starts as soon as the nonce exists and
    the collection waits on the result much later. Tests that only care about
    what is authorized use both at once; the ones about the queue place the drain
    themselves.
    """
    channel.start_accepting(nonce=nonce, timeout=timeout)
    channel.attached_within(timeout=timeout)


class TestAuthorization:
    def test_the_record_reaches_the_child_that_presented_the_nonce(
        self, listener: ControlListener
    ):
        child = _attach(listener)
        try:
            _collect(listener, timeout=5.0)
            listener.send(_RECORD)

            assert child.readline() == _RECORD
        finally:
            child.close()

    def test_a_peer_without_the_nonce_never_becomes_the_child(
        self, listener: ControlListener
    ):
        # The port is reachable by anything running as this account, so a
        # stranger arriving first must not be able to take the authorization or
        # to spend the rendezvous the real child is queued in.
        stranger = _attach(listener, _OTHER_NONCE)
        child = _attach(listener)
        try:
            _collect(listener, timeout=5.0)
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            # And the stranger got nothing at all, not even a truncated record.
            assert stranger.readline() == ""
        finally:
            stranger.close()
            child.close()

    def test_a_silent_peer_does_not_spend_the_whole_rendezvous(
        self, listener: ControlListener
    ):
        # Connecting and then saying nothing is the cheapest way to occupy this,
        # and it is bounded per peer rather than only by the whole wait.
        silent = socket.create_connection((listener.host, listener.port))
        child = _attach(listener)
        try:
            began = time.monotonic()
            _collect(listener, timeout=30.0)
            elapsed = time.monotonic() - began
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            assert elapsed < 10.0, "one silent peer held the rendezvous"
        finally:
            silent.close()
            child.close()

    def test_a_silent_peer_leaves_the_production_wait_enough_to_attach(
        self, listener: ControlListener
    ):
        # The wait the election actually gives this is one second, and the
        # per-peer bound used to be one second as well, so the first silent peer
        # spent the whole of it and the owner queued behind it was killed for
        # never attaching. The owner's frame is already in the receive buffer
        # here, exactly as it is in production: it is sent on connect, long
        # before the prepared generation that starts this wait.
        silent = socket.create_connection((listener.host, listener.port))
        child = _attach(listener)
        try:
            began = time.monotonic()
            _collect(listener, timeout=daemon_election._PREPARED_READ_SECONDS)
            elapsed = time.monotonic() - began
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            assert elapsed < daemon_election._PREPARED_READ_SECONDS, (
                "the silent peer spent the whole production wait"
            )
        finally:
            silent.close()
            child.close()

    def test_a_full_queue_of_silent_peers_leaves_the_owner_its_turn(
        self, listener: ControlListener
    ):
        # The whole queue against the whole production wait, which is the worst
        # case that can be standing there when the wait begins: the listener
        # holds _BACKLOG connections, so the most silent peers that can sit ahead
        # of the owner is one fewer, the owner itself filling the last slot.
        # Measured, and the reason this is not _BACKLOG silent peers plus a
        # child: a seventeenth connect is not refused but dropped, and the client
        # then waits a full second on a SYN retransmit for a slot that nothing is
        # draining. A share-of-what-is-left bound is what survives this; every
        # fixed per-peer span, floors included, is emptied by enough repetitions.
        silent = [
            socket.create_connection((listener.host, listener.port))
            for _ in range(process_control._BACKLOG - 1)
        ]
        child = _attach(listener)
        try:
            began = time.monotonic()
            _collect(listener, timeout=daemon_election._PREPARED_READ_SECONDS)
            elapsed = time.monotonic() - began
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            # Keep a quarter of the production wait for the owner. The peer
            # allowances consume about three eighths; requiring the complete
            # drain below one half left only one eighth for thread scheduling
            # and socket cleanup, and a loaded runner spent 0.535s on a correct
            # handoff. Three quarters still fails any peer that can spend the
            # whole wait while leaving realistic scheduler margin.
            assert elapsed < daemon_election._PREPARED_READ_SECONDS * 3 / 4, (
                "a full queue of silent peers spent the production wait"
            )
        finally:
            for peer in silent:
                peer.close()
            child.close()

    def test_a_rendezvous_nothing_attaches_to_is_bounded(
        self, listener: ControlListener
    ):
        began = time.monotonic()

        listener.start_accepting(nonce=_NONCE, timeout=0.2)
        with pytest.raises(TimeoutError, match="control channel"):
            listener.attached_within(timeout=0.2)

        assert time.monotonic() - began < 5.0

    def test_sending_before_a_child_is_attached_is_refused(
        self, listener: ControlListener
    ):
        with pytest.raises(OSError, match="No daemon is attached"):
            listener.send(_RECORD)


class TestWireContracts:
    """What a peer of another version has to keep producing and accepting.

    A frontend and the owner it starts can come from different installations,
    so these two shapes are read by code that was compiled from other source.
    Asserting them through their own constants would move with any edit.
    """

    def test_the_attachment_frame_is_the_literal_one(self):
        nonce = "0" * 64

        assert process_control._attach_frame(nonce) == b"attach " + b"0" * 64 + b"\n"

    def test_a_frontend_of_another_version_is_refused_by_its_nonce_alone(
        self, listener: ControlListener
    ):
        # The frame shape is shared; the nonce inside it is what separates one
        # generation from the next, and it is compared whole.
        listener.start_accepting(nonce=_NONCE, timeout=2.0)
        stranger = _attach(listener, nonce=_OTHER_NONCE)
        try:
            with pytest.raises(TimeoutError):
                listener.attached_within(timeout=1.0)
        finally:
            stranger.close()


class TestAddressFamilyFallback:
    """Which bind failures mean the host has no IPv6, and which mean take.

    A host without IPv6 is ordinary and has to fall back; a port that is taken
    or a permission that is refused has to be reported. Both directions are
    asserted, or narrowing the set leaves an IPv6-less host unable to start
    while the suite stays green.
    """

    @pytest.mark.parametrize(
        "code",
        [errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT],
    )
    def test_a_host_without_ipv6_falls_back(
        self, code: int, monkeypatch: pytest.MonkeyPatch
    ):
        real_socket = socket.socket

        def refusing(family, *rest):
            if family is socket.AF_INET6:
                raise OSError(code, os.strerror(code))
            return real_socket(family, *rest)

        monkeypatch.setattr(socket, "socket", refusing)

        channel = ControlListener.open()
        try:
            assert channel.host == "127.0.0.1"
        finally:
            channel.close()

    def test_an_unrelated_bind_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def refusing(*_args):
            raise OSError(errno.EACCES, "permission denied")

        monkeypatch.setattr(socket, "socket", refusing)

        with pytest.raises(OSError) as failure:
            ControlListener.open()

        assert failure.value.errno == errno.EACCES


class TestPeerAllowance:
    """What one unauthenticated peer is given, out of the wait that is left."""

    def test_a_peer_never_gets_the_whole_remaining_wait(self):
        # However little is left, including the short remainders a floor used to
        # hand over whole. This is the property the guarantee rests on.
        for remaining in (0.001, 0.01, 0.06, 0.2, 1.0, 5.0, 30.0):
            assert process_control._peer_allowance(remaining) < remaining

    def test_a_wait_already_spent_gives_nothing(self):
        assert process_control._peer_allowance(0.0) == 0.0
        assert process_control._peer_allowance(-1.0) == 0.0

    def test_a_full_queue_of_silent_peers_cannot_spend_the_production_wait(self):
        # The arithmetic behind the socket test, stated where it can be read: a
        # peer for every slot in the backlog, each spending its whole allowance,
        # against the wait the election actually gives. More than half of it is
        # still there afterwards, and the owner needs a scheduled read.
        remaining = daemon_election._PREPARED_READ_SECONDS
        for _ in range(process_control._BACKLOG):
            remaining -= process_control._peer_allowance(remaining)

        assert remaining > daemon_election._PREPARED_READ_SECONDS / 2

    def test_no_number_of_silent_peers_closes_the_wait(self):
        # Peers keep arriving as the queue drains, so the guarantee cannot stop
        # at one backlog. The share leaves a remainder at every depth.
        remaining = daemon_election._PREPARED_READ_SECONDS
        for _ in range(10 * process_control._BACKLOG):
            remaining -= process_control._peer_allowance(remaining)

        assert remaining > 0.0

    def test_a_full_queue_is_emptied_inside_the_production_attachment_wait(self):
        # The drain runs for the whole spawn, but a peer's allowance comes out of
        # the window the parent will actually wait for an attachment in, and not
        # out of the drain's own budget. Keyed to the drain instead, each peer
        # grows with it: measured against an eight second spawn, a full queue of
        # silent peers took three seconds against a one second wait.
        assert (
            process_control._PEER_WINDOW_SECONDS
            <= daemon_election._PREPARED_READ_SECONDS
        )
        remaining = process_control._PEER_WINDOW_SECONDS
        for _ in range(process_control._BACKLOG):
            remaining -= process_control._peer_allowance(remaining)

        spent = process_control._PEER_WINDOW_SECONDS - remaining
        assert spent < daemon_election._PREPARED_READ_SECONDS / 2

    def test_a_long_drain_does_not_make_each_peer_proportionally_longer(
        self, listener: ControlListener
    ):
        # The same property through the socket. The drain is given thirty
        # seconds, which is the shape of a real spawn, and the peers ahead of the
        # child still cost a share of the attachment window rather than of that.
        silent = [
            socket.create_connection((listener.host, listener.port)) for _ in range(3)
        ]
        child = _attach(listener)
        try:
            listener.start_accepting(nonce=_NONCE, timeout=30.0)
            began = time.monotonic()
            listener.attached_within(timeout=daemon_election._PREPARED_READ_SECONDS)
            elapsed = time.monotonic() - began
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            assert elapsed < daemon_election._PREPARED_READ_SECONDS / 2, (
                "the peers ahead were paid out of the drain's own budget"
            )
        finally:
            for peer in silent:
                peer.close()
            child.close()

    def test_a_long_wait_is_still_capped_absolutely(self):
        # The ceiling binds only past thirty-two seconds; no caller has a wait
        # like that today, and the cap is what keeps one from handing a stranger
        # a share proportional to it.
        assert process_control._peer_allowance(60.0) == process_control._PEER_SECONDS


class TestSaturatedQueue:
    """A queue already full when the child goes looking for a slot.

    The case the per-peer allowance does not reach. That bound divides the wait
    among peers *ahead of a connection that is already queued*, and it therefore
    assumes the child got into the queue. Nothing guarantees that: the rendezvous
    is bound before the child exists, anything running as this account can reach
    it, and the queue holds sixteen. A child that finds it full does not fail, it
    blocks in connect for its own thirty second timeout, so a parent that starts
    accepting only after the child has reported something is waiting for a report
    from a process it has stopped from reaching it.
    """

    def _saturate(self, channel: ControlListener) -> list[socket.socket]:
        return [
            socket.create_connection((channel.host, channel.port))
            for _ in range(process_control._BACKLOG)
        ]

    def test_a_queue_full_before_the_child_exists_still_lets_it_attach(
        self, listener: ControlListener
    ):
        # Production order: every slot taken while the parent is still spawning,
        # and the child arriving afterwards. It attaches because the drain has
        # been emptying the queue since the nonce existed; without that it waits
        # out its connect timeout and never reports the prepared generation the
        # parent is waiting for before it would accept.
        silent = self._saturate(listener)
        try:
            listener.start_accepting(nonce=_NONCE, timeout=2.0)
            began = time.monotonic()
            child = _attach(listener)
            try:
                listener.attached_within(timeout=5.0)
                listener.send(_RECORD)

                assert child.readline() == _RECORD
                assert time.monotonic() - began < 5.0
            finally:
                child.close()
        finally:
            for peer in silent:
                peer.close()

    def test_a_full_queue_of_strangers_is_emptied_rather_than_waited_out(
        self, listener: ControlListener
    ):
        # The drain is what makes room, so it has to discard rather than park on
        # the first stranger. Every slot holds a peer that says nothing at all,
        # and the queue is empty again well inside the drain's own budget.
        silent = self._saturate(listener)

        def discarded(peer: socket.socket) -> bool:
            # Bounded, or a drain that parks on the first stranger leaves this
            # read waiting past every deadline the loop below could check, and
            # the regression arrives as a hung run rather than a failure.
            try:
                return peer.recv(1) == b""
            except TimeoutError:
                return False

        try:
            for peer in silent:
                peer.settimeout(0.05)
            listener.start_accepting(nonce=_NONCE, timeout=2.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if all(discarded(peer) for peer in silent):
                    break
                time.sleep(0.01)
            else:  # pragma: no cover - the drain kept a stranger
                pytest.fail("the strangers were never discarded")
        finally:
            for peer in silent:
                peer.close()


class TestWorkerLifetime:
    """What the accept worker may still do once the channel is closed.

    It runs on its own thread and the parent walks away from it, so the two
    questions are whether it can touch state the parent has finished with, and
    whether it can touch a descriptor the parent has reissued.
    """

    def test_a_worker_that_never_starts_leaves_a_closable_channel(
        self, listener: ControlListener, monkeypatch: pytest.MonkeyPatch
    ):
        """A published worker that does not exist is one close cannot join.

        The listener has already left this object by then, so the failure would
        also take the socket with it and replace the caller's own error with a
        second one raised while cleaning up after it.
        """

        def refuse(_self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)

        with pytest.raises(RuntimeError, match="can't start new thread"):
            listener.start_accepting(nonce=_NONCE, timeout=5.0)

        monkeypatch.undo()
        assert listener._drain is None, "nothing was published for close to join"

        listener.close()

        with pytest.raises(OSError):
            socket.create_connection((listener.host, listener.port), timeout=1.0)

    def test_a_worker_never_publishes_into_a_closed_channel(
        self, listener: ControlListener
    ):
        # The guard itself, called the way the worker calls it: an authenticated
        # connection arriving after close is closed rather than published.
        # Publishing it would hand a record-carrying socket to a channel whose
        # abort has already been decided.
        listener.start_accepting(nonce=_NONCE, timeout=5.0)
        listener.close()
        first, second = socket.socketpair()
        try:
            listener._keep(first)

            assert listener._connection is None
            with pytest.raises(OSError, match="No daemon is attached"):
                listener.send(_RECORD)
            assert second.recv(16) == b"", "the refused peer was left open"
        finally:
            first.close()
            second.close()

    def test_a_peer_that_authenticates_late_gets_the_abort(
        self, listener: ControlListener
    ):
        # The same thing through the socket rather than the method. Whether the
        # worker was between accepts or already reading this peer, the peer ends
        # up with end of file and the channel with nothing attached.
        peer = socket.create_connection((listener.host, listener.port))
        try:
            listener.start_accepting(nonce=_NONCE, timeout=5.0)
            listener.close()
            with contextlib.suppress(OSError):
                peer.sendall(f"attach {_NONCE}\n".encode("ascii"))
            peer.settimeout(5.0)

            assert peer.recv(16) == b""
            assert listener._connection is None
        finally:
            peer.close()

    def test_the_worker_and_its_socket_are_gone_after_close(
        self, listener: ControlListener
    ):
        # The listener is closed by the worker and never under it: a descriptor
        # closed while a thread is blocked in accept can be reissued to an
        # unrelated socket in this process, and the accept would then return a
        # connection belonging to that one.
        listener.start_accepting(nonce=_NONCE, timeout=30.0)
        worker = listener._drain
        assert worker is not None
        assert listener._listener is None, (
            "the channel kept a socket close would race the worker for"
        )
        host, port = listener.host, listener.port

        listener.close()

        deadline = time.monotonic() + 5.0
        while worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not worker.is_alive(), "the accept worker outlived the closed channel"
        with pytest.raises(OSError):
            socket.create_connection((host, port), timeout=5.0).close()

    def test_a_second_drain_is_never_started(self, listener: ControlListener):
        # Idempotent, so a caller that cannot tell whether it already started the
        # drain does not leave a second thread accepting on a socket the first
        # one owns.
        listener.start_accepting(nonce=_NONCE, timeout=5.0)
        worker = listener._drain
        listener.start_accepting(nonce=_NONCE, timeout=5.0)

        assert listener._drain is worker

    def test_collecting_before_the_drain_exists_is_refused(
        self, listener: ControlListener
    ):
        # An attachment can only be required of a channel that was told to look
        # for one. Waiting on a channel that never started would report a silent
        # child where the parent simply never asked.
        with pytest.raises(OSError, match="not accepting"):
            listener.attached_within(timeout=0.1)


class TestAbortAuthority:
    def test_closing_without_a_record_reads_as_end_of_file(
        self, listener: ControlListener
    ):
        # The property the configuration pipe used to carry. A parent that goes
        # away before it validates has authorized nothing, and the child has to
        # be able to tell that from a commit.
        child = _attach(listener)
        try:
            _collect(listener, timeout=5.0)
            listener.close()

            assert child.readline() == ""
        finally:
            child.close()

    def test_a_parent_that_never_accepts_still_ends_the_wait(
        self, listener: ControlListener
    ):
        # The connection is queued rather than accepted, which is where a child
        # sits for the whole of its endpoint startup. Closing the listener has to
        # reach it there too.
        child = _attach(listener)
        try:
            listener.close()

            assert child.readline() == ""
        finally:
            child.close()

    def test_a_reset_reads_as_end_of_file_rather_than_an_error(
        self, listener: ControlListener
    ):
        # A parent killed mid-startup resets rather than closing cleanly. Both
        # mean the same thing: nothing was authorized.
        child = _attach(listener)
        try:
            _collect(listener, timeout=5.0)
            connection = listener._connection
            assert connection is not None
            connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
            )
            connection.close()
            listener._connection = None

            assert child.readline() == ""
        finally:
            child.close()


class TestRendezvous:
    def test_the_address_is_loopback_and_ephemeral(self, listener: ControlListener):
        assert listener.host in ("::1", "127.0.0.1")
        assert listener.port > 0

    def test_two_rendezvous_never_share_a_port(self):
        first = ControlListener.open()
        second = ControlListener.open()
        try:
            assert first.port != second.port
        finally:
            first.close()
            second.close()

    def test_attaching_to_a_closed_rendezvous_fails_rather_than_hangs(self):
        channel = ControlListener.open()
        host, port = channel.host, channel.port
        channel.close()

        began = time.monotonic()
        with pytest.raises(OSError):
            process_control.attach(host, port, nonce=_NONCE, timeout=5.0)
        assert time.monotonic() - began < 5.0

    def test_accepting_on_a_closed_rendezvous_is_refused(self):
        channel = ControlListener.open()
        channel.close()

        with pytest.raises(OSError, match="closed"):
            channel.start_accepting(nonce=_NONCE, timeout=0.1)

    def test_closing_twice_is_not_an_error(self, listener: ControlListener):
        listener.close()
        listener.close()

    def test_the_child_waits_for_a_record_that_arrives_late(
        self, listener: ControlListener
    ):
        # The commit record follows a whole endpoint startup, so the child's read
        # must not carry the connect timeout into it.
        child = _attach(listener)
        _collect(listener, timeout=5.0)
        received: list[str] = []
        reader = threading.Thread(target=lambda: received.append(child.readline()))
        reader.start()
        try:
            time.sleep(0.3)
            assert received == [], "the child stopped waiting for its record"
            listener.send(_RECORD)
            reader.join(timeout=5.0)

            assert received == [_RECORD]
        finally:
            reader.join(timeout=5.0)
            child.close()


def _park_a_reader(child: Any) -> tuple[threading.Thread, list[str]]:
    """Put a thread inside ``readline`` and wait until it is really in the recv.

    The sleep is the point rather than slack. A thread that has merely been
    scheduled has not taken the buffer lock yet, and it is holding that lock
    across a blocking ``recv`` that makes closing the wrapper deadlock.
    """
    entered = threading.Event()
    received: list[str] = []

    def read() -> None:
        entered.set()
        received.append(child.readline())

    reader = threading.Thread(target=read, name="parked-control-read", daemon=True)
    reader.start()
    assert entered.wait(5.0)
    time.sleep(0.2)
    return reader, received


class TestCloseUnderABlockedRead:
    """Closing has to work while the read it is aborting is still in flight.

    This is the ordinary shape of an abort, not a corner: the parent's
    authorization deadline expires *while* the child sits in the read waiting
    for a record that is never coming, and the child then tears the channel
    down from a different thread than the one parked in it.
    """

    def test_closing_while_a_read_is_parked_returns_promptly(
        self, listener: ControlListener
    ):
        # The peer stays open and sends nothing, so the parked ``recv`` has no
        # reason of its own to return. Closing the wrapper first takes the lock
        # that read holds and waits for it forever; the socket has to be made to
        # end before the wrapper is closed.
        child = _attach(listener)
        _collect(listener, timeout=5.0)
        reader, received = _park_a_reader(child)

        closed = threading.Event()
        threading.Thread(
            target=lambda: (child.close(), closed.set()), daemon=True
        ).start()

        assert closed.wait(5.0), "close did not return while a read was parked"
        reader.join(timeout=5.0)
        assert not reader.is_alive(), "the parked read was never released"
        # The abort semantics are unchanged: an interrupted read is end of file,
        # exactly as a parent that closed without committing produces.
        assert received == [""]

    def test_closing_twice_and_from_two_threads_at_once_is_safe(
        self, listener: ControlListener
    ):
        # A close that has to interrupt a socket first has more to get wrong when
        # it runs twice, and both callers exist: the owner's ``finally`` closes
        # the channel it opened, and a failure path above it may already have.
        child = _attach(listener)
        _collect(listener, timeout=5.0)
        reader, received = _park_a_reader(child)

        errors: list[BaseException] = []

        def close() -> None:
            try:
                child.close()
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                errors.append(exc)

        closers = [
            threading.Thread(target=close, name=f"closer-{index}", daemon=True)
            for index in range(4)
        ]
        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(timeout=5.0)

        assert not [c for c in closers if c.is_alive()], "a concurrent close hung"
        assert errors == []
        child.close()
        reader.join(timeout=5.0)
        assert received == [""]
        # A read after the close is still an abort rather than an exception.
        assert child.readline() == ""


@pytest.mark.skipif(
    os.name == "nt",
    reason="the owner entry point takes a Job Object handoff on Windows",
)
class TestOwnerFinalization:
    """What the owner's ``finally`` owes the next election.

    ``daemon_owner.main`` closes its control channel before it releases the
    daemon lock. That order is deliberate, and it means a close that does not
    return is a lock that is never released: the process has already decided it
    is not going to serve, and every later election contends with a corpse that
    still holds the position.
    """

    def test_an_unauthorized_owner_frees_the_lock_it_holds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        listener: ControlListener,
    ):
        from linkedin_mcp_server import daemon_config, daemon_lock, daemon_owner

        profile = tmp_path / "profile"
        profile.mkdir()
        config = AppConfig()
        config.browser.user_data_dir = str(profile)

        class SilentBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                pass

            def close(self) -> None:
                pass

        class SilentHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def retry(self) -> None:
                pass

            def abort(self) -> None:
                pass

            def fail(self) -> None:
                pass

            def close(self) -> None:
                pass

        # The real rendezvous, so the owner attaches over a real socket and this
        # test is the parent that stays open and never commits.
        listener.start_accepting(nonce=_NONCE, timeout=10.0)
        handover = daemon_config.OwnerHandover(
            config,
            _NONCE,
            control=daemon_config.ControlEndpoint(listener.host, listener.port),
        )

        held: list[daemon_lock.DaemonLock] = []

        def take_lock(auth_root: Path, _fd: object) -> daemon_lock.DaemonLock:
            lock = daemon_lock.DaemonLock(auth_root)
            assert lock.try_acquire()
            held.append(lock)
            return lock

        authorization_timed_out = threading.Event()

        async def stalled_serve(**kwargs: Any) -> int:
            # Production authorization, production read. The record never comes,
            # so ``_read_control_until`` gives up with its reader thread still
            # inside ``readline`` on the socket, which is the state the owner's
            # own finalization then has to close through.
            channel = kwargs["control"]
            with pytest.raises(TimeoutError):
                await daemon_owner._read_control_until(channel, time.monotonic() + 0.3)
            authorization_timed_out.set()
            return 1

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", SilentBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", SilentHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_handshake_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_read_handover", lambda: handover)
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda _profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner, "_attach_daemon_log", lambda _root: tmp_path / "daemon.log"
        )
        monkeypatch.setattr(daemon_owner, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(daemon_owner, "_take_lock", take_lock)
        monkeypatch.setattr(daemon_owner, "_serve", stalled_serve)

        status: list[int] = []
        runner = threading.Thread(
            target=lambda: status.append(daemon_owner.main([])),
            name="owner-main",
            daemon=True,
        )
        runner.start()
        runner.join(timeout=20.0)

        # Bounded on purpose. Reverting the close order does not make this fail
        # an assertion about ordering, it stops `main` from ever returning, and a
        # test without a bound would hang the suite instead of reporting it.
        assert not runner.is_alive(), (
            "the owner never finished finalizing, so it still holds the lock"
        )
        assert authorization_timed_out.is_set()
        assert status == [1]
        assert held, "the test never took the lock it is asserting about"

        acquired: list[bool] = []
        contender = daemon_lock.DaemonLock(tmp_path)
        successor = threading.Thread(
            target=lambda: acquired.append(contender.try_acquire()), daemon=True
        )
        successor.start()
        successor.join(timeout=10.0)
        try:
            assert acquired == [True], (
                "the position is still held by an owner that gave up"
            )
        finally:
            contender.release()
