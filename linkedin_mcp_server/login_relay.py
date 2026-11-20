"""Carry the sign-in view's calls to the process that owns the login.

Claude Desktop runs one stdio server entry as two server processes: the
model's ``open_login`` lands in one, the app's ``login_view_attach``,
``login_view_frame`` and ``login_view_input`` may land in the other. Both
share only the auth root. So every process that runs a ``LoginAttempt``
serves those three operations for it on a Unix socket in the auth root,
``<auth root>/login-view/<attempt id>.sock``, and a process that does not know
an attempt forwards the call there and returns the owner's result as it came.

The owner enforces everything: grants, opens, page ids and input rules run
once, in the process that holds the attempt. This module only moves bytes:
one request per connection, a 4-byte big-endian length and a JSON object each
way, at most :data:`MAX_MESSAGE_BYTES`. The decoding rules follow
``daemon/startup.py``: strict UTF-8, no repeated keys, no non-JSON constants,
an object body, the length refused before the body is read.

Trust: the socket lives in the auth root next to the session cookies, mode
0600 in a 0700 directory, and accepts only the three view operations. Anything
that can connect could already read the session there.

The relay needs asyncio's Unix sockets (Linux, macOS; Docker is Linux) and a
socket path that fits ``sockaddr_un``. Without either there is no relay: a
view in the owner's own process still works, a view in another process ends
with the fallback help, and the login itself is unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import stat
import struct
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from linkedin_mcp_server import session_state
from linkedin_mcp_server.common_utils import secure_mkdir
from linkedin_mcp_server.login_view import LoginAttempt

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 1024 * 1024
CONNECT_TIMEOUT_S = 5.0
CLOSE_TIMEOUT_S = 1.0
ENDPOINT_DIR = "login-view"

Operation = Literal["login_view_attach", "login_view_frame", "login_view_input"]
_OPERATIONS: dict[str, Operation] = {
    "login_view_attach": "login_view_attach",
    "login_view_frame": "login_view_frame",
    "login_view_input": "login_view_input",
}

_HEADER = struct.Struct("!I")
#: What ``LoginAttempt`` ids look like; nothing else ever names a socket path.
_ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{12}\Z")
#: ``sockaddr_un.sun_path`` holds this many bytes, the terminating NUL included.
_PATH_LIMIT = 104 if sys.platform == "darwin" else 108
_SUPPORTED = hasattr(socket, "AF_UNIX") and sys.platform != "win32"


class RelayProtocolError(Exception):
    """A relay message that this protocol cannot have produced."""


@dataclass(frozen=True, slots=True)
class Envelope:
    """One view tool result on the wire.

    The error flag travels on its own, because an error result's text and its
    ``_meta`` do not say it is one. *image* is the JPEG already in base64.
    """

    is_error: bool
    text: str
    image: str | None = None
    meta: dict[str, Any] | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "is_error": self.is_error,
            "text": self.text,
            "image": self.image,
            "meta": self.meta,
        }

    @classmethod
    def from_wire(cls, content: Mapping[str, object]) -> Envelope:
        is_error, text = content.get("is_error"), content.get("text")
        image, meta = content.get("image"), content.get("meta")
        if (
            set(content) != {"is_error", "text", "image", "meta"}
            or not isinstance(is_error, bool)
            or not isinstance(text, str)
            or not (image is None or isinstance(image, str))
            or not (meta is None or isinstance(meta, dict))
        ):
            raise RelayProtocolError("A relay reply has the wrong shape")
        return cls(is_error, text, image, meta)


#: Runs one operation locally, arguments not yet validated, and never forwards.
Handler = Callable[[Operation, Mapping[str, object]], Awaitable[Envelope]]


def encode(content: Mapping[str, object]) -> bytes:
    """One message: a 4-byte big-endian length, then the JSON body."""
    body = json.dumps(
        content, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    if not 0 < len(body) <= MAX_MESSAGE_BYTES:
        raise RelayProtocolError(f"A relay message of {len(body)} bytes is too large")
    return _HEADER.pack(len(body)) + body


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    content: dict[str, Any] = {}
    for key, value in pairs:
        if key in content:
            raise RelayProtocolError("A relay message repeats a key")
        content[key] = value
    return content


def _no_constant(name: str) -> object:
    raise RelayProtocolError(f"A relay message carries the non-JSON constant {name}")


def decode(body: bytes) -> dict[str, object]:
    """Parse one message body, refusing anything :func:`encode` cannot produce."""
    try:
        content = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_no_constant,
        )
    except RelayProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RelayProtocolError("A relay message is not valid JSON") from exc
    if not isinstance(content, dict):
        raise RelayProtocolError("A relay message is not an object")
    return content


async def read_message(reader: asyncio.StreamReader) -> dict[str, object]:
    """Read exactly one message; the caller bounds the wait.

    Raises:
        EOFError: The peer closed before sending a header.
        RelayProtocolError: The message is cut off, too large or malformed.
    """
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise EOFError("The relay peer closed its end") from exc
        raise RelayProtocolError("A relay message header was cut off") from exc
    (size,) = _HEADER.unpack(header)
    if not 0 < size <= MAX_MESSAGE_BYTES:
        # Refused before the body is read, so a hostile length costs nothing.
        raise RelayProtocolError(f"A relay message announces {size} bytes")
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as exc:
        raise RelayProtocolError("A relay message body was cut off") from exc
    return decode(body)


async def write_message(
    writer: asyncio.StreamWriter, content: Mapping[str, object]
) -> None:
    writer.write(encode(content))
    await writer.drain()


async def _close_stream(writer: asyncio.StreamWriter, timeout_s: float) -> None:
    """Close *writer*, and abort it when the peer holds the close past *timeout_s*."""
    writer.close()
    with contextlib.suppress(Exception):
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout_s)
        except TimeoutError:
            writer.transport.abort()


def endpoint_path(attempt_id: str) -> Path | None:
    """Where the owner of *attempt_id* listens, or ``None`` when there is no relay.

    Built only from a validated attempt id under the canonical auth root, never
    from a path anybody supplied. ``None`` on a platform without Unix sockets
    and for a path longer than ``sockaddr_un`` holds.
    """
    if not _SUPPORTED or not _ATTEMPT_ID.match(attempt_id):
        return None
    path = session_state.auth_root_dir() / ENDPOINT_DIR / f"{attempt_id}.sock"
    if len(os.fsencode(path)) + 1 > _PATH_LIMIT:
        return None
    return path


@dataclass(eq=False)
class _Endpoint:
    """One attempt's listening socket, and the requests it accepted."""

    attempt_id: str
    path: Path
    identity: tuple[int, int]
    sock: socket.socket
    started: asyncio.Task[None] | None = None
    server: asyncio.Server | None = None
    accepted: dict[asyncio.Task[Any], asyncio.StreamWriter] = field(
        default_factory=dict
    )
    timer: asyncio.TimerHandle | None = None
    closed: bool = False

    def close(self) -> None:
        """Stop listening, abort accepted requests, remove the socket if it is ours.

        Synchronous, so neither a peer that stops reading nor a cancelled
        caller can hold or skip any of it.
        """
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
        if self.started is not None and not self.started.done():
            self.started.cancel()
        if self.server is not None:
            self.server.close()
        self.sock.close()
        for task, writer in self.accepted.items():
            writer.transport.abort()
            task.cancel()
        _remove_if_ours(self.path, self.identity)


