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

import socket
import threading
import time
from typing import Any

import pytest

from linkedin_mcp_server import process_control
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


class TestAuthorization:
    def test_the_record_reaches_the_child_that_presented_the_nonce(
        self, listener: ControlListener
    ):
        child = _attach(listener)
        try:
            listener.accept_within(nonce=_NONCE, timeout=5.0)
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
            listener.accept_within(nonce=_NONCE, timeout=5.0)
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
            listener.accept_within(nonce=_NONCE, timeout=30.0)
            elapsed = time.monotonic() - began
            listener.send(_RECORD)

            assert child.readline() == _RECORD
            assert elapsed < 10.0, "one silent peer held the rendezvous"
        finally:
            silent.close()
            child.close()

    def test_a_rendezvous_nothing_attaches_to_is_bounded(
        self, listener: ControlListener
    ):
        began = time.monotonic()

        with pytest.raises(TimeoutError, match="control channel"):
            listener.accept_within(nonce=_NONCE, timeout=0.2)

        assert time.monotonic() - began < 5.0

    def test_sending_before_a_child_is_attached_is_refused(
        self, listener: ControlListener
    ):
        with pytest.raises(OSError, match="No daemon is attached"):
            listener.send(_RECORD)


class TestAbortAuthority:
    def test_closing_without_a_record_reads_as_end_of_file(
        self, listener: ControlListener
    ):
        # The property the configuration pipe used to carry. A parent that goes
        # away before it validates has authorized nothing, and the child has to
        # be able to tell that from a commit.
        child = _attach(listener)
        try:
            listener.accept_within(nonce=_NONCE, timeout=5.0)
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
            listener.accept_within(nonce=_NONCE, timeout=5.0)
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
            channel.accept_within(nonce=_NONCE, timeout=0.1)

    def test_closing_twice_is_not_an_error(self, listener: ControlListener):
        listener.close()
        listener.close()

    def test_the_child_waits_for_a_record_that_arrives_late(
        self, listener: ControlListener
    ):
        # The commit record follows a whole endpoint startup, so the child's read
        # must not carry the connect timeout into it.
        child = _attach(listener)
        listener.accept_within(nonce=_NONCE, timeout=5.0)
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
