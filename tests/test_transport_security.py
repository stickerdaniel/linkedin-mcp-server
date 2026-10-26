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
_HOST_ORIGIN_PROTECTION = True

# Every case runs against both, because the guard can behave differently
# depending on which address the connection landed on, and it once did. The
# "auto" setting validated only a loopback connection, so a server bound to
# 0.0.0.0 and reached over its LAN address checked nothing, which is precisely
# the exposed case. A suite that only ever connected to 127.0.0.1 could not see
# that. 192.0.2.1 is the reserved documentation range, so it is never a real
# host on the machine running these tests.
_LOOPBACK_URL = "http://127.0.0.1:8000"
_EXPOSED_URL = "http://192.0.2.1:8000"


@pytest.fixture(params=[_LOOPBACK_URL, _EXPOSED_URL], ids=["loopback", "exposed"])
def base_url(request: pytest.FixtureRequest) -> str:
    """The address the server is reached at, loopback or not."""
    return str(request.param)


@pytest.fixture
def post(base_url: str, monkeypatch: pytest.MonkeyPatch):
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
        # base_url sets the ASGI scope's server address, which is what decides
        # whether the connection looks local, not the Host header.
        # raise_server_exceptions=False so a rejection arrives as its status
        # rather than an exception, which is what a real client would see.
        with TestClient(
            app, base_url=base_url, raise_server_exceptions=False
        ) as client:
            return client.post(
                f"{base_url}/mcp",
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

    @pytest.mark.parametrize(
        ("label", "host"),
        [
            ("a subdomain of a trusted name", "localhost.attacker.example"),
            ("a trusted name as a prefix", "127.0.0.1.attacker.example"),
        ],
    )
    def test_a_host_that_merely_contains_a_trusted_name_is_refused(
        self, post, label: str, host: str
    ) -> None:
        """The Host must be a trusted name, not merely contain one.

        Both of these are domains an attacker can register and point anywhere,
        and a substring check would wave them through.
        """
        assert post({"Host": host}) == 421

    @pytest.mark.parametrize(
        "spoof",
        [{"X-Forwarded-Host": "127.0.0.1"}, {"Forwarded": "host=127.0.0.1"}],
        ids=["x-forwarded-host", "forwarded"],
    )
    def test_a_forwarded_header_cannot_vouch_for_an_attacker_host(
        self, post, spoof: dict[str, str]
    ) -> None:
        """Proxy headers are attacker-supplied here; only the real Host counts.

        Nothing trustworthy sits in front of this server by default, so a guard
        that believed these would accept any request that claimed to have come
        through a proxy.
        """
        assert post({"Host": "attacker.example", **spoof}) == 421

    def test_a_null_origin_is_refused(self, post) -> None:
        """What a sandboxed iframe or a redirected form sends."""
        assert post({"Origin": "null"}) == 403


class TestLegitimateClientsStillWork:
    """A guard that breaks the documented flows would simply be turned off."""

    def test_a_client_without_an_origin_is_served(self, post) -> None:
        """Every non-browser client: curl, the MCP inspector, an SDK."""
        assert post({}) == 200

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1:8000",
            "localhost:8000",
            "[::1]:8000",  # IPv6 literal, bracketed as a URL carries it
            "LOCALHOST:8000",  # case is not part of a hostname
        ],
    )
    def test_loopback_hosts_are_always_served(self, post, host: str) -> None:
        """This is the documented Docker flow, and it needs both parameters.

        A published container port is reached at ``localhost``, but inside the
        container the connection lands on the container's own address, which
        is a non-loopback scope. So the case that ships is a localhost Host
        arriving on a non-loopback server, and only running this against both
        scopes covers it.
        """
        assert post({"Host": host}) == 200

    def test_the_docker_flow_survives_a_matching_origin(self, post) -> None:
        """A browser client at the published port sends an Origin too."""
        assert (
            post({"Host": "localhost:8000", "Origin": "http://localhost:8000"}) == 200
        )

    def test_a_hostname_the_server_does_not_know_is_refused(self, post) -> None:
        """The documented cost of strict validation, recorded deliberately.

        Reaching an exposed server by its machine name rather than its address
        gets 421, because the guard trusts the address it is bound to and
        nothing else. Every documented flow uses localhost or an address, so
        this affects setups that are not described here, and it fails loudly
        with a status that names the problem rather than quietly working until
        someone points a domain at the same address.
        """
        assert post({"Host": "my-laptop.local:8000"}) == 421

    def test_the_servers_own_address_is_served(self, post, base_url: str) -> None:
        """However the server is reached, its own address is a valid Host.

        This is what keeps a deliberately exposed server usable: clients arrive
        under the address it is bound to, and strict validation accepts it
        without anything having to be configured.
        """
        host = base_url.removeprefix("http://")
        assert post({"Host": host}) == 200
        assert post({"Host": host, "Origin": base_url}) == 200
