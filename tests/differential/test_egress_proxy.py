"""The synthetic origin's proxy refuses everything but a routed HTTPS tunnel.

The browser row can only show what the browser happened to send, and it sends
HTTPS. These drive the proxy directly, so a proxy that starts forwarding plain
HTTP, or tunnelling a routed name on another port, fails here on every run
rather than going unnoticed until a browser tries it.
"""

from __future__ import annotations

import http.client
import socket
from collections.abc import Iterator

import pytest

from differential.synthetic_origin import EgressProxy


@pytest.fixture
def upstream() -> Iterator[socket.socket]:
    """A loopback listener standing in for the origin. It never accepts, so a
    connection the proxy opens to it waits in the backlog, where it is found."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.setblocking(False)
    try:
        yield listener
    finally:
        listener.close()


@pytest.fixture
def proxy(upstream: socket.socket) -> Iterator[EgressProxy]:
    port = upstream.getsockname()[1]
    egress = EgressProxy({"www.linkedin.com": port, "static.licdn.com": port})
    egress.start()
    try:
        yield egress
    finally:
        egress.stop()


def _send(proxy: EgressProxy, method: str, target: str) -> int:
    host, port = proxy.server_address[:2]
    connection = http.client.HTTPConnection(str(host), int(port), timeout=10)
    try:
        connection.request(method, target)
        return connection.getresponse().status
    finally:
        connection.close()


def _reached(upstream: socket.socket) -> bool:
    try:
        accepted, _ = upstream.accept()
    except BlockingIOError:
        return False
    accepted.close()
    return True


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("GET", "http://www.linkedin.com/feed/"),
        ("GET", "http://www.linkedin.com:443/feed/"),
        ("POST", "http://static.licdn.com/"),
        ("CONNECT", "www.linkedin.com:80"),
        ("CONNECT", "elsewhere.invalid:443"),
    ],
)
def test_anything_but_a_routed_https_tunnel_is_refused(
    proxy: EgressProxy, upstream: socket.socket, method: str, target: str
):
    assert _send(proxy, method, target) == 403
    assert not _reached(upstream), f"{method} {target} opened a connection"
    assert not proxy.forwarded(), proxy.decisions
    assert [(d.method, d.target) for d in proxy.refused()] == [(method, target)]


def test_a_routed_https_tunnel_reaches_its_origin(
    proxy: EgressProxy, upstream: socket.socket
):
    """The control: without it, a proxy that refused everything would pass."""
    assert _send(proxy, "CONNECT", "www.linkedin.com:443") == 200
    assert _reached(upstream)
    assert [(d.host, d.port) for d in proxy.forwarded()] == [("www.linkedin.com", 443)]
