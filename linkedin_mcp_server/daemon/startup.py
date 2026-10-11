"""The one startup protocol between a frontend and the owner it spawns.

Everything travels on the child's own standard input and output, in both
directions, as length-prefixed JSON frames. Not a loopback port and not an
inherited descriptor: a port is reachable by anything running as this account,
so it needs an authentication step of its own and a queue strangers can fill,
and ``pass_fds`` is POSIX only while the Windows owner starts behind
:mod:`linkedin_mcp_server.process_gate`, which forwards the three standard
handles and nothing else. The pipes reach exactly one process, need nobody's
cooperation in between, and end when that process does.

The exchange, in order::

    frontend -> HELLO     {nonce, protocol}
    owner    -> sync line, then ACK {protocol, build}
    frontend -> CONFIG    {config}
    owner    -> PREPARED  {instance}    or a VERDICT that ends the startup
    frontend -> COMMIT    {instance}
    owner    -> VERDICT   {status}

Every frame after HELLO carries the HELLO's nonce, which is created after the
child exists and is never on its command line or in its environment, so output
written before the child read it cannot pass for one of these frames.

**Nothing usable crosses before ACK.** The configuration carries
``proxy_password`` and decides which browser opens against a logged-in session,
so it is written only once the child has answered HELLO in this framing, with
this protocol number and the frontend's own build identity
(:func:`linkedin_mcp_server.daemon.version.build_identity`). A child that does
none of that, an earlier owner that reads its standard input to end of file
included, never sees it: it is stopped when the frontend's wait runs out.

**The frontend may kill the child until it writes COMMIT.** From the first
byte of COMMIT the frame may have arrived, the child may already own itself (on
Windows it adopts its own Job before publishing) and its descriptor may be
canonical, so only the lock holder can settle publication. After that only an
authenticated VERDICT saying the child did not publish permits a kill; silence
or a malformed verdict does not. End of file on the child's standard input
before COMMIT is the abort.

**The sync line.** Interpreter startup hooks (a ``.pth`` file, a
``sitecustomize``) run before the owner can claim its standard output, and may
write to it. So the owner's first output is one line naming the nonce, found by
the same bounded scan that authenticates other post-spawn status lines
(:func:`linkedin_mcp_server.process_protocol.read_authenticated_status`), and
framing is strict from the byte after it. The owner moves file descriptor 1 to
standard error before it writes anything, so nothing else in the process can put
a byte between two frames.

Frames are 1 to 65,536 bytes of strict UTF-8 JSON naming exactly the fields of
their type. Anything else, a zero or oversized length, a header or body cut off
by end of file, duplicate keys, a non-object, an unknown field or a wrong nonce,
ends the exchange; neither side tries to resynchronise. Reads are blocking and
every wait on them is bounded by the side that started it, so a silent peer
costs that side's deadline and nothing more.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import queue
import secrets
import struct
import sys
import threading
from collections.abc import Callable
from typing import Any, BinaryIO, cast

from linkedin_mcp_server.process_protocol import read_authenticated_status, valid_nonce

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

#: The one place that names the startup protocol. Both ends are one
#: installation, so this changes only together with the frames below. The
#: number continues from the line-record protocols before this framing (1 to 3
#: released, 4 never), so no number ever names two different exchanges.
PROTOCOL = 5

#: The largest frame body either side writes or accepts. A configuration is a
#: few kilobytes; this leaves room for long proxy bypass lists and paths while
#: keeping every read bounded before its body is allocated.
MAX_FRAME_BYTES = 65_536

_HEADER = struct.Struct("!I")

HELLO = "hello"
ACK = "ack"
CONFIG = "config"
PREPARED = "prepared"
COMMIT = "commit"
VERDICT = "verdict"

#: What a VERDICT may say. ``failed``, ``aborted`` and ``retry`` may also stand
#: in place of PREPARED, when the owner cannot get that far.
COMMITTED = "committed"
FAILED = "failed"
ABORTED = "aborted"
RETRY = "retry"
UNCERTAIN = "uncertain"
VERDICTS = frozenset({COMMITTED, FAILED, ABORTED, RETRY, UNCERTAIN})

#: The fields each frame carries besides ``type`` and ``nonce``, exactly.
_FIELDS: dict[str, frozenset[str]] = {
    HELLO: frozenset({"protocol"}),
    ACK: frozenset({"protocol", "build"}),
    CONFIG: frozenset({"config"}),
    PREPARED: frozenset({"instance"}),
    COMMIT: frozenset({"instance"}),
    VERDICT: frozenset({"status"}),
}

_SYNC_PREFIX = b"owner-startup "

#: How long closing the frontend's end waits for its writer to let go of the
#: child's standard input. Short: the writer is idle unless the child stopped
#: reading, and then it is unwound by the child's death rather than by waiting.
_INPUT_CLOSE_SECONDS = 0.2


class ProtocolError(ValueError):
    """A frame that is not what this protocol permits at this point."""


def message(kind: str, nonce: str, **fields: object) -> dict[str, object]:
    """Build one frame's content."""
    return {"type": kind, "nonce": nonce, **fields}


