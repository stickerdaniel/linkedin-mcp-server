"""What the HTTP transport actually answers, as opposed to how it is wired.

These drive the real ASGI app rather than asserting on arguments passed to a
mock. That distinction is the reason this file exists: an earlier version of the
guard passed every wiring assertion while still serving a DNS-rebinding request,
because the tests checked which keyword arguments were forwarded and never asked
what the resulting server would reply.

The attack: a site the user merely visits resolves its own domain to this
server's address, so the user's own browser sends the request from inside the
network. A firewall cannot see it, and neither can a bind address. What gives it
away is the ``Host`` header, which carries the attacker's domain rather than a
name this server answers to.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from linkedin_mcp_server.server import create_mcp_server

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}
_ACCEPT = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# What cli_main passes for a streamable-http server. Spelled out rather than
# imported from it, so weakening the guard there has to be done here too and
# cannot pass unnoticed.
_HOST_ORIGIN_PROTECTION = "auto"


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch):
    """POST an initialize request through the real app, returning the status."""
    # The lifespan starts browser setup and a handoff poller, neither of which
    # this file is about; stubbing them keeps the request path real while the
    # server startup stays offline.
    import linkedin_mcp_server.server as server_module

    monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
    monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
    monkeypatch.setattr(
        server_module, "start_background_browser_setup_if_needed", AsyncMock()
    )
    monkeypatch.setattr(server_module, "watch_for_handoff_requests", AsyncMock())
    monkeypatch.setattr(server_module, "close_browser", AsyncMock())

    def _post(headers: dict[str, str]) -> int:
        app = create_mcp_server().http_app(
            path="/mcp", host_origin_protection=_HOST_ORIGIN_PROTECTION
        )
        # raise_server_exceptions=False so a rejection arrives as its status
        # rather than an exception, which is what a real client would see.
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post(
                "http://127.0.0.1:8000/mcp",
                json=_INITIALIZE,
                headers={**_ACCEPT, **headers},
            ).status_code

    return _post


class TestDnsRebinding:
    def test_an_attacker_domain_is_refused(self, post) -> None:
        """The real attack: Host and Origin both name the attacker's site.

        They agree with each other, so origin validation alone has nothing to
        object to. Only the Host check notices that this server was never known
        by that name.
        """
        assert (
            post(
                {
                    "Host": "attacker.example",
                    "Origin": "http://attacker.example",
                }
            )
            == 421
        )

    def test_a_cross_origin_request_is_refused(self, post) -> None:
        """The simpler case, where the Origin does not match the Host."""
        assert post({"Origin": "https://attacker.example"}) == 403

    def test_an_attacker_host_is_refused_even_without_an_origin(self, post) -> None:
        assert post({"Host": "attacker.example"}) == 421


class TestLegitimateClientsStillWork:
    """A guard that breaks the documented flows would simply be turned off."""

    def test_a_client_without_an_origin_is_served(self, post) -> None:
        """Every non-browser client: curl, the MCP inspector, an SDK."""
        assert post({}) == 200

    @pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000"])
    def test_loopback_hosts_are_served(self, post, host: str) -> None:
        """Including the documented Docker flow, which publishes a port and is
        reached at localhost even though the container binds 0.0.0.0."""
        assert post({"Host": host}) == 200

    def test_a_loopback_origin_is_served(self, post) -> None:
        assert post({"Origin": "http://localhost:8000"}) == 200
