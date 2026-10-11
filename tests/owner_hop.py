"""In-process owners, reached over the frontend's real HTTP hop without a socket.

The forwarder builds its own HTTP client, MCP session and negotiation for every
operation, and takes only the byte transport from ``forwarder.owner_transport``.
That is the one thing replaced here, so everything above the socket is
production code: the URL comes from whatever attachment the backend holds, the
bearer token and call header are the ones it sets, and a failure is judged from
what the wire showed rather than raised in the shape a test wanted.

An owner is a real ``FastMCP`` server whose HTTP app runs in this event loop,
behind fastmcp's own streaming ASGI bridge. Its lifespan is hosted in a task of
its own, started on first use and ended when the test's loop closes, so the
session manager outlives every hop the way an owner process does.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.utilities.asgi_transport import StreamingASGITransport, run_asgi_lifespan
from starlette.types import ASGIApp, Receive, Scope, Send

#: Names the in-process owner a request is for, between the router and the
#: app it dispatches to. Removed before the owner sees the request.
_OWNER_KEY = b"x-owner-under-test"

Forward = Callable[[], Awaitable[httpx2.Response]]


class Interceptor:
    """An in-process owner whose every request passes through :meth:`handle` first.

    *message* is the JSON-RPC message a POST carried, or ``None`` for any
    other request. ``forward()`` hands the request to the owner and returns
    its response; not calling it keeps the request from the owner entirely.
    """

    def __init__(self, owner: FastMCP) -> None:
        self.owner = owner

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        return await forward()

    async def closed(self, methods: list[str]) -> None:
        """Called as a hop that reached this owner closes its transport.

        *methods* is every JSON-RPC method that hop posted here, in order. An
        exception raised here leaves the hop's HTTP client as it closes.
        """


def method_of(message: dict[str, Any] | None) -> str | None:
    """The JSON-RPC method *message* names, if it is a request or notification."""
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    return method if isinstance(method, str) else None


class _Hosts:
    """The owner apps one test reaches, each started once and kept running."""

    def __init__(self) -> None:
        self.ready: dict[tuple[int, str], asyncio.Future[ASGIApp]] = {}
        # Strong references: an unreferenced task can be collected mid-flight.
        self.tasks: list[asyncio.Task[None]] = []
        self.owners: list[FastMCP] = []

    async def app_for(self, owner: FastMCP, path: str) -> ASGIApp:
        key = (id(owner), path)
        ready = self.ready.get(key)
        if ready is None:
            ready = asyncio.get_running_loop().create_future()
            self.ready[key] = ready
            self.owners.append(owner)
            # A context of its own, as an owner process has: the task asking
            # is a frontend request, with the frontend's bindings set.
            self.tasks.append(
                asyncio.create_task(
                    self._host(owner, path, ready), context=contextvars.Context()
                )
            )
        return await asyncio.shield(ready)

    @staticmethod
    async def _host(owner: FastMCP, path: str, ready: asyncio.Future[ASGIApp]) -> None:
        app = owner.http_app(transport="http", path=path)
        try:
            async with run_asgi_lifespan(app):
                ready.set_result(app)
                await asyncio.Event().wait()
        except BaseException as failed:
            if not ready.done():
                ready.set_exception(
                    failed if isinstance(failed, Exception) else RuntimeError(failed)
                )
            raise


class _Routed(httpx2.AsyncBaseTransport):
    """One hop's transport: each request goes to whatever serves its URL."""

    def __init__(self, owner_at: Callable[[str], Any], hosts: _Hosts) -> None:
        self.owner_at = owner_at
        self.hosts = hosts
        self.apps: dict[bytes, ASGIApp] = {}
        self.bridge = StreamingASGITransport(self._dispatch)
        #: Every interceptor this hop reached, with the methods it posted there.
        self.intercepted: dict[int, tuple[Interceptor, list[str]]] = {}

    async def __aenter__(self) -> _Routed:
        await self.bridge.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.bridge.__aexit__(exc_type, exc_value, traceback)
        for interceptor, methods in self.intercepted.values():
            await interceptor.closed(methods)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        reached = self.owner_at(url)
        if reached is None:
            raise httpx2.ConnectError(f"nothing is listening on {url}", request=request)
        if isinstance(reached, httpx2.AsyncBaseTransport):
            return await reached.handle_async_request(request)
        if isinstance(reached, Interceptor):
            body = await request.aread()
            message: dict[str, Any] | None = None
            if request.method == "POST":
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
                message = parsed if isinstance(parsed, dict) else None
            _, methods = self.intercepted.setdefault(id(reached), (reached, []))
            method = method_of(message)
            if method is not None:
                methods.append(method)
            owner = reached.owner
            return await reached.handle(
                message, request, lambda: self._to(owner, request)
            )
        return await self._to(reached, request)

    async def _to(self, owner: FastMCP, request: httpx2.Request) -> httpx2.Response:
        app = await self.hosts.app_for(owner, request.url.path)
        key = str(id(app)).encode()
        self.apps[key] = app
        request.headers[_OWNER_KEY.decode()] = key.decode()
        return await self.bridge.handle_async_request(request)

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = list(scope["headers"])
        key = next(value for name, value in headers if name == _OWNER_KEY)
        scope = {**scope, "headers": [h for h in headers if h[0] != _OWNER_KEY]}
        await self.apps[key](scope, receive, send)


def reach_owners_in_process(
    monkeypatch: pytest.MonkeyPatch, owner_at: Callable[[str], Any]
) -> None:
    """Reach in-process owners, but only at the address production chose.

    *owner_at* is asked for every request with its URL, and answers with a
    ``FastMCP`` to reach, an :class:`Interceptor` around one, an
    ``httpx2.AsyncBaseTransport`` to use as it is, or ``None`` for an address
    nobody serves, which refuses the connection the way a departed owner's port
    does.

    The heartbeat preflight goes to the same place. An in-process owner answers
    it the way the owner's own route answers a call it has not registered yet.
    """
    from linkedin_mcp_server.daemon import forwarder
    from linkedin_mcp_server.daemon.discovery import Attachment
    from linkedin_mcp_server.daemon.proxy import FrontendCallHeartbeatMiddleware

    hosts = _Hosts()

    async def beat(attachment: Attachment, _call_id: str) -> httpx2.Response:
        if owner_at(attachment.descriptor.url) is None:
            raise httpx2.ConnectError(
                f"nothing is listening on {attachment.descriptor.url}"
            )
        return httpx2.Response(200, json={"watched": False})

    monkeypatch.setattr(forwarder, "owner_transport", lambda: _Routed(owner_at, hosts))
    monkeypatch.setattr(FrontendCallHeartbeatMiddleware, "_beat", staticmethod(beat))
