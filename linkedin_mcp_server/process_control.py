"""The parent-owned channel that authorizes one owner's publication.

The configuration pipe cannot also carry the commit record, and the reason is a
rollback rather than an upgrade. ``@latest`` is the documented install, so a
long-running frontend can start an owner from a package that was replaced on
disk while it ran, and every owner older than the commit boundary frames its
configuration by reading standard input to end of file. A parent that holds that
pipe open for its own commit record then waits for a verdict from a child that is
waiting for the parent to close. Neither side is wrong and neither side moves:
the frontend spends its whole election budget, kills the child, and starts an
identical one on the next pass.

So the configuration pipe is closed as soon as the record is written, which is
the framing every predecessor already expects, and authorization moves here.

A loopback rendezvous rather than an inherited descriptor, because an inherited
one cannot reach the child on both platforms. ``pass_fds`` is POSIX only, and the
Windows owner is started behind :mod:`linkedin_mcp_server.process_gate`, which
forwards the three standard handles and nothing else. Teaching the gate to
forward a fourth would not help either: under a rollback the gate comes off the
same replaced package as the owner, so the version that would forward it is
exactly the version that is not running. Loopback needs no cooperation from
anything in between, and both ends already depend on it, since loopback is how a
frontend talks to the owner it elected.

The channel is authenticated in both directions by the post-spawn handshake
nonce, the same token that authenticates the startup verdict. It is handed over
on the private configuration pipe after the child exists, so no other process on
this machine has seen it: a stranger that reaches the port cannot present it, and
a commit record that does not carry it is not the parent's. Closing is the abort,
exactly as closing the configuration pipe used to be. A parent that goes away
without committing leaves the child reading end of file.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import secrets
import socket
import threading
import time
from typing import Protocol

logger = logging.getLogger(__name__)

#: The child's half of the rendezvous. Fixed length, so the parent reads exactly
#: as many bytes as it expects from a peer it has not authenticated yet.
_ATTACH_PREFIX = "attach "

#: Enough that strangers arriving on the port cannot push the child's own
#: connection out of the queue before the parent gets to it.
_BACKLOG = 16

#: The longest authorization record the child will assemble. The commit record
#: is 78 bytes; this leaves room for a longer one without leaving the read
#: unbounded.
_MAX_RECORD_BYTES = 128

#: A bind that fails this way on IPv6 means the host has none, not that the
#: address is taken. Mirrors ``daemon_owner._bind_loopback``.
_NO_IPV6 = (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT)

#: The absolute ceiling on what one unauthenticated peer may take to present its
#: frame. The share below is what bounds this for every caller there is; the
#: ceiling only binds a wait longer than thirty-two seconds, and it is here so
#: that a future caller with one cannot hand a stranger a proportionally
#: enormous span.
_PEER_SECONDS = 1.0

#: The share of what is *left* of the whole wait that one unauthenticated peer
#: may spend, derived from the backlog rather than chosen: the backlog is how
#: many connections can be queued at once, so it is also the most peers that can
#: already sit ahead of the child when the wait begins. At half of its
#: reciprocal, a full backlog of silent peers costs ``1 - (1 - 1/32)**16``, three
#: eighths of the wait, and the child queued behind them still has the rest.
#:
#: A share and never a fixed span, in either direction. The fixed span was the
#: defect: an owner is accepted within ``daemon_election._PREPARED_READ_SECONDS``,
#: one second, and the per-peer bound was one second too, so the first silent
#: peer consumed the whole wait and the owner queued behind it was killed for
#: never attaching. A *floor* under the share is the same defect one step down,
#: because any allowance that stops shrinking with the wait can be repeated until
#: the wait is gone: at a floor of 50ms, ten silent peers emptied a one-second
#: wait, and the backlog holds sixteen.
_PEER_SHARE = 1.0 / (2 * _BACKLOG)

#: The window a queued child has to be reached inside, and therefore the basis
#: for what one unauthenticated peer ahead of it may spend. It is the wait the
#: parent gives the attachment once a prepared generation exists
#: (``daemon_election._PREPARED_READ_SECONDS``), and a test pins the pair.
#:
#: The basis is this window and not the drain's own budget, which is far longer.
#: Only the peers *ahead* of the child's connection can delay it and the queue
#: holds ``_BACKLOG`` of those, so a share of this window empties a full queue
#: well inside it however long the drain itself is allowed to keep running. A
#: share of the drain's budget instead makes each peer proportionally larger:
#: measured against an eight second spawn, sixteen silent peers took three
#: seconds and the owner behind them was killed inside a one second wait.
_PEER_WINDOW_SECONDS = 1.0

#: How long the drain blocks in one ``accept`` before rechecking whether it has
#: been stopped. Short, so a closed channel leaves no worker behind for long, and
#: long enough that an idle rendezvous is not a spin.
_DRAIN_POLL_SECONDS = 0.05

#: How long :meth:`ControlListener.close` waits for the drain to leave the socket
#: it owns. Deliberately short: the worker is safe by construction once the
#: channel is closed (it can no longer publish a connection, and it closes its
#: own listener), so this only buys the common case where the port is released
#: before ``close`` returns, and never lengthens the caller's own deadline by
#: much.
_DRAIN_JOIN_SECONDS = 0.2


def _peer_allowance(remaining: float) -> float:
    """How long to give one unauthenticated peer, out of the wait that is left.

    Always less than what is left, however little that is, so the connection
    behind this peer keeps a turn. The honest frame does not need a long
    allowance: the child sends it immediately on connect, a whole endpoint
    startup before the parent begins this wait, so it is in the receive buffer
    before the connection is even accepted and the read only has to be scheduled.

    The share cannot be widened to cover a child descheduled between its connect
    and its send, which is the one honest case this refuses: at a share of an
    eighth, sixteen silent peers take nine tenths of the wait, and spending the
    owner's wait on strangers is the defect the share exists for. That child
    loses its attachment and the election runs again.
    """
    if remaining <= 0.0:
        return 0.0
    return min(remaining * _PEER_SHARE, _PEER_SECONDS)


class ControlChannel(Protocol):
    """The child's side: one authenticated record, then end of file."""

    def readline(self) -> str: ...


