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
import socket
import threading
import time
from typing import Any

import pytest

from linkedin_mcp_server import daemon_election, process_control
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
            # And with room to spare rather than by a hair, so a loaded machine
            # reaches the same verdict.
            assert elapsed < daemon_election._PREPARED_READ_SECONDS / 2, (
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
        try:
            listener.start_accepting(nonce=_NONCE, timeout=2.0)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if all(peer.recv(1) == b"" for peer in silent):
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