@dataclass
class _Relay:
    handler: Handler | None = None
    status_tail_s: Callable[[], float] = lambda: 0.0
    endpoints: dict[str, _Endpoint] = field(default_factory=dict)


_relay = _Relay()


def install(handler: Handler, *, status_tail_s: Callable[[], float]) -> None:
    """Serve view calls through *handler* for every attempt this process runs.

    The first server built in a process installs its operations; a process
    runs one server, and a second one built for a test reaches the first
    through the socket like another process would.
    """
    if _relay.handler is None:
        _relay.handler = handler
        _relay.status_tail_s = status_tail_s


def reset_for_testing() -> None:
    """Forget the installed handler and drop every endpoint without awaiting."""
    global _relay
    for endpoint in _relay.endpoints.values():
        endpoint.closed = True
        if endpoint.timer is not None:
            endpoint.timer.cancel()
        for task in (endpoint.started, *endpoint.accepted):
            if task is not None and not task.done():
                task.cancel()
        if endpoint.server is not None:
            endpoint.server.close()
        endpoint.sock.close()
        with contextlib.suppress(OSError):
            _remove_if_ours(endpoint.path, endpoint.identity, claim=False)
    _relay = _Relay()


def serve(attempt: LoginAttempt, task: asyncio.Task[None]) -> None:
    """Listen for *attempt*'s view calls until its status tail ends.

    Called when the attempt is created, so the socket listens, with mode 0600,
    before any ``open_login`` returns the attempt's id. Never raises: a relay
    that cannot start leaves only views in other processes without a route.
    """
    if _relay.handler is None:
        return
    try:
        endpoint = _bind(attempt.id)
    except Exception:
        logger.warning("The sign-in view relay is not available", exc_info=True)
        return
    if endpoint is None:
        return
    _relay.endpoints[attempt.id] = endpoint
    endpoint.started = asyncio.get_running_loop().create_task(
        _start(endpoint), name="linkedin-login-view-relay"
    )
    task.add_done_callback(lambda _task: _retire_after_tail(endpoint))


