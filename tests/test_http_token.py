"""What an HTTP server started from the command line answers about its token.

Every case starts ``cli_main.main`` with real arguments and a real environment,
and drives the ASGI app that ``mcp.run`` would have served, so the token is
followed from where a user sets it to the request a client sends. Only
Uvicorn's ``serve`` is replaced, by a stub that keeps the app instead of
listening on a port.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
from starlette.testclient import TestClient

import linkedin_mcp_server.cli_main as cli_main
import linkedin_mcp_server.server as server_module
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.server_role import ServerRole

#: Searched for in every log line, repr and error message. Long enough to pass
#: the length check, and unlike anything else the server prints.
_SENTINEL = "SENTINEL-http-token-7f3a9c2e5b1d4086"

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
_HTTP = ["--transport", "streamable-http", "--port", "8000", "--path", "/mcp"]


@pytest.fixture(autouse=True)
def offline_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Everything around the HTTP app that would touch this machine."""
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.config.loaders.is_interactive_environment",
        lambda: False,
    )
    monkeypatch.setattr(cli_main, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(cli_main, "configure_browser_environment", lambda: None)
    monkeypatch.setattr(cli_main, "ensure_profile_claim", lambda *_a, **_k: None)
    monkeypatch.setattr(cli_main, "teardown_trace_logging", lambda **_kwargs: None)
    monkeypatch.setattr(server_module, "initialize_bootstrap", MagicMock())
    monkeypatch.setattr(server_module, "get_runtime_policy", MagicMock())
    monkeypatch.setattr(
        server_module, "start_background_browser_setup_if_needed", AsyncMock()
    )
    monkeypatch.setattr(server_module, "watch_for_handoff_requests", AsyncMock())
    monkeypatch.setattr(server_module, "close_browser", AsyncMock())
    monkeypatch.setattr(
        server_module, "report_retained_browser_revisions_if_ready", MagicMock()
    )


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch):
    """Run ``main(argv)`` and return the ASGI app it would have served."""

    def _start(argv: list[str]) -> Any:
        served: list[Any] = []

        async def keep_the_app(self: uvicorn.Server, sockets: Any = None) -> None:
            served.append(self.config.app)

        monkeypatch.setattr(uvicorn.Server, "serve", keep_the_app)
        cli_main.main(argv)
        (app,) = served
        return app

    return _start


def _post(app: Any, headers: dict[str, str], host: str = "127.0.0.1") -> Any:
    base_url = f"http://{host}:8000"
    with TestClient(app, base_url=base_url, raise_server_exceptions=False) as client:
        return client.post(
            f"{base_url}/mcp", json=_INITIALIZE, headers={**_ACCEPT, **headers}
        )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _private(path: Path, content: str | bytes) -> Path:
    """Write *content* to *path* as a token file belongs: readable by its owner only."""
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    return _private(tmp_path / "http-token", _SENTINEL + "\n")


class TestATokenIsRequired:
    @pytest.fixture(
        params=["environment", "file"],
        ids=["LINKEDIN_MCP_HTTP_TOKEN", "--http-token-file"],
    )
    def app(
        self,
        request: pytest.FixtureRequest,
        started,
        monkeypatch: pytest.MonkeyPatch,
        token_file: Path,
    ) -> Any:
        argv = [*_HTTP, "--host", "0.0.0.0"]
        if request.param == "environment":
            monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", _SENTINEL)
        else:
            argv += ["--http-token-file", str(token_file)]
        return started(argv)

    def test_a_request_without_a_token_is_refused(self, app) -> None:
        response = _post(app, {})

        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")

    def test_a_wrong_token_is_refused(self, app) -> None:
        response = _post(app, _bearer("x" * len(_SENTINEL)))

        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")

    def test_the_right_token_initializes(self, app) -> None:
        response = _post(app, _bearer(_SENTINEL))

        assert response.status_code == 200
        assert "serverInfo" in response.text

    def test_host_checking_still_applies_with_the_right_token(self, app) -> None:
        response = _post(
            app,
            {
                **_bearer(_SENTINEL),
                "Host": "attacker.example",
                "Origin": "http://attacker.example",
            },
        )

        assert response.status_code == 421

    def test_origin_checking_still_applies_with_the_right_token(self, app) -> None:
        response = _post(
            app, {**_bearer(_SENTINEL), "Origin": "https://attacker.example"}
        )

        assert response.status_code == 403


