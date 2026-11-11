"""The shared key a streamable-http server demands, driven through the real app."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from linkedin_mcp_server.config.loaders import load_from_env
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.server import create_mcp_server

_KEY = "k" * 32
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


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch):
    import linkedin_mcp_server.server as server_module

    monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
    monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
    monkeypatch.setattr(
        server_module, "start_background_browser_setup_if_needed", AsyncMock()
    )
    monkeypatch.setattr(server_module, "watch_for_handoff_requests", AsyncMock())
    monkeypatch.setattr(server_module, "close_browser", AsyncMock())

    def _post(auth_token: str | None, headers: dict[str, str]) -> int:
        app = create_mcp_server(auth_token=auth_token).http_app(
            path="/mcp", host_origin_protection=True
        )
        with TestClient(
            app, base_url="http://127.0.0.1:8000", raise_server_exceptions=False
        ) as client:
            return client.post(
                "http://127.0.0.1:8000/mcp",
                json=_INITIALIZE,
                headers={**_ACCEPT, **headers},
            ).status_code

    return _post


class TestSharedKey:
    def test_a_request_without_the_key_is_refused(self, post) -> None:
        assert post(_KEY, {}) == 401

    def test_a_wrong_key_is_refused(self, post) -> None:
        assert post(_KEY, {"Authorization": "Bearer wrong"}) == 401

    def test_the_right_key_is_served(self, post) -> None:
        assert post(_KEY, {"Authorization": f"Bearer {_KEY}"}) == 200

    def test_without_a_configured_key_nothing_is_demanded(self, post) -> None:
        assert post(None, {}) == 200


class TestSharedKeyConfig:
    def test_the_key_is_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", f" {_KEY} ")

        assert load_from_env(AppConfig()).server.auth_token == _KEY

    def test_a_blank_key_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", "  ")

        assert load_from_env(AppConfig()).server.auth_token is None

    def test_the_key_stays_out_of_the_logged_config(self) -> None:
        config = AppConfig()
        config.server.auth_token = _KEY

        assert _KEY not in repr(config)


def test_health_answers_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_mcp_server(auth_token=_KEY).http_app(path="/mcp")

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health").status_code == 200
