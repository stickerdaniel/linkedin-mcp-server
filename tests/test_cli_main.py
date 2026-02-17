"""Tests for CLI startup behavior and transport selection."""

import importlib.metadata
from typing import Literal
from unittest.mock import MagicMock

import pytest

import linkedin_mcp_server.cli_main as cli_main
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.exceptions import CredentialsNotFoundError


def _make_config(
    *,
    is_interactive: bool,
    transport: Literal["stdio", "streamable-http"],
    transport_explicitly_set: bool,
) -> AppConfig:
    config = AppConfig()
    config.is_interactive = is_interactive
    config.server.transport = transport
    config.server.transport_explicitly_set = transport_explicitly_set
    return config


def _patch_main_dependencies(
    monkeypatch: pytest.MonkeyPatch, config: AppConfig
) -> None:
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.ensure_authentication_ready", lambda: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.set_headless", lambda _x: None)


def test_main_non_interactive_stdio_has_no_human_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    mcp = MagicMock()
    monkeypatch.setattr("linkedin_mcp_server.cli_main.create_mcp_server", lambda: mcp)

    cli_main.main()

    mcp.run.assert_called_once_with(transport="stdio")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_interactive_prompts_when_transport_not_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=True, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    choose_transport = MagicMock(return_value="streamable-http")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.choose_transport_interactive", choose_transport
    )
    mcp = MagicMock()
    monkeypatch.setattr("linkedin_mcp_server.cli_main.create_mcp_server", lambda: mcp)

    cli_main.main()

    choose_transport.assert_called_once_with()
    captured = capsys.readouterr()
    assert "Server ready! Choose transport mode:" in captured.out
    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host=config.server.host,
        port=config.server.port,
        path=config.server.path,
    )


def test_main_explicit_transport_skips_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=True, transport="stdio", transport_explicitly_set=True
    )
    _patch_main_dependencies(monkeypatch, config)
    choose_transport = MagicMock(return_value="streamable-http")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.choose_transport_interactive", choose_transport
    )
    mcp = MagicMock()
    monkeypatch.setattr("linkedin_mcp_server.cli_main.create_mcp_server", lambda: mcp)

    cli_main.main()

    choose_transport.assert_not_called()
    captured = capsys.readouterr()
    assert "Server ready! Choose transport mode:" not in captured.out
    mcp.run.assert_called_once_with(transport="stdio")


def test_main_streamable_http_passes_host_port_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=False,
        transport="streamable-http",
        transport_explicitly_set=True,
    )
    config.server.host = "0.0.0.0"
    config.server.port = 8123
    config.server.path = "/custom-mcp"
    _patch_main_dependencies(monkeypatch, config)
    mcp = MagicMock()
    monkeypatch.setattr("linkedin_mcp_server.cli_main.create_mcp_server", lambda: mcp)

    cli_main.main()

    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host="0.0.0.0",
        port=8123,
        path="/custom-mcp",
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_get_version_prefers_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_version(package_name: str) -> str:
        calls.append(package_name)
        if package_name == "linkedin-scraper-mcp":
            return "4.2.0"
        raise importlib.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)

    assert cli_main.get_version() == "4.2.0"
    assert calls == ["linkedin-scraper-mcp"]


def test_main_non_interactive_auth_failure_has_no_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.ensure_authentication_ready",
        lambda: (_ for _ in ()).throw(CredentialsNotFoundError("missing profile")),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