class TestStartup:
    def test_a_reachable_bind_without_a_token_does_not_start(
        self, started, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exited:
            started([*_HTTP, "--host", "0.0.0.0"])

        assert exited.value.code == 1
        error = capsys.readouterr().err
        assert "LINKEDIN_MCP_HTTP_TOKEN" in error
        assert "--host 127.0.0.1" in error

    def test_the_opt_out_starts_without_a_token_and_warns(
        self,
        started,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP", "true")

        with caplog.at_level(logging.WARNING):
            app = started([*_HTTP, "--host", "0.0.0.0"])

        assert "no authentication" in caplog.text
        assert _post(app, {}).status_code == 200

    def test_loopback_without_a_token_serves_as_before(
        self, started, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            app = started([*_HTTP, "--host", "127.0.0.1"])

        assert _post(app, {}).status_code == 200
        assert "no authentication" not in caplog.text

    def test_a_valid_file_replaces_a_malformed_variable(
        self, started, monkeypatch: pytest.MonkeyPatch, token_file: Path
    ) -> None:
        """Only the token that applies is judged, so the file can repair it."""
        monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", "short")

        app = started(
            [*_HTTP, "--host", "0.0.0.0", "--http-token-file", str(token_file)]
        )

        assert _post(app, _bearer(_SENTINEL)).status_code == 200
        assert _post(app, _bearer("short")).status_code == 401

    @pytest.mark.parametrize("token", [_SENTINEL, "short"], ids=["valid", "malformed"])
    def test_stdio_ignores_the_token(
        self, monkeypatch: pytest.MonkeyPatch, token: str
    ) -> None:
        """A stdio server has no listener, so it neither judges nor uses one."""
        monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", token)
        built: list[dict[str, Any]] = []

        def recording(**kwargs: Any) -> MagicMock:
            built.append(kwargs)
            return MagicMock()

        monkeypatch.setattr(cli_main, "create_mcp_server", recording)
        cli_main.main(["--transport", "stdio"])

        assert built[0].get("http_token") is None


class TestTheTokenStaysSecret:
    def test_it_is_absent_from_the_debug_log_and_the_config_repr(
        self,
        started,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from linkedin_mcp_server.config import get_config

        monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", _SENTINEL)

        with caplog.at_level(logging.DEBUG):
            started([*_HTTP, "--host", "0.0.0.0", "--log-level", "DEBUG"])

        assert "Server configuration" in caplog.text
        assert _SENTINEL not in caplog.text
        assert _SENTINEL not in repr(get_config())
        # The verifier holds a digest, so its own repr cannot carry it either.
        assert _SENTINEL not in repr(vars(server_module._StaticTokenAuth(_SENTINEL)))

    @pytest.mark.parametrize(
        "bad",
        [
            _SENTINEL[:31],  # too short
            _SENTINEL[:20] + " " + _SENTINEL[20:],  # a space
        ],
        ids=["short", "space"],
    )
    def test_a_refused_variable_is_not_quoted(
        self,
        started,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        bad: str,
    ) -> None:
        monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", bad)

        with pytest.raises(SystemExit):
            started([*_HTTP, "--host", "0.0.0.0"])

        error = capsys.readouterr().err
        assert "LINKEDIN_MCP_HTTP_TOKEN" in error
        assert _SENTINEL[:20] not in error

    def test_a_refused_file_is_not_quoted(
        self, started, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _private(tmp_path / "short-token", _SENTINEL[:31])

        with pytest.raises(SystemExit):
            started([*_HTTP, "--host", "0.0.0.0", "--http-token-file", str(path)])

        error = capsys.readouterr().err
        assert "--http-token-file" in error
        assert _SENTINEL[:20] not in error


class TestTheTokenFile:
    """One trailing newline is forgiven; everything else is the token."""

    @staticmethod
    def _load(path: Path) -> str | None:
        from linkedin_mcp_server.config.loaders import load_config

        argv = ["--transport", "streamable-http", "--http-token-file", str(path)]
        return load_config(argv).server.http_token

    @pytest.mark.parametrize("ending", ["", "\n", "\r\n"], ids=repr)
    def test_one_line_ending_is_dropped(self, tmp_path: Path, ending: str) -> None:
        path = _private(tmp_path / "token", _SENTINEL + ending)

        assert self._load(path) == _SENTINEL

    @pytest.mark.parametrize(
        "content",
        ["", "\n", "   \n", _SENTINEL + "\n\n", "\n" + _SENTINEL],
        ids=["empty", "newline", "whitespace", "two-newlines", "leading-newline"],
    )
    def test_anything_else_is_refused(self, tmp_path: Path, content: str) -> None:
        from linkedin_mcp_server.config.schema import ConfigurationError

        path = _private(tmp_path / "token", content)

        with pytest.raises(ConfigurationError, match="--http-token-file"):
            self._load(path)

    def test_a_directory_is_refused(self, tmp_path: Path) -> None:
        from linkedin_mcp_server.config.schema import ConfigurationError

        with pytest.raises(ConfigurationError, match="--http-token-file"):
            self._load(tmp_path)

    @pytest.mark.parametrize("step", ["fstat", "read"])
    def test_a_failed_read_is_a_refusal_naming_the_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
    ) -> None:
        """An I/O error after the open still says which setting failed."""
        from linkedin_mcp_server.config.schema import ConfigurationError

        path = _private(tmp_path / "token", _SENTINEL)

        def failing(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError(5, "Input/output error")

        if step == "fstat":
            monkeypatch.setattr(os, "fstat", failing)
        else:
            opened = os.fdopen

            def fdopen(*args: Any, **kwargs: Any) -> Any:
                file = opened(*args, **kwargs)
                monkeypatch.setattr(file, "read", failing)
                return file

            monkeypatch.setattr(os, "fdopen", fdopen)

        with pytest.raises(ConfigurationError, match="--http-token-file") as refused:
            self._load(path)
        assert "Input/output error" in str(refused.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs only")
    def test_a_fifo_is_refused_without_waiting(self, tmp_path: Path) -> None:
        from linkedin_mcp_server.config.schema import ConfigurationError

        fifo = tmp_path / "token"
        os.mkfifo(fifo, 0o600)

        with pytest.raises(ConfigurationError, match="not a regular file"):
            self._load(fifo)

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no mode bits")
    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
    def test_a_file_others_may_access_is_refused(
        self, tmp_path: Path, mode: int
    ) -> None:
        from linkedin_mcp_server.config.schema import ConfigurationError

        path = _private(tmp_path / "token", _SENTINEL)
        os.chmod(path, mode)

        with pytest.raises(ConfigurationError, match="chmod 600") as refused:
            self._load(path)
        assert _SENTINEL not in str(refused.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no mode bits")
    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_a_file_private_to_its_owner_is_read(
        self, tmp_path: Path, mode: int
    ) -> None:
        path = _private(tmp_path / "token", _SENTINEL)
        os.chmod(path, mode)

        assert self._load(path) == _SENTINEL

    def test_the_file_wins_over_the_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LINKEDIN_MCP_HTTP_TOKEN", "e" * 32)
        path = _private(tmp_path / "token", _SENTINEL)

        assert self._load(path) == _SENTINEL


class TestTheOwnerTokenIsSeparate:
    """The operator's token and a daemon owner's token never stand in for each other."""

    _OWNER_TOKEN = "owner-token-" + "o" * 32

    def test_an_owner_refuses_the_http_client_token(self) -> None:
        with pytest.raises(ValueError, match="Direct"):
            create_mcp_server(
                role=ServerRole.OWNER,
                auth_token=self._OWNER_TOKEN,
                http_token=_SENTINEL,
            )

    def test_a_direct_server_refuses_the_owner_token(self) -> None:
        with pytest.raises(ValueError, match="daemon owner"):
            create_mcp_server(auth_token=self._OWNER_TOKEN, http_token=_SENTINEL)

    def test_the_owner_answers_only_its_own_token(self) -> None:
        app = create_mcp_server(
            role=ServerRole.OWNER, auth_token=self._OWNER_TOKEN
        ).http_app(path="/mcp", host_origin_protection=True)

        assert _post(app, _bearer(_SENTINEL)).status_code == 401
        assert _post(app, _bearer(self._OWNER_TOKEN)).status_code == 200

    def test_a_direct_server_answers_only_the_http_client_token(self) -> None:
        app = create_mcp_server(http_token=_SENTINEL).http_app(
            path="/mcp", host_origin_protection=True
        )

        assert _post(app, _bearer(self._OWNER_TOKEN)).status_code == 401
        assert _post(app, _bearer(_SENTINEL)).status_code == 200