def _bind(attempt_id: str) -> _Endpoint | None:
    path = endpoint_path(attempt_id)
    if path is None:
        logger.info("No sign-in view relay: Unix sockets or the path do not fit")
        return None
    # The claim check comes before anything is created or, later, removed.
    session_state._owned(None)
    directory = path.parent
    if directory.is_symlink():
        raise OSError(f"{directory} is a symlink")
    secure_mkdir(directory)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # A path that already exists, socket or not, is not ours: bind refuses
        # it and nothing is removed.
        sock.bind(str(path))
        info = os.lstat(path)
        identity = (info.st_dev, info.st_ino)
        try:
            os.chmod(path, 0o600)
            sock.listen(16)
            sock.setblocking(False)
        except BaseException:
            _remove_if_ours(path, identity)
            raise
    except BaseException:
        sock.close()
        raise
    return _Endpoint(attempt_id, path, identity, sock)


async def _start(endpoint: _Endpoint) -> None:
    async def accepted(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _answer(endpoint, reader, writer)

    try:
        # Nothing suspends before the server is recorded, so a close that runs
        # while it starts also stops the listener.
        if sys.version_info >= (3, 13):
            # Its own cleanup would unlink the path without the claim check.
            endpoint.server = await asyncio.start_unix_server(
                accepted, sock=endpoint.sock, start_serving=False, cleanup_socket=False
            )
        else:
            endpoint.server = await asyncio.start_unix_server(
                accepted, sock=endpoint.sock, start_serving=False
            )
        await endpoint.server.start_serving()
    except Exception:
        logger.warning("The sign-in view relay did not start", exc_info=True)
        # Bound but not served, the socket would hold a sibling's call until
        # its deadline instead of refusing it.
        _close(endpoint)


def _retire_after_tail(endpoint: _Endpoint) -> None:
    if endpoint.closed:
        return
    loop = asyncio.get_running_loop()
    endpoint.timer = loop.call_later(max(_relay.status_tail_s(), 0.0), _close, endpoint)


def _close(endpoint: _Endpoint) -> None:
    if _relay.endpoints.get(endpoint.attempt_id) is endpoint:
        del _relay.endpoints[endpoint.attempt_id]
    try:
        endpoint.close()
    except Exception:
        logger.warning("The sign-in view relay did not close cleanly", exc_info=True)


async def close_all() -> None:
    """Close every endpoint of this process, retained status tails included.

    Every endpoint is closed before the first await; the aborted requests then
    get :data:`CLOSE_TIMEOUT_S` to finish.
    """
    endpoints = list(_relay.endpoints.values())
    for endpoint in endpoints:
        _close(endpoint)
    handlers = [task for endpoint in endpoints for task in endpoint.accepted]
    if handlers:
        await asyncio.wait(handlers, timeout=CLOSE_TIMEOUT_S)


def _remove_if_ours(
    path: Path, identity: tuple[int, int], *, claim: bool = True
) -> None:
    """Unlink *path* only while it is still the socket this process bound."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        logger.info(
            "Left %s in place: it is not the relay socket this process made", path
        )
        return
    if claim:
        session_state._owned(None)
    os.unlink(path)


async def _answer(
    endpoint: _Endpoint, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one request with the local operation, exactly once."""
    task = asyncio.current_task()
    if task is not None:
        endpoint.accepted[task] = writer
    try:
        request = await asyncio.wait_for(read_message(reader), CONNECT_TIMEOUT_S)
        operation, arguments = _request(request)
        handler = _relay.handler
        if handler is None:
            raise RelayProtocolError("No view operations are installed")
        try:
            envelope = await handler(operation, arguments)
        except Exception:
            # Never the arguments: they carry the grant and what the user typed.
            logger.warning("A relayed %s failed", operation, exc_info=False)
            envelope = Envelope(True, "The sign-in view's request failed.")
        await _write_reply(writer, envelope)
    except (EOFError, RelayProtocolError, ConnectionError, TimeoutError) as exc:
        logger.debug("A relay request ended without an answer: %s", type(exc).__name__)
    finally:
        if task is not None:
            endpoint.accepted.pop(task, None)
        await _close_stream(writer, CLOSE_TIMEOUT_S)


async def _write_reply(writer: asyncio.StreamWriter, envelope: Envelope) -> None:
    await write_message(writer, envelope.to_wire())


def _request(content: Mapping[str, object]) -> tuple[Operation, Mapping[str, object]]:
    name, arguments = content.get("tool"), content.get("arguments")
    operation = _OPERATIONS.get(name) if isinstance(name, str) else None
    if (
        set(content) != {"tool", "arguments"}
        or operation is None
        or not isinstance(arguments, dict)
    ):
        raise RelayProtocolError("A relay request names no view operation")
    return operation, arguments


Delivery = Literal["answered", "unavailable", "lost"]


@dataclass(frozen=True, slots=True)
class Forwarded:
    """How a forwarded call ended.

    ``unavailable``: nothing was sent, no owner listens. ``lost``: the request
    went out and no answer came back, so the owner may have acted on it.
    """

    delivery: Delivery
    envelope: Envelope | None = None


async def forward(
    operation: Operation,
    arguments: Mapping[str, object],
    attempt_id: str,
    *,
    deadline_s: float,
) -> Forwarded:
    """Send one view call to the owner of *attempt_id* and return its answer."""
    path = endpoint_path(attempt_id)
    if path is None:
        return Forwarded("unavailable")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)),
            min(CONNECT_TIMEOUT_S, deadline_s),
        )
    except (OSError, TimeoutError):
        return Forwarded("unavailable")
    try:
        # One deadline for the whole hop: an owner that stops reading holds
        # the send as surely as one that never answers.
        async with asyncio.timeout_at(deadline):
            await write_message(
                writer, {"tool": operation, "arguments": dict(arguments)}
            )
            reply = await read_message(reader)
        return Forwarded("answered", Envelope.from_wire(reply))
    except (OSError, TimeoutError, EOFError, RelayProtocolError):
        return Forwarded("lost")
    finally:
        remaining = max(deadline - loop.time(), 0.0)
        await _close_stream(writer, min(CLOSE_TIMEOUT_S, remaining))