def encode(content: dict[str, object]) -> bytes:
    """One frame: a 4-byte big-endian length, then the JSON body."""
    body = json.dumps(
        content, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    if not 0 < len(body) <= MAX_FRAME_BYTES:
        raise ProtocolError(
            f"A startup frame of {len(body)} bytes exceeds {MAX_FRAME_BYTES}"
        )
    return _HEADER.pack(len(body)) + body


def fits(kind: str, **fields: object) -> bool:
    """Whether a frame with these fields can be sent at all, before any child exists."""
    try:
        encode(message(kind, "0" * 64, **fields))
    except (ProtocolError, TypeError, ValueError):
        return False
    return True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    content: dict[str, Any] = {}
    for key, value in pairs:
        if key in content:
            raise ProtocolError("A startup frame repeats a key")
        content[key] = value
    return content


def _no_constant(name: str) -> object:
    raise ProtocolError(f"A startup frame carries the non-JSON constant {name}")


def decode(body: bytes) -> dict[str, object]:
    """Parse one frame body, refusing anything :func:`encode` cannot produce."""
    try:
        text = body.decode("utf-8")
        content = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_no_constant
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("A startup frame is not valid JSON") from exc
    if not isinstance(content, dict):
        raise ProtocolError("A startup frame is not an object")
    return content


def read_frame(read: Callable[[int], bytes]) -> dict[str, object]:
    """Read exactly one frame through *read*, which returns short only at EOF.

    :class:`EOFError` means the peer ended cleanly between frames; a frame cut
    off anywhere inside is a :class:`ProtocolError`, because a peer that stops
    halfway did not say anything.
    """
    header = read(_HEADER.size)
    if not header:
        raise EOFError("The startup peer closed its end")
    if len(header) < _HEADER.size:
        raise ProtocolError("A startup frame header was cut off")
    (size,) = _HEADER.unpack(header)
    if not 0 < size <= MAX_FRAME_BYTES:
        # Refused before the body is read, so a hostile length costs nothing.
        raise ProtocolError(f"A startup frame announces {size} bytes")
    body = read(size)
    if len(body) < size:
        raise ProtocolError("A startup frame body was cut off")
    return decode(body)


def same_nonce(value: object, nonce: str) -> bool:
    """Whether *value* is *nonce*, compared without an early exit."""
    if not isinstance(value, str):
        return False
    return secrets.compare_digest(
        value.encode("utf-8", "surrogatepass"), nonce.encode("ascii")
    )


def expect(content: object, kind: str, *, nonce: str | None) -> dict[str, object]:
    """Return *content* if it is exactly a *kind* frame for *nonce*.

    *nonce* is ``None`` only for HELLO, which is where the owner learns it.
    """
    if not isinstance(content, dict) or content.get("type") != kind:
        raise ProtocolError(f"Expected a {kind} startup frame")
    if set(content) != {"type", "nonce"} | _FIELDS[kind]:
        raise ProtocolError(f"A {kind} startup frame has the wrong fields")
    presented = content["nonce"]
    if nonce is None:
        if not valid_nonce(presented):
            raise ProtocolError("A startup frame carries no valid nonce")
    elif not same_nonce(presented, nonce):
        raise ProtocolError("A startup frame carries another nonce")
    if "protocol" in content and type(content["protocol"]) is not int:
        raise ProtocolError("A startup frame names its protocol with a non-integer")
    if "build" in content and not _printable(content["build"]):
        raise ProtocolError("A startup frame names no build")
    if "config" in content and not isinstance(content["config"], dict):
        raise ProtocolError("A startup frame carries no configuration object")
    if "instance" in content and not _instance_id(content["instance"]):
        raise ProtocolError("A startup frame names no instance")
    if "status" in content and (
        not isinstance(content["status"], str) or content["status"] not in VERDICTS
    ):
        raise ProtocolError("A startup frame names an unknown verdict")
    return content


def _printable(value: object) -> bool:
    return (
        isinstance(value, str) and bool(value) and all(c.isprintable() for c in value)
    )


def _instance_id(value: object) -> bool:
    return _printable(value) and not any(c.isspace() for c in cast(str, value))


def sync_line(nonce: str) -> bytes:
    """The one line the owner writes before its first frame."""
    return _SYNC_PREFIX + nonce.encode("ascii") + b"\n"


# --------------------------------------------------------------------- frontend


class _ActiveRead:
    """Who may close the owner's standard output, so that exactly one side does.

    The reader owns the close while its read is in flight, the caller once that
    read is over. Both answers are given under one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reading = True
        self._abandoned = False

    def finish(self, stream: Any) -> None:
        """End the read, closing the pipe if the caller has already left it."""
        with self._lock:
            self._reading = False
            abandoned = self._abandoned
        if abandoned:
            _close_output(stream)

    def abandon(self) -> bool:
        """Whether the caller may close: true once the read is over."""
        with self._lock:
            if not self._reading:
                return True
            self._abandoned = True
            return False


def _close_output(stream: Any) -> None:
    """Close the descriptor rather than the buffer, which a parked reader holds.

    ``BufferedReader.close()`` waits for that reader's I/O lock, so it blocks for
    as long as the child stays silent: measured at 29.27 of thirty seconds.
    ``detach()`` first, or the raw object's finalizer prints ``Bad file
    descriptor`` from a context nothing can catch.
    """
    # `Popen.stdout` is declared as a plain `IO`, which has no `detach`; the
    # object is a BufferedReader in practice, and the fallback covers a caller
    # that handed us something else rather than assuming.
    detach = getattr(stream, "detach", None)
    try:
        raw = detach() if callable(detach) else stream
        raw.close()
    except (OSError, ValueError):
        # Already gone, which is the ordinary case for a child that exited.
        logger.debug("The owner's standard output was already closed", exc_info=True)


class _Writer:
    """The only thread that touches the child's standard input.

    It owns the stream from the moment it starts, close included. A write
    blocked on a child that stopped reading holds the ``BufferedWriter`` lock a
    ``close`` needs, so a close from any other thread would wait on that write
    with no deadline of its own. The caller waits on each write with its own
    deadline instead, and the thread unwinds when the child's death breaks the
    pipe.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._requests: queue.Queue[bytes | None] = queue.Queue()
        self._results: queue.Queue[BaseException | None] = queue.Queue()
        self._closed = threading.Event()
        self._broken = False
        self._thread = threading.Thread(
            target=self._run, name="daemon-startup-input", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while (data := self._requests.get()) is not None:
                try:
                    self._stream.write(data)
                    self._stream.flush()
                except BaseException as exc:  # noqa: BLE001 - reported to the caller
                    self._results.put(exc)
                    return
                self._results.put(None)
        finally:
            with contextlib.suppress(OSError, ValueError):
                self._stream.close()
            self._closed.set()

    def write(self, data: bytes, *, timeout: float) -> None:
        if self._broken:
            raise OSError("The owner's standard input is no longer writable")
        self._requests.put(data)
        try:
            failure = self._results.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            # The write may still complete. Nothing after it may be queued,
            # because the caller can no longer tell which result is whose.
            self._broken = True
            raise TimeoutError("The daemon did not read its startup input in time")
        if failure is not None:
            self._broken = True
            raise failure

    def close(self, *, timeout: float) -> None:
        """End the child's standard input, which before COMMIT is the abort."""
        self._broken = True
        self._requests.put(None)
        self._closed.wait(max(timeout, 0.0))


class _Reader:
    """The only thread that reads the child's standard output.

    One thread for the whole exchange, so frames are read in order and the
    caller waits on a queue with its own deadline. It stops after a VERDICT, at
    end of file and at the first frame it cannot accept, and hands over
    ``None`` for the last two.
    """

    def __init__(self, stream: Any, nonce: str) -> None:
        self.stream = stream
        self._nonce = nonce
        self.active = _ActiveRead()
        self._messages: queue.Queue[dict[str, object] | None] = queue.Queue()
        self._ended = False
        self._thread = threading.Thread(
            target=self._run, name="daemon-startup-output", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        final: dict[str, object] | None = None
        try:
            expected = sync_line(self._nonce)
            marker = expected.removesuffix(b"\n")
            synced = read_authenticated_status(
                cast(BinaryIO, self.stream),
                marker=marker,
                parse=lambda line: True if line == expected else None,
            )
            if synced is None:
                return
            while True:
                content = read_frame(self.stream.read)
                if not same_nonce(content.get("nonce"), self._nonce):
                    raise ProtocolError("A startup frame carries another nonce")
                if content.get("type") == VERDICT:
                    final = content
                    return
                self._messages.put(content)
        except (EOFError, OSError, ValueError, RecursionError):
            logger.debug("The owner's startup output ended", exc_info=True)
        finally:
            # Before the last answer is handed over, so a caller that reads it
            # and then releases the pipe finds a read that is over and closes it.
            self.active.finish(self.stream)
            self._messages.put(final)

    def receive(self, *, timeout: float) -> dict[str, object] | None:
        if self._ended:
            return None
        try:
            content = self._messages.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise TimeoutError("The daemon did not answer in time") from None
        if content is None or content.get("type") == VERDICT:
            self._ended = True
        return content


class FrontendChannel:
    """The frontend's end of the exchange, over the pipes ``Popen`` created.

    Both pipes leave the ``Popen`` object here, so nothing on the frontend's
    deadline path can close one behind the thread that owns it. Created only
    once the child may run, which on Windows is after its gate is released.
    """

    def __init__(self, child: Any, nonce: str) -> None:
        if not valid_nonce(nonce):
            raise ValueError("The owner startup nonce is invalid")
        stdin, stdout = child.stdin, child.stdout
        if stdin is None or stdout is None:
            raise OSError("The owner was started without its startup pipes")
        self.nonce = nonce
        self._writer = _Writer(stdin)
        self._reader: _Reader | None = _Reader(stdout, nonce)
        self._writer.start()
        child.stdin = None
        try:
            self._reader.start()
        except BaseException:
            # The writer took the input; ending it is the abort. The output is
            # still the caller's, on ``Popen``, with no reader to race.
            self._reader = None
            self._writer.close(timeout=0.0)
            raise
        child.stdout = None

    def send(self, kind: str, *, timeout: float, **fields: object) -> None:
        """Write one frame, or raise if it was not written within *timeout*."""
        self._writer.write(encode(message(kind, self.nonce, **fields)), timeout=timeout)

    def receive(self, *, timeout: float) -> dict[str, object] | None:
        """The next frame, ``None`` once the output ended or was refused.

        Raises :class:`TimeoutError` when nothing arrived in time; the read
        stays parked, and a later call picks up whatever it finds.
        """
        if self._reader is None:
            return None
        return self._reader.receive(timeout=timeout)

    def close(self) -> None:
        """End the input, then stop listening, without waiting for the child.

        Closing the output under a parked reader is what makes this prompt on
        POSIX: the pending read fails and the thread unwinds. Windows closes
        through the UCRT, which waits for the same handle lock the blocked
        ``ReadFile`` holds, so after a commit whose verdict timed out the close
        would wait for an owner that keeps its output open for as long as it
        runs. There a read still in flight keeps its pipe and closes it itself
        at end of file.
        """
        self._writer.close(timeout=_INPUT_CLOSE_SECONDS)
        reader, self._reader = self._reader, None
        if reader is None:
            return
        if _IS_WINDOWS and not reader.active.abandon():
            logger.debug("The owner's output stays with its reader until end of file")
            return
        _close_output(reader.stream)


# ------------------------------------------------------------------------ owner


class OwnerChannel:
    """The owner's end: private copies of its standard input and output.

    Reads are blocking; the owner bounds each wait itself. A read may be left
    parked by a wait that gave up, so the input descriptor is closed only once
    no read is inside it: closing a descriptor under a blocked read frees its
    number for reuse, and the parked read can then be answered by an unrelated
    file in this process.
    """

    def __init__(self, inbound: int, outbound: int) -> None:
        self._lock = threading.Lock()
        self._inbound: int | None = inbound
        self._outbound: int | None = outbound
        self._reads = 0
        self._abandoned = False

    @classmethod
    def claim(cls) -> OwnerChannel:
        """Take standard input and output for the exchange, and point both away.

        The protocol gets private duplicates, and descriptor 1 is pointed at
        standard error, so anything else that writes there, this process or a
        child it starts, lands in the bootstrap diagnostics and later the daemon
        log instead of between two frames. Descriptor 0 is pointed at the null
        device for the same reason in the other direction: a stray reader must
        not consume the commit. The duplicates are not inheritable, so no
        browser started later holds the frontend's pipes open.
        """
        with contextlib.suppress(AttributeError, OSError, ValueError):
            sys.stdout.flush()
        inbound = os.dup(0)
        try:
            outbound = os.dup(1)
        except BaseException:
            os.close(inbound)
            raise
        try:
            os.dup2(2, 1)
            null = os.open(os.devnull, os.O_RDONLY)
            try:
                os.dup2(null, 0)
            finally:
                os.close(null)
            if _IS_WINDOWS:  # pragma: no cover - exercised on the Windows runner
                # Text mode would turn a length byte of 0x1A into end of file
                # and rewrite line endings inside a frame.
                msvcrt = importlib.import_module("msvcrt")
                msvcrt.setmode(inbound, os.O_BINARY)
                msvcrt.setmode(outbound, os.O_BINARY)
        except BaseException:
            os.close(inbound)
            os.close(outbound)
            raise
        return cls(inbound, outbound)

    def _chunk(self, size: int) -> bytes:
        with self._lock:
            descriptor = self._inbound
            if descriptor is None:
                raise OSError("The owner's startup input is closed")
            self._reads += 1
        try:
            return os.read(descriptor, size)
        finally:
            with self._lock:
                self._reads -= 1
                close = self._abandoned and self._reads == 0
                if close:
                    self._inbound = None
            if close:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _exactly(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            piece = self._chunk(size - len(data))
            if not piece:
                break
            data.extend(piece)
        return bytes(data)

    def read(self) -> dict[str, object]:
        """Block for one frame from the frontend."""
        return read_frame(self._exactly)

    def _write(self, data: bytes) -> None:
        descriptor = self._outbound
        if descriptor is None:
            raise OSError("The owner's startup output is closed")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]

    def send(self, kind: str, nonce: str, **fields: object) -> None:
        """Write one frame to the frontend."""
        self._write(encode(message(kind, nonce, **fields)))

    def accept(self, *, build: str) -> tuple[str, dict[str, object]]:
        """The owner's half up to CONFIG: HELLO in, ACK out, CONFIG in.

        Returns the nonce and the configuration payload. A HELLO naming another
        protocol is refused without an answer: a frontend that does not speak
        this one could not read it either.
        """
        hello = expect(self.read(), HELLO, nonce=None)
        if hello["protocol"] != PROTOCOL:
            raise ProtocolError("The frontend speaks another startup protocol")
        nonce = cast(str, hello["nonce"])
        self._write(sync_line(nonce))
        self.send(ACK, nonce, protocol=PROTOCOL, build=build)
        configuration = expect(self.read(), CONFIG, nonce=nonce)
        return nonce, cast(dict[str, object], configuration["config"])

    def close_output(self) -> None:
        """End the output, which the frontend reads as no further answer."""
        with self._lock:
            descriptor, self._outbound = self._outbound, None
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def close(self) -> None:
        """End both directions; the input waits for a parked read to return."""
        self.close_output()
        with self._lock:
            if self._reads:
                self._abandoned = True
                return
            descriptor, self._inbound = self._inbound, None
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