def _attach_frame(nonce: str) -> bytes:
    return f"{_ATTACH_PREFIX}{nonce}\n".encode("ascii")


class ControlListener:
    """One rendezvous, bound by the parent before the child it belongs to exists.

    Bound before the spawn on purpose. The address travels in the configuration
    record, so it has to be settled before that record is written, and a bind
    that fails then has cost nothing: there is no child to stop.

    Bound before the spawn also means reachable before the child exists, and the
    queue is what that costs. Accepting only once the child has reported a
    prepared generation was the defect: the listen queue is finite, anything
    running as this account can fill it, and a child whose connect finds it full
    blocks until its own connect timeout. It then cannot report the very thing
    the parent was waiting for before it would accept, so the parent kills a
    healthy owner for never attaching. The per-peer allowance does not reach
    this, because it bounds peers ahead of a connection that is already queued.
    So the queue is drained from the moment the nonce exists
    (:meth:`start_accepting`) and the attachment is collected later
    (:meth:`attached_within`).
    """

    def __init__(self, sock: socket.socket) -> None:
        self._lock = threading.Lock()
        self._listener: socket.socket | None = sock
        self._connection: socket.socket | None = None
        self._drain: threading.Thread | None = None
        self._attached = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        address = sock.getsockname()
        self.host: str = str(address[0])
        self.port: int = int(address[1])

    @classmethod
    def open(cls) -> ControlListener:
        """Bind an ephemeral loopback port, IPv6 first and IPv4 after it."""
        for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
            sock: socket.socket | None = None
            try:
                # Inside the guard with the bind: a kernel built without IPv6
                # refuses the family here rather than at the bind, and that is
                # the same answer, not a different failure.
                sock = socket.socket(family, socket.SOCK_STREAM)
                # Deliberately no SO_REUSEADDR, for the reason
                # ``daemon_owner._bind_loopback`` gives: a port the kernel just
                # handed out is unused, and on the BSDs the option would let a
                # second bind succeed against a live listener.
                sock.bind((host, 0))
                sock.listen(_BACKLOG)
            except OSError as exc:
                if sock is not None:
                    sock.close()
                if family is socket.AF_INET6 and exc.errno in _NO_IPV6:
                    continue
                raise
            return cls(sock)
        raise OSError("No loopback address could be bound for the owner control")

    def start_accepting(self, *, nonce: str, timeout: float) -> None:
        """Begin emptying the listen queue, before anything waits on the child.

        Started as soon as the post-spawn nonce exists, which is the earliest
        moment a peer can be authenticated at all, and a whole child startup
        before the parent has anything to wait for. Strangers are read and
        discarded as they arrive rather than accumulating, so the child's own
        connect finds room in the queue whenever it gets there.

        The listener socket moves to the worker and only the worker closes it. A
        descriptor closed under a blocked ``accept`` can be reissued to an
        unrelated socket in this process, and the accept would then return a
        connection belonging to that one.

        Idempotent, so a caller that cannot tell whether it already started the
        drain does not start a second one. *timeout* bounds the whole drain: past
        it the worker stops, exactly as the old single accept did.
        """
        expected = _attach_frame(nonce)
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            if self._drain is not None:
                return
            listener = self._listener
            if self._closed or listener is None:
                raise OSError("The owner control channel is closed")
            self._listener = None
            drain = threading.Thread(
                target=self._drain_queue,
                args=(listener, expected, deadline),
                name="daemon-control-accept",
                daemon=True,
            )
            self._drain = drain
        try:
            drain.start()
        except BaseException:
            # The listener left this object above and the worker never took it,
            # so put it back rather than leave a published thread `close` would
            # try to join and a socket nothing holds.
            with self._lock:
                self._drain = None
                orphaned = self._closed
                if not orphaned:
                    self._listener = listener
            if orphaned:
                with contextlib.suppress(OSError):
                    listener.close()
            raise

    def attached_within(self, *, timeout: float) -> None:
        """Wait for the attachment the drain authenticated.

        Only a wait: the accepting happened while the child was starting, so in
        the ordinary case this returns at once. It is still the parent's
        pre-commit requirement, because nothing may be authorized before a peer
        has presented the nonce.
        """
        with self._lock:
            if self._drain is None:
                raise OSError("The owner control channel is not accepting")
            if self._closed:
                raise OSError("The owner control channel is closed")
        if not self._attached.wait(max(timeout, 0.0)):
            raise TimeoutError("The daemon did not attach to its control channel")

    def _drain_queue(
        self, listener: socket.socket, expected: bytes, deadline: float
    ) -> None:
        """Accept and authenticate until the child is found, or time runs out.

        Anything that is not the child is refused and the drain continues,
        because a stranger reaching a loopback port cannot be prevented and must
        not be able to spend the child's rendezvous. Each one may spend only a
        share of ``_PEER_WINDOW_SECONDS``, and less than that once the drain has
        less than a window left to run.
        """
        try:
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.debug("The owner control channel stopped accepting in time")
                    return
                listener.settimeout(min(remaining, _DRAIN_POLL_SECONDS))
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    logger.debug(
                        "The owner control channel could not accept", exc_info=True
                    )
                    return
                window = min(deadline - time.monotonic(), _PEER_WINDOW_SECONDS)
                if self._proves_itself(connection, expected, time.monotonic() + window):
                    self._keep(connection)
                    return
                logger.debug(
                    "An unauthenticated peer reached the owner control channel"
                )
                with contextlib.suppress(OSError):
                    connection.close()
        finally:
            with contextlib.suppress(OSError):
                listener.close()

    def _keep(self, connection: socket.socket) -> None:
        """Publish the authenticated connection, unless the channel is gone.

        The one place the worker touches shared state, and it declines to touch
        it after :meth:`close`. A worker that published into a closed channel
        would hand a record-carrying socket to a listener object the caller has
        already finished with, and the abort that close is supposed to be would
        never reach the child.
        """
        with self._lock:
            keeping = not self._closed and self._connection is None
            if keeping:
                self._connection = connection
        if not keeping:
            with contextlib.suppress(OSError):
                connection.close()
            return
        self._attached.set()

    def send(self, record: str) -> None:
        """Deliver one authorization record to the attached child."""
        connection = self._connection
        if connection is None:
            raise OSError("No daemon is attached to the owner control channel")
        connection.sendall(record.encode("ascii"))

    def close(self) -> None:
        """End the channel. Without a record first, this is the abort.

        The drain is told to stop and joined briefly, but the listener it owns is
        never closed from here: see :meth:`start_accepting` for why closing a
        descriptor under a blocked ``accept`` is not a shortcut. A worker that
        outlives the join is harmless, because :meth:`_keep` refuses to publish
        into a closed channel and the worker closes its own socket on the way
        out.
        """
        with self._lock:
            self._closed = True
            drain = self._drain
            listener, self._listener = self._listener, None
            connection, self._connection = self._connection, None
        self._stop.set()
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.close()
        if drain is not None:
            drain.join(timeout=_DRAIN_JOIN_SECONDS)
            if drain.is_alive():
                logger.debug("The owner control channel worker is still unwinding")

    @staticmethod
    def _proves_itself(
        connection: socket.socket, expected: bytes, deadline: float
    ) -> bool:
        """Whether this peer presented the nonce, within the caller's deadline.

        Exactly as many bytes as the frame occupies, so a peer cannot make this
        read further than the parent agreed to read, and compared whole rather
        than while it arrives, so a wrong nonce costs the same wherever it
        diverges. It also takes only a share of the time the caller has left, so
        a peer that never speaks cannot spend the wait of the one behind it.
        """
        presented = bytearray()
        began = time.monotonic()
        peer_deadline = began + _peer_allowance(deadline - began)
        try:
            while len(presented) < len(expected):
                connection.settimeout(max(peer_deadline - time.monotonic(), 0.0))
                piece = connection.recv(len(expected) - len(presented))
                if not piece:
                    return False
                presented.extend(piece)
        except OSError:
            return False
        return secrets.compare_digest(bytes(presented), expected)


