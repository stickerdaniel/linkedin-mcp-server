"""A loopback relay that loses one marker response on the owner hop: the
process-free feasibility control behind H-R16's response-loss lane.

A native claim that a frontend survives a lost auth marker needs exactly that
response gone and nothing else. The owner hop is loopback HTTP/1.1, so the
relay works on whole exchanges: it reads each request whole and forwards it,
reads each response whole before it forwards a byte of it, and the first
response whose body carries the marker key (``daemon_auth.MARKER_KEY``) is
never forwarded at all: the client's connection is closed instead, with the
upstream's answer read in full and recorded as arrived. Every other exchange
reaches the client byte for byte.

**What it records**, per exchange: the method, the path without its query,
the status, the body's length, whether the marker was in it, and whether it
was forwarded. Never a header value, so never the bearer token the hop
carries, and never a body.

**Its boundary.** A body is read by its ``Content-Length``, as chunks when it
is chunked (the marker found in the chunks' data, wherever a chunk boundary
falls), or, with neither, until the upstream closes. A response that never
ends, such as the server's own event stream on ``GET``, is not an exchange
this relay can judge: its connection ends at the relay's I/O bound and is
recorded as an error, never as forwarded.

**Why it is not wired natively** (``auth_repair.RESPONSE_LOSS_OPEN``): the
frontend reaches its owner at the address the owner's own descriptor
publishes, so putting this relay between them means editing the product's
daemon state, an intervention no seam offers and the plan stops at.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from linkedin_mcp_server.daemon_auth import MARKER_KEY

#: How long one read or write on either side may block: the hop's own
#: requests are answered well inside it.
RELAY_IO_SECONDS = 10.0
#: The largest request or response head the relay reads.
_HEAD_LIMIT = 64 * 1024
_CRLF = b"\r\n"


class _Reader:
    """Buffered reads of one socket: a head, an exact count, a line, or the
    rest until the peer closes."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def _fill(self) -> bool:
        data = self.sock.recv(65536)
        if not data:
            return False
        self.buffer += data
        return True

    def head(self) -> bytes | None:
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > _HEAD_LIMIT:
                raise OSError("a head beyond the relay's limit")
            if not self._fill():
                if self.buffer:
                    raise OSError("the peer closed inside a head")
                return None
        end = self.buffer.index(b"\r\n\r\n") + 4
        found, self.buffer = self.buffer[:end], self.buffer[end:]
        return found

    def exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            if not self._fill():
                raise OSError("the peer closed inside a body")
        found, self.buffer = self.buffer[:count], self.buffer[count:]
        return found

    def line(self) -> bytes:
        while _CRLF not in self.buffer:
            if not self._fill():
                raise OSError("the peer closed inside a chunk")
        end = self.buffer.index(_CRLF) + 2
        found, self.buffer = self.buffer[:end], self.buffer[end:]
        return found

    def rest(self) -> bytes:
        while self._fill():
            pass
        found, self.buffer = self.buffer, b""
        return found


def _headers(head: bytes) -> tuple[str, dict[str, str]]:
    """The start line and the headers by lower-cased name."""
    lines = head.decode("latin-1").split("\r\n")
    found: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            found[name.strip().lower()] = value.strip()
    return lines[0], found


def _body(
    reader: _Reader, headers: dict[str, str], *, until_close: bool
) -> tuple[bytes, bytes]:
    """A body as it was sent, and its data with any chunking taken off."""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raw, data = b"", b""
        while True:
            line = reader.line()
            raw += line
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                # Trailers, if any, to the empty line.
                while True:
                    trailer = reader.line()
                    raw += trailer
                    if trailer == _CRLF:
                        return raw, data
            chunk = reader.exact(size + 2)
            raw += chunk
            data += chunk[:size]
    if "content-length" in headers:
        body = reader.exact(int(headers["content-length"]))
        return body, body
    if until_close:
        body = reader.rest()
        return body, body
    return b"", b""


class MarkerDropRelay:
    """Relay loopback HTTP/1.1 to *upstream*, dropping the first *drops*
    responses whose body carries *marker*."""

    def __init__(
        self,
        upstream: tuple[str, int],
        *,
        marker: str = MARKER_KEY,
        drops: int = 1,
    ) -> None:
        self.upstream = upstream
        self.marker = marker.encode()
        self._drops = drops
        self._lock = threading.Lock()
        self._exchanges: list[dict[str, Any]] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._threads: list[threading.Thread] = []
        self._stopped = threading.Event()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    @property
    def exchanges(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(exchange) for exchange in self._exchanges]

    def start(self) -> MarkerDropRelay:
        thread = threading.Thread(target=self._accept, daemon=True, name="relay")
        thread.start()
        self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass
        for thread in list(self._threads):
            thread.join(RELAY_IO_SECONDS + 1.0)

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            thread = threading.Thread(
                target=self._serve, args=(client,), daemon=True, name="relay hop"
            )
            thread.start()
            self._threads.append(thread)

    def _record(self, **fields: Any) -> None:
        with self._lock:
            self._exchanges.append({"monotonic_ns": time.monotonic_ns(), **fields})

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        exchange: dict[str, Any] = {}
        try:
            client.settimeout(RELAY_IO_SECONDS)
            upstream = socket.create_connection(self.upstream, RELAY_IO_SECONDS)
            from_client, from_upstream = _Reader(client), _Reader(upstream)
            while not self._stopped.is_set():
                head = from_client.head()
                if head is None:
                    return
                line, headers = _headers(head)
                method, _, target = line.partition(" ")
                exchange = {
                    "method": method,
                    "path": target.split(" ", 1)[0].split("?", 1)[0],
                }
                raw, _ = _body(from_client, headers, until_close=False)
                upstream.sendall(head + raw)
                answer = from_upstream.head()
                if answer is None:
                    self._record(**exchange, error="the upstream closed")
                    return
                status_line, answer_headers = _headers(answer)
                status = int(status_line.split(" ", 2)[1])
                bodiless = method == "HEAD" or status in (204, 304) or status < 200
                raw, data = (
                    (b"", b"")
                    if bodiless
                    else _body(from_upstream, answer_headers, until_close=True)
                )
                marked = self.marker in data
                with self._lock:
                    drop = marked and self._drops > 0
                    if drop:
                        self._drops -= 1
                self._record(
                    **exchange,
                    status=status,
                    length=len(data),
                    marked=marked,
                    forwarded=not drop,
                )
                exchange = {}
                if drop:
                    # Read in full, recorded as arrived, and never a byte of
                    # it sent on: the client's connection simply ends.
                    return
                client.sendall(answer + raw)
                if "close" in (
                    headers.get("connection", "").lower(),
                    answer_headers.get("connection", "").lower(),
                ):
                    return
        except (OSError, ValueError, IndexError) as exc:
            if exchange:
                self._record(**exchange, error=type(exc).__name__)
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
