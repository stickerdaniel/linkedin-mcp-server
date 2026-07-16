"""Tests for CLI startup behavior and transport selection."""

import importlib.metadata
import json
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

import linkedin_mcp_server.cli_main as cli_main
from linkedin_mcp_server.config.schema import AppConfig


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
    monkeypatch.setattr("linkedin_mcp_server.cli_main.set_headless", lambda _x: None)


def test_main_non_interactive_stdio_has_no_human_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    mcp = MagicMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

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
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

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
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

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
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

    cli_main.main()

    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host="0.0.0.0",
        port=8123,
        path="/custom-mcp",
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_passes_configured_tool_timeout_to_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    config.server.tool_timeout_seconds = 42.0
    _patch_main_dependencies(monkeypatch, config)

    captured: dict[str, float] = {}

    def fake_create(**kwargs: float) -> MagicMock:
        captured.update(kwargs)
        mcp = MagicMock()
        return mcp

    monkeypatch.setattr("linkedin_mcp_server.cli_main.create_mcp_server", fake_create)

    cli_main.main()

    assert captured["tool_timeout"] == 42.0


def test_get_version_prefers_installed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_version(package_name: str) -> str:
        calls.append(package_name)
        if package_name == "mcp-server-linkedin":
            return "4.2.0"
        raise importlib.metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)

    assert cli_main.get_version() == "4.2.0"
    assert calls == ["mcp-server-linkedin"]


def test_main_non_interactive_no_auth_still_starts_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    mcp = MagicMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

    cli_main.main()

    mcp.run.assert_called_once_with(transport="stdio")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_profile_info_validates_foreign_runtime_through_fresh_bridge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )
    (tmp_path / "source-state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(tmp_path / "cookies.json"),
            }
        )
    )

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile_dir
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_runtime_id", lambda: "linux-amd64-container"
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: AppConfig())
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    browser = MagicMock(is_authenticated=True)
    get_browser = AsyncMock(return_value=browser)
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_or_create_browser", get_browser
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.close_browser", close)

    with pytest.raises(SystemExit) as exit_info:
        cli_main.profile_info_and_exit()

    assert exit_info.value.code == 0
    get_browser.assert_awaited_once()
    close.assert_awaited_once()
    captured = capsys.readouterr()
    assert "fresh bridge each startup" in captured.out.lower()
    assert "session is valid" in captured.out.lower()


def test_profile_info_reports_expired_after_live_bridge(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )
    (tmp_path / "source-state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(tmp_path / "cookies.json"),
            }
        )
    )
    browser = MagicMock(is_authenticated=False)

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile_dir
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_runtime_id", lambda: "linux-amd64-container"
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: AppConfig())
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_or_create_browser",
        AsyncMock(return_value=browser),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.close_browser", AsyncMock(return_value=True)
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.profile_info_and_exit()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "session expired or invalid" in captured.out.lower()


def _patch_import_handler(monkeypatch, tmp_path, *, is_interactive=False):
    config = AppConfig()
    config.is_interactive = is_interactive
    config.server.import_from_browser = "chrome"
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr("linkedin_mcp_server.cli_main.set_headless", lambda _x: None)
    configured = {"called": False}
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_browser_environment",
        lambda: configured.__setitem__("called", True),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
    )
    return config, configured


def test_import_from_browser_success_exits_zero(monkeypatch, capsys, tmp_path):
    _config, configured = _patch_import_handler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
        AsyncMock(return_value=True),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.import_from_browser_and_exit()

    assert exit_info.value.code == 0
    assert configured["called"] is True
    assert "imported and validated" in capsys.readouterr().out.lower()


def test_import_from_browser_failure_exits_one(monkeypatch, capsys, tmp_path):
    _patch_import_handler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
        AsyncMock(return_value=False),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.import_from_browser_and_exit()

    assert exit_info.value.code == 1
    assert "did not produce a valid session" in capsys.readouterr().out.lower()


def test_import_from_browser_no_session_guidance(monkeypatch, capsys, tmp_path):
    from linkedin_mcp_server.exceptions import NoLinkedInSessionFoundError

    _patch_import_handler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
        AsyncMock(side_effect=NoLinkedInSessionFoundError("none found")),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.import_from_browser_and_exit()

    assert exit_info.value.code == 1
    out = capsys.readouterr().out.lower()
    assert "log into linkedin" in out
    assert "--login" in out


def test_import_from_browser_app_bound_message(monkeypatch, capsys, tmp_path):
    from linkedin_mcp_server.exceptions import CookieDecryptionError

    _patch_import_handler(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
        AsyncMock(side_effect=CookieDecryptionError("app-bound in Brave")),
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.import_from_browser_and_exit()

    assert exit_info.value.code == 1
    assert "could not import session" in capsys.readouterr().out.lower()


def test_main_dispatches_import_before_login(monkeypatch, tmp_path):
    # Driving the dispatch through main() (not the handler directly) proves the
    # wiring: import is gated into ensure_browser_installed and runs before the
    # --login handler.
    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    config.server.import_from_browser = "chrome"
    config.server.login = True  # also set; import must win and exit first
    _patch_main_dependencies(monkeypatch, config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
    )

    calls: list[str] = []

    def fake_ensure(*, full: bool = False) -> None:
        # Both --login and --import-from-browser are set; import dispatches
        # first, so the install requests full chromium for the headed login.
        calls.append(f"ensure(full={full})")

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.ensure_browser_installed", fake_ensure
    )

    def fake_import():
        calls.append("import")
        raise SystemExit(0)

    def fake_login():
        calls.append("login")
        raise SystemExit(0)

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.import_from_browser_and_exit", fake_import
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_profile_and_exit", fake_login)

    with pytest.raises(SystemExit) as exit_info:
        cli_main.main()

    assert exit_info.value.code == 0
    # Browser install gate ran for import, import dispatched, login never reached.
    # --login is also set, so the install requests full chromium.
    assert calls == ["ensure(full=True)", "import"]


def test_clear_profile_and_exit_clears_all_auth_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (tmp_path / "source-state.json").write_text("{}")

    cleared = {}

    def fake_clear(profile):
        cleared["profile"] = profile
        return True

    monkeypatch.setattr("linkedin_mcp_server.cli_main.clear_auth_state", fake_clear)

    with pytest.raises(SystemExit) as exit_info:
        cli_main.clear_profile_and_exit()

    assert exit_info.value.code == 0
    assert cleared["profile"] == profile_dir
    captured = capsys.readouterr()
    assert "authentication state cleared" in captured.out.lower()


def test_clear_profile_and_exit_does_not_skip_pending_only_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config = AppConfig()
    profile_dir = tmp_path / "profile"
    config.browser.user_data_dir = str(profile_dir)
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile_dir
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    pending = tmp_path / ".login-pending-only"
    pending.mkdir()
    (pending / "cookies.json").write_text("private li_at")

    cleared: list[object] = []
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.clear_auth_state",
        lambda profile: cleared.append(profile) or True,
    )

    with pytest.raises(SystemExit) as exit_info:
        cli_main.clear_profile_and_exit()

    assert exit_info.value.code == 0
    assert cleared == [profile_dir]