class _AttachedControl:
    """The child's end, read as one line so it frames like the pipe it replaces.

    A reset reads as end of file rather than as an error, and that is the point
    rather than a convenience: a parent whose socket dies has not committed, and
    the child must reach the same abort it reaches when the parent closes.
    """

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._stream = connection.makefile("r", encoding="ascii", newline="\n")
        # Serializes the close sequence so a second caller cannot start closing
        # the wrapper while the first is still interrupting the socket under it.
        self._closing = threading.Lock()

    def readline(self) -> str:
        try:
            # Bounded, so nothing on this socket can grow without a newline. A
            # truncated record simply is not the commit record and aborts.
            return self._stream.readline(_MAX_RECORD_BYTES)
        except (OSError, ValueError):
            # ValueError covers a closed stream and an undecodable byte. Neither
            # can have come from the parent, so both mean the same as a close.
            logger.debug("The owner control channel ended", exc_info=True)
            return ""

    def close(self) -> None:
        """End the channel, even while another thread is parked in a read.

        The shutdown is not tidiness, it is the whole of this method. Closing
        the wrapper first deadlocks whenever a read is in flight: CPython's
        buffered close takes the same lock :meth:`readline` holds for the
        duration of its ``recv``, and that ``recv`` only returns when the peer
        writes or hangs up. A parent that keeps its socket open and never
        commits satisfies neither, so the close never returns. Measured on
        CPython 3.13 (darwin): with a reader parked and the peer open, closing
        the wrapper alone never came back, while a ``shutdown`` first let it
        return in under a millisecond.

        That matters here rather than in the abstract, because this is the
        owner's teardown path. ``daemon_owner.main`` closes the control channel
        in its ``finally``, *before* it releases the daemon lock, so a close
        that never returns leaves a process that has already given up holding
        the lock every future election needs.

        ``shutdown`` rather than a close of the socket, because the descriptor
        must stay valid while a reader is still inside it: closing it under a
        blocked ``recv`` frees the number for reuse and the parked read can then
        be answered by an unrelated socket in this process. ``shutdown`` leaves
        the descriptor in place and only makes it end. The wrapper close that
        follows is what proves nobody is inside the read any more, since it
        cannot complete until the reader has released the buffer lock, so by the
        time the socket itself is closed the descriptor is unused.

        The abort semantics are unchanged: the interrupted read returns end of
        file, which is what a parent that closed without committing produces,
        and what the child already treats as "not authorized".

        Safe to call twice and from two threads at once. The lock orders the
        sequence, every step is idempotent, and a ``shutdown`` that arrives
        after the socket is closed raises ``EBADF`` against a detached
        descriptor (CPython sets its ``fileno`` to -1 on close) rather than
        reaching whatever now holds that number.
        """
        with self._closing:
            with contextlib.suppress(OSError):
                self._connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError, ValueError):
                self._stream.close()
            with contextlib.suppress(OSError):
                self._connection.close()


def attach(host: str, port: int, *, nonce: str, timeout: float) -> ControlChannel:
    """Connect to the parent's rendezvous and prove which child this is."""
    connection = socket.create_connection((host, port), timeout=max(timeout, 0.0))
    try:
        connection.sendall(_attach_frame(nonce))
        # Blocking from here on. The record may be a whole startup away, and the
        # caller bounds that wait against its own commit deadline.
        connection.settimeout(None)
    except BaseException:
        connection.close()
        raise
    return _AttachedControl(connection)
