"""A catalog and call endpoint on 127.0.0.1 that answers as a test tells it.

Real sockets and real HTTP, so the server's request code runs unchanged: what
the stub records is what left the process. Usage::

    with CatalogStub() as stub:
        stub.answer = stub.json(200, {"tools": [...]})
        ...  # point LINKEDIN_MCP_REMOTE_TOOLS_URL at stub.url
        stub.requests  # every request seen, in order

``answer`` handles every request; ``call_answer``, when set, handles the
POSTs to ``<url>/call`` instead. Every answer carries a signature by
:data:`SIGNING_KEY` unless it says otherwise; a test admits that key by
putting :data:`PUBLIC_KEY` into ``remote_catalog.CATALOG_KEYS``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import dotenv.main
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: Every variable that decides whether, where and how the catalog is asked.
GATE_KEYS = (
    "LINKEDIN_MCP_REMOTE_TOOLS_URL",
    "LINKEDIN_MCP_REMOTE_TOOLS",
    "LINKEDIN_MCP_CHECK_FOR_UPDATES",
    "CI",
    "DAEMON_ENABLED",
    "IMPORT_FROM_BROWSER",
    "LINKEDIN_MCP_RUNTIME",
    "LC_ALL",
    "LC_MESSAGES",
    "LANG",
)


@contextlib.contextmanager
def gate_environment(
    monkeypatch: pytest.MonkeyPatch, dotenv_path: Path
) -> Generator[None]:
    """No gate variable set, and the ``.env`` lookup finding *dotenv_path*.

    The real lookup walks up from the package directory and would read
    whatever ``.env`` the checkout holds. The whole environment is restored
    afterwards, because ``load_dotenv`` writes into ``os.environ`` behind
    monkeypatch's back.
    """
    saved = dict(os.environ)
    for key in GATE_KEYS:
        os.environ.pop(key, None)
    monkeypatch.setattr(dotenv.main, "find_dotenv", lambda *_a, **_k: str(dotenv_path))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


type Answer = Callable[[BaseHTTPRequestHandler], None]

#: The catalog wire format, written out rather than imported so that a change
#: on one side alone fails here.
SIGNATURE_HEADER = "X-Catalog-Signature"
SIGNED_PREFIX = b"linkedin-mcp-server catalog v1\n"

SIGNING_KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY = SIGNING_KEY.public_key().public_bytes_raw()


def signature(
    body: bytes,
    *,
    key: Ed25519PrivateKey = SIGNING_KEY,
    prefix: bytes = SIGNED_PREFIX,
) -> str:
    """The header value the remote end sends for *body*: unpadded base64url."""
    raw = key.sign(prefix + body)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class Seen:
    """One request as it arrived; header names are lower-cased."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


class CatalogStub:
    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self.answer: Answer = self.json(200, {"instructions": "", "tools": []})
        self.call_answer: Answer | None = None
        #: When a dripping answer found the client gone, by `time.monotonic()`.
        self.disconnected_at: float | None = None
        #: Set once a slow answer has sent its last byte.
        self.finished = threading.Event()
        self._stop = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {key.lower(): value for key, value in self.headers.items()}
                stub.requests.append(Seen(self.command, self.path, headers, body))
                answer = stub.answer
                if stub.call_answer is not None and self.path.endswith("/call"):
                    answer = stub.call_answer
                try:
                    answer(self)
                except (BrokenPipeError, ConnectionResetError):
                    stub.disconnected_at = time.monotonic()

            do_GET = do_POST = _handle

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # A short poll, because shutdown() waits for the loop to notice.
        self._thread = threading.Thread(
            target=self._server.serve_forever, args=(0.05,), daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/oss/tools"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()

    # Answers --------------------------------------------------------------

    @staticmethod
    def raw(
        status: int,
        body: bytes,
        headers: Iterable[tuple[str, str]] = (),
        *,
        signed: bool = True,
    ) -> Answer:
        """*body* as sent, after *headers* in order and, if *signed*, its signature."""
        sent = [*headers]
        if signed:
            sent.append((SIGNATURE_HEADER, signature(body)))

        def answer(handler: BaseHTTPRequestHandler) -> None:
            handler.send_response(status)
            for key, value in sent:
                handler.send_header(key, value)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)

        return answer

    @classmethod
    def json(cls, status: int, document: object) -> Answer:
        return cls.raw(status, json.dumps(document).encode())

    @classmethod
    def redirect(cls, location: str) -> Answer:
        return cls.raw(302, b"", [("Location", location)])

    def drip(
        self,
        document: object | None = None,
        *,
        every: float,
        status: int = 200,
        headers_first: bool = True,
        size: int = 1,
    ) -> Answer:
        """Send *size* bytes per *every* seconds, waiting before each write.

        With *document*, a complete answer whose body arrives in
        ``ceil(len(body) / size)`` writes, and :attr:`finished` is set after
        its last one. Without, an answer that never ends: the headers come
        first unless *headers_first* is false, in which case even the status
        line drips.
        """

        def answer(handler: BaseHTTPRequestHandler) -> None:
            if document is None:
                if headers_first:
                    handler.send_response(status)
                    handler.send_header("Content-Type", "application/json")
                    handler.end_headers()
                    handler.wfile.flush()
                    stream = b'{"content": [' + b" " * 10_000_000
                else:
                    stream = b"HTTP/1.1 200 OK" + b" " * 10_000_000
            else:
                stream = json.dumps(document).encode()
                handler.send_response(status)
                handler.send_header(SIGNATURE_HEADER, signature(stream))
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(stream)))
                handler.end_headers()
                handler.wfile.flush()
            for start in range(0, len(stream), size):
                if self._stop.wait(every):
                    return
                handler.wfile.write(stream[start : start + size])
                handler.wfile.flush()
            self.finished.set()

        return answer

    def calls(self) -> list[Seen]:
        return [seen for seen in self.requests if seen.path.endswith("/call")]
