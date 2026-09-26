"""Tests for CLI startup behavior and transport selection."""

import importlib.metadata
import json
import logging
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

import linkedin_mcp_server.cli_main as cli_main
from linkedin_mcp_server.config.schema import AppConfig, ConfigurationError
from linkedin_mcp_server.exceptions import ProfileRootRefusedError


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
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.load_config", lambda _argv: config
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr("linkedin_mcp_server.cli_main.set_headless", lambda _x: None)


@pytest.mark.parametrize(
    ("argv", "process_argv", "expected"),
    [
        (None, ["linkedin-mcp-server", "--log-level", "INFO"], ["--log-level", "INFO"]),
        (["--log-level", "DEBUG"], ["host", "--foreign"], ["--log-level", "DEBUG"]),
    ],
)
def test_main_loads_and_installs_cli_config_first(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str] | None,
    process_argv: list[str],
    expected: list[str],
) -> None:
    from linkedin_mcp_server.config import get_config, reset_config
    from linkedin_mcp_server.config import set_config as install_config

    config = _make_config(
        is_interactive=False, transport="stdio", transport_explicitly_set=False
    )
    _patch_main_dependencies(monkeypatch, config)
    reset_config()
    monkeypatch.setattr("sys.argv", process_argv)
    events: list[str] = []

    def load_config(received: object) -> AppConfig:
        assert received == expected
        events.append("load")
        return config

    def set_config(loaded: AppConfig) -> None:
        events.append("set")
        install_config(loaded)

    def preflight(loaded: AppConfig) -> None:
        assert loaded is config
        assert get_config() is config
        events.append("preflight")

    monkeypatch.setattr(cli_main, "load_config", load_config)
    monkeypatch.setattr(cli_main, "set_config", set_config)
    monkeypatch.setattr(cli_main, "_preflight_login_viewer", preflight)
    monkeypatch.setattr(cli_main, "configure_browser_environment", lambda: None)
    monkeypatch.setattr(
        cli_main, "ensure_profile_claim", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(cli_main, "create_mcp_server", lambda **_kwargs: MagicMock())

    if argv is None:
        cli_main.main()
    else:
        cli_main.main(argv)

    assert events == ["load", "set", "preflight"]


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

    cli_main.main([])

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

    cli_main.main([])

    choose_transport.assert_called_once_with()
    captured = capsys.readouterr()
    assert "Server ready! Choose transport mode:" in captured.out
    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host=config.server.host,
        port=config.server.port,
        path=config.server.path,
        host_origin_protection=True,
    )
    assert config.server.transport == "streamable-http"


def test_choosing_http_at_the_prompt_warns_about_an_exposed_bind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Answering the prompt has to update the stored transport, not a local.

    Several checks read it to decide how exposed this process is. Leaving it at
    stdio told the bind-address warning there was no listener to warn about,
    and told the cookie-import gate that a server listening on every interface
    was a private one.
    """
    config = _make_config(
        is_interactive=True, transport="stdio", transport_explicitly_set=False
    )
    config.server.host = "0.0.0.0"
    _patch_main_dependencies(monkeypatch, config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.choose_transport_interactive",
        lambda: "streamable-http",
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: MagicMock()
    )

    with caplog.at_level(logging.WARNING):
        cli_main.main([])

    assert config.server.transport == "streamable-http"
    assert "no authentication" in caplog.text


def test_choosing_stdio_at_the_prompt_leaves_no_listener_recorded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The host is meaningless without a listener, so it must not warn."""
    config = _make_config(
        is_interactive=True, transport="streamable-http", transport_explicitly_set=False
    )
    config.server.host = "0.0.0.0"
    _patch_main_dependencies(monkeypatch, config)
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.choose_transport_interactive", lambda: "stdio"
    )
    mcp = MagicMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

    with caplog.at_level(logging.WARNING):
        cli_main.main([])

    assert config.server.transport == "stdio"
    assert "no authentication" not in caplog.text
    mcp.run.assert_called_once_with(transport="stdio")


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

    cli_main.main([])

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

    cli_main.main([])

    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host="0.0.0.0",
        port=8123,
        path="/custom-mcp",
        host_origin_protection=True,
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_main_streamable_http_enables_host_and_origin_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is not optional and has no configuration switch.

    Two details here are load-bearing rather than incidental. ``True`` instead
    of ``"auto"``: the latter validates only when the connection landed on a
    loopback address, so an exposed server checked nothing over its own LAN
    address. And no ``allowed_hosts``: a wildcard would accept an attacker's
    domain as the Host and reopen the hole from the other side.

    See ``test_transport_security.py`` for what the resulting server answers.
    """
    config = _make_config(
        is_interactive=False,
        transport="streamable-http",
        transport_explicitly_set=True,
    )
    _patch_main_dependencies(monkeypatch, config)
    mcp = MagicMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.create_mcp_server", lambda **_kwargs: mcp
    )

    cli_main.main([])

    assert mcp.run.call_args.kwargs["host_origin_protection"] is True
    assert "allowed_hosts" not in mcp.run.call_args.kwargs


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

    cli_main.main([])

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

    cli_main.main([])

    mcp.run.assert_called_once_with(transport="stdio")
    captured = capsys.readouterr()
    assert captured.out == ""


def test_profile_info_reports_a_downgrade_plainly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    tmp_path,
) -> None:
    """`--status` is the first thing a puzzled user runs, so a refused browser
    must not arrive there as an unexpected internal error.

    Without its own branch it goes through `logger.exception` ("Unexpected
    error checking session") and then prints "Could not validate session ...
    Check logs and browser configuration" over a message that already names
    both versions and the exact fix.
    """
    from linkedin_mcp_server.exceptions import BrowserDowngradeError

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    (tmp_path / "cookies.json").write_text(json.dumps([{"name": "li_at"}]))
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

    async def refuse() -> bool:
        raise BrowserDowngradeError(
            profile_version="151.0.7922.34",
            browser_version="148.0.7778.96",
            browser_product="Google Chrome for Testing",
        )

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile_dir
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_runtime_id", lambda: "macos-arm64-host"
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: AppConfig())
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_or_create_browser", lambda: refuse()
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.close_browser", AsyncMock(return_value=None)
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exit_info:
            cli_main.profile_info_and_exit()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "151.0.7922.34" in captured.out
    assert "148.0.7778.96" in captured.out
    assert "check logs and browser configuration" not in captured.out.lower()
    # And no traceback logged as an unexpected failure either. The two halves
    # of "internal error" are the printed advice and the ERROR-level trace, and
    # each has its own branch to skip.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [
        r.getMessage() for r in caplog.records
    ]


def test_profile_info_reports_bridge_required_for_foreign_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    (tmp_path / "cookies.json").write_text(json.dumps([{"name": "li_at"}]))
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

    with pytest.raises(SystemExit) as exit_info:
        cli_main.profile_info_and_exit()

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert "fresh bridge each startup" in captured.out.lower()
    assert "fresh bridged foreign-runtime session" in captured.out.lower()
    assert "source cookie validity is not verified" in captured.out.lower()


def test_profile_info_reports_committed_derived_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    runtime_profile = (
        tmp_path / "runtime-profiles" / "linux-amd64-container" / "profile"
    )
    runtime_profile.mkdir(parents=True)
    (runtime_profile / "Default").mkdir(parents=True)
    (runtime_profile / "Default" / "Cookies").write_text("placeholder")
    storage_state = (
        tmp_path / "runtime-profiles" / "linux-amd64-container" / "storage-state.json"
    )
    storage_state.write_text("{}")
    (tmp_path / "cookies.json").write_text(json.dumps([{"name": "li_at"}]))
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
    (
        tmp_path / "runtime-profiles" / "linux-amd64-container" / "runtime-state.json"
    ).write_text(
        json.dumps(
            {
                "version": 1,
                "runtime_id": "linux-amd64-container",
                "source_runtime_id": "macos-arm64-host",
                "source_login_generation": "gen-1",
                "created_at": "2026-03-12T17:10:00Z",
                "committed_at": "2026-03-12T17:10:05Z",
                "profile_path": str(runtime_profile),
                "storage_state_path": str(storage_state),
                "commit_method": "checkpoint_restart",
            }
        )
    )

    browser = MagicMock()
    browser.is_authenticated = True

    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile_dir
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_runtime_id", lambda: "linux-amd64-container"
    )
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: AppConfig())
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.configure_logging", lambda **_kwargs: None
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_or_create_browser",
        AsyncMock(return_value=browser),
    )
    monkeypatch.setattr("linkedin_mcp_server.cli_main.close_browser", AsyncMock())

    with pytest.raises(SystemExit) as exit_info:
        cli_main.profile_info_and_exit()

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert "derived (committed, current generation)" in captured.out.lower()
    assert str(storage_state) in captured.out


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

    def fake_ensure() -> None:
        # One browser for every mode now, so the install takes no argument.
        # What still matters here is that it runs once, before dispatch.
        calls.append("ensure")

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
        cli_main.main([])

    assert exit_info.value.code == 0
    # Install gate ran, import dispatched, login never reached.
    assert calls == ["ensure", "import"]


class TestTheProfileRootIsClaimedBeforeAnythingTouchesIt:
    """The ordering is the whole protection.

    Logout deletes the auth root, the browser install downloads into it and the
    daemon spawns an owner that opens it. Any of those running before the claim
    would mean the refusal arrives after the damage.
    """

    def test_it_runs_before_logout(self, monkeypatch, tmp_path):
        config = _make_config(
            is_interactive=False, transport="stdio", transport_explicitly_set=False
        )
        config.browser.user_data_dir = str(tmp_path / "custom" / "profile")
        config.server.logout = True
        _patch_main_dependencies(monkeypatch, config)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
        )

        calls: list[str] = []
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.ensure_profile_claim",
            lambda path, claim_anyway=False: calls.append("claim") or path,
        )

        def fake_logout() -> None:
            calls.append("logout")
            raise SystemExit(0)

        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.clear_profile_and_exit", fake_logout
        )

        with pytest.raises(SystemExit):
            cli_main.main([])

        assert calls == ["claim", "logout"]

    def test_a_refusal_exits_without_a_traceback(self, monkeypatch, tmp_path, capsys):
        config = _make_config(
            is_interactive=True, transport="stdio", transport_explicitly_set=True
        )
        config.browser.user_data_dir = str(tmp_path / "Documents" / "profile")
        config.server.logout = True
        _patch_main_dependencies(monkeypatch, config)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
        )

        def refuse(path, claim_anyway=False):
            raise ProfileRootRefusedError("that directory is not ours")

        monkeypatch.setattr("linkedin_mcp_server.cli_main.ensure_profile_claim", refuse)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.clear_profile_and_exit",
            lambda: pytest.fail("logout must not run after a refusal"),
        )

        with pytest.raises(SystemExit) as exit_info:
            cli_main.main([])

        assert exit_info.value.code == 1
        assert "that directory is not ours" in capsys.readouterr().out

    def test_a_fresh_custom_root_really_claims_through_the_real_startup(
        self, monkeypatch, tmp_path
    ):
        """The real ordering, with the real predicate, and nothing stubbed out.

        Every other test here replaces `ensure_profile_claim`, and the ones in
        `test_profile_claim.py` call it with no startup in front of it. Both
        miss what `main()` does *before* the claim: `configure_logging` creates
        a trace directory in the auth root, because trace capture defaults to
        on_error rather than off. Measured against the real entry point, that
        made a genuinely empty custom root read as occupied and refused every
        first run, telling the user to point at an empty directory.
        """
        from linkedin_mcp_server.profile_claim import claim_path

        target = tmp_path / "custom" / "profile"
        target.parent.mkdir(parents=True)
        config = _make_config(
            is_interactive=False, transport="stdio", transport_explicitly_set=False
        )
        config.browser.user_data_dir = str(target)
        config.server.logout = True
        # Deliberately not `_patch_main_dependencies`: it stubs
        # `configure_logging`, which is the very thing that runs first.
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.load_config", lambda _argv: config
        )
        monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", lambda: config)
        monkeypatch.setattr("linkedin_mcp_server.cli_main.get_version", lambda: "4.0.0")
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.set_headless", lambda _x: None
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.clear_profile_and_exit",
            lambda: (_ for _ in ()).throw(SystemExit(0)),
        )

        with pytest.raises(SystemExit) as exit_info:
            cli_main.main([])

        assert exit_info.value.code == 0, "the claim refused a genuinely empty root"
        assert claim_path(target).exists()

    def test_the_operator_flag_reaches_the_claim(self, monkeypatch, tmp_path):
        config = _make_config(
            is_interactive=False, transport="stdio", transport_explicitly_set=False
        )
        config.browser.user_data_dir = str(tmp_path / "custom" / "profile")
        config.server.claim_profile_root = True
        config.server.logout = True
        _patch_main_dependencies(monkeypatch, config)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
        )

        seen: list[bool] = []
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.ensure_profile_claim",
            lambda path, claim_anyway=False: seen.append(claim_anyway) or path,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.clear_profile_and_exit",
            lambda: (_ for _ in ()).throw(SystemExit(0)),
        )

        with pytest.raises(SystemExit):
            cli_main.main([])

        assert seen == [True]


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


class TestForwardingToASharedOwner:
    """Which server this process builds, and what it does when there is no owner.

    The flag is off by default, so the ordinary case here is that nothing
    happens. What these pin is the two ways the daemon can go wrong quietly: a
    process that elects an owner and then ignores it, and a process that starts
    an owner when there was never any point.
    """

    @pytest.fixture(autouse=True)
    def _local_storage(self, monkeypatch: pytest.MonkeyPatch) -> list:
        # Storage is its own refusal, with its own tests below. Everywhere else
        # in this class it is explicitly local, so the runner's real home can
        # neither refuse a positive case nor stand in for another refusal.
        asked: list = []

        def classify(path):
            from linkedin_mcp_server.storage_class import Classification, StorageClass

            asked.append(path)
            return Classification(StorageClass.LOCAL, "local test filesystem")

        monkeypatch.setattr("linkedin_mcp_server.storage_class.classify", classify)
        return asked

    @staticmethod
    def _outcome(attachment):
        from linkedin_mcp_server.daemon import OwnerLookup, OwnerState
        from linkedin_mcp_server.daemon_election import ElectionOutcome

        state = OwnerState.ATTACHABLE if attachment else OwnerState.ABSENT
        return ElectionOutcome(OwnerLookup(state=state, attachment=attachment))

    def _config(self, *, daemon_enabled: bool, transport="stdio") -> AppConfig:
        config = _make_config(
            is_interactive=False,
            transport=transport,
            transport_explicitly_set=True,
        )
        config.server.daemon_enabled = daemon_enabled
        return config

    def test_no_owner_is_sought_when_the_daemon_is_switched_off(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The default. Electing an owner here would cost every user a detached
        # process for a feature they did not ask for.
        asked = MagicMock()
        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", asked)

        assert cli_main._obtain_shared_owner(self._config(daemon_enabled=False)) is None
        asked.assert_not_called()

    def test_no_owner_is_sought_for_an_http_server(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # An explicit HTTP bind is already one server for many clients, so there
        # is nothing left for a daemon to deduplicate.
        asked = MagicMock()
        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", asked)
        config = self._config(daemon_enabled=True, transport="streamable-http")

        assert cli_main._obtain_shared_owner(config) is None
        asked.assert_not_called()

    def test_no_owner_is_sought_for_a_custom_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Only the bundled browser is shared; CHROME_PATH keeps the Direct server
        # it had before. Nothing about the profile or an owner may be looked up.
        asked = MagicMock()
        looked_up = MagicMock()
        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", asked)
        monkeypatch.setattr("linkedin_mcp_server.cli_main.get_profile_dir", looked_up)
        config = self._config(daemon_enabled=True)
        config.browser.chrome_path = "/opt/custom/chrome"

        assert cli_main._obtain_shared_owner(config) is None
        asked.assert_not_called()
        looked_up.assert_not_called()

    @pytest.mark.parametrize("refused", ["non-local", "synced", "unknown"])
    def test_no_owner_is_sought_on_storage_that_is_not_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        caplog: pytest.LogCaptureFixture,
        refused: str,
    ):
        # Recorded rather than raised: `_obtain_shared_owner` swallows what the
        # election raises, so a raising sentinel would be caught and missed.
        from linkedin_mcp_server.storage_class import Classification, StorageClass

        effects = {
            name: MagicMock(name=name)
            for name in ("obtain_owner", "prepare", "lock", "read", "backend")
        }
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            effects["obtain_owner"],
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_descriptor.prepare_daemon_state",
            effects["prepare"],
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_descriptor.read", effects["read"]
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_lock.DaemonLock.__init__", effects["lock"]
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_proxy.DaemonProxyBackend.__init__",
            effects["backend"],
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.storage_class.classify",
            lambda _path: Classification(StorageClass(refused), "test storage"),
        )
        config = self._config(daemon_enabled=True)
        config.browser.user_data_dir = str(tmp_path / "profile")

        with caplog.at_level(logging.WARNING):
            assert cli_main._obtain_shared_owner(config) is None

        assert len(caplog.records) == 1
        assert f"on {refused} storage" in caplog.records[0].getMessage()
        for effect in effects.values():
            effect.assert_not_called()

    def test_local_storage_reaches_the_election_for_the_same_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, _local_storage: list
    ):
        # The positive control for the refusal above, and the check that the
        # root classified is the root the election is handed.
        from linkedin_mcp_server.session_state import canonical

        profile = tmp_path / "profile"
        elected = MagicMock(return_value=self._outcome(None))
        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", elected)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.get_profile_dir", lambda: profile
        )
        config = self._config(daemon_enabled=True)
        config.browser.user_data_dir = str(profile)

        cli_main._obtain_shared_owner(config)

        elected.assert_called_once()
        auth_root = elected.call_args.args[0]
        # Asked second: the profile itself comes first, then the root above it.
        assert canonical(auth_root) == canonical(_local_storage[1])

    def test_the_elected_owner_is_handed_back_rather_than_discarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # The mutation every test that stubs this helper would miss: before this
        # PR the election ran and its result was thrown away, which looks
        # identical from the outside until nothing forwards.
        attachment = MagicMock(name="attachment")
        # A mock answers every attribute truthily, and a truthy `control_only`
        # is the one attachment the proxy backend refuses to be built around.
        attachment.control_only = False
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_args, **_kwargs: self._outcome(attachment),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
        )

        found = cli_main._obtain_shared_owner(self._config(daemon_enabled=True))

        # Through the backend the proxy layer is built from, which is what now
        # carries it. The claim is unchanged: the election's answer survives.
        assert found is not None
        assert found.attachment is attachment

    def test_a_failed_election_leaves_this_process_driving_its_own_browser(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, caplog
    ):
        # Deliberate, and a real trade: falling back means two clients can hand
        # the profile back and forth per call again, which is the cost #606
        # exists to remove. A client that refused to start would fail where
        # nobody reads the reason, so the warning is what has to carry it.
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_args, **_kwargs: self._outcome(None),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
        )

        with caplog.at_level(logging.WARNING):
            assert (
                cli_main._obtain_shared_owner(self._config(daemon_enabled=True)) is None
            )

        assert "drive its own browser" in caplog.text

    def test_an_election_that_raises_is_never_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        def explode(*_args, **_kwargs):
            raise RuntimeError("the lock is on a filesystem without locking")

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", explode)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
        )

        assert cli_main._obtain_shared_owner(self._config(daemon_enabled=True)) is None

    def test_an_owner_makes_this_process_a_proxy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        from linkedin_mcp_server.server_role import ServerRole

        config = self._config(daemon_enabled=True)
        _patch_main_dependencies(monkeypatch, config)
        attachment = MagicMock(name="attachment")
        # A mock answers every attribute truthily, and a truthy `control_only`
        # is the one attachment the proxy backend refuses to be built around.
        attachment.control_only = False
        # Patched at the election rather than at the helper, so the real helper
        # runs. Stubbing `_obtain_shared_owner` would let it go on discarding the
        # outcome — the bug this PR fixes — while this test still passed.
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon_election.obtain_owner",
            lambda *_args, **_kwargs: self._outcome(attachment),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "profile"
        )
        built = {}
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.create_mcp_server",
            lambda **kwargs: built.update(kwargs) or MagicMock(),
        )

        cli_main.main([])

        assert built["role"] is ServerRole.PROXY
        assert built["proxy_backend"].attachment is attachment
        # The owner's inbound credential must not be reused for the outbound hop.
        assert "auth_token" not in built

    def test_no_owner_leaves_the_historical_server_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        config = self._config(daemon_enabled=False)
        _patch_main_dependencies(monkeypatch, config)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main._obtain_shared_owner", lambda _config: None
        )
        built = {}
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.create_mcp_server",
            lambda **kwargs: built.update(kwargs) or MagicMock(),
        )

        cli_main.main([])

        assert set(built) == {"tool_timeout"}

    def test_an_interactively_chosen_http_transport_elects_no_daemon(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The ordering this depends on is easy to break by accident: the
        # interactive answer is written back into the config, and the election
        # reads that stored value. Moving the election above the prompt, or
        # keeping the answer in a local, would start a detached owner for a
        # server that is already one-for-many — and every other test here would
        # still pass, because they all set the transport explicitly.
        config = self._config(daemon_enabled=True, transport="stdio")
        config.server.transport_explicitly_set = False
        config.is_interactive = True
        _patch_main_dependencies(monkeypatch, config)

        # Recorded rather than raised from inside: the helper wraps the election
        # in `except Exception`, so an assertion thrown there would be swallowed
        # and this test would pass against the very bug it exists for. Found by
        # mutating the ordering and watching it pass.
        called = []

        def record(*_args, **_kwargs):
            called.append(True)
            raise RuntimeError("no owner")

        monkeypatch.setattr("linkedin_mcp_server.daemon_election.obtain_owner", record)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.choose_transport_interactive",
            lambda: "streamable-http",
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.create_mcp_server",
            lambda **_kwargs: MagicMock(),
        )

        cli_main.main([])

        assert called == [], "an HTTP server must not elect a daemon"


class TestConfigurationErrorAtStartup:
    """A bad setting has to name itself, not arrive as a stack trace.

    Under a stdio host the process has no console: stderr is the log file and
    stdout is the protocol. An unhandled ConfigurationError put eleven frames
    of this package into that log and the actual problem on the last line,
    behind a "Server disconnected" the host reports for any early exit.
    """

    def _raise(self, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
        def boom(_argv: object) -> AppConfig:
            raise ConfigurationError(message)

        monkeypatch.setattr("linkedin_mcp_server.cli_main.load_config", boom)

    def test_it_exits_with_the_message_and_no_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._raise(monkeypatch, "proxy_server needs a host and an explicit port")

        with pytest.raises(SystemExit) as exit_info:
            cli_main.main([])

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "proxy_server needs a host and an explicit port" in captured.err
        assert "Traceback" not in captured.err

    def test_the_interactive_transport_choice_gets_the_same_answer(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A setting that only applies to HTTP passes the startup validation and
        # fails the second one, after the user picks a transport. That call
        # sits inside the runtime handler, which would log it as an unexpected
        # error with its traceback.
        config = _make_config(
            is_interactive=True, transport="stdio", transport_explicitly_set=False
        )
        _patch_main_dependencies(monkeypatch, config)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.choose_transport_interactive",
            lambda: "streamable-http",
        )

        def boom() -> None:
            raise ConfigurationError("HTTP_PATH must start with a slash")

        monkeypatch.setattr(config, "validate", boom)

        with pytest.raises(SystemExit) as exit_info:
            cli_main.main([])

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "HTTP_PATH must start with a slash" in captured.err
        assert "Traceback" not in captured.err

    def test_it_leaves_stdout_to_the_protocol(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A stdio client parses stdout as JSON-RPC. A diagnostic there is worse
        # than no diagnostic: it corrupts the stream it is trying to explain.
        self._raise(monkeypatch, "PORT must be an integer")

        with pytest.raises(SystemExit):
            cli_main.main([])

        assert capsys.readouterr().out == ""


class _Owner:
    """A published owner of the test profile, served by the real route.

    The route and its ``CallLiveness`` are the owner's own. Only the socket is
    replaced: the CLI's HTTP client is handed a transport that runs each
    request through the owner's ASGI app, and records it. *answer* replaces the
    app for the rows a same-build owner never gives.
    """

    HOST = "127.0.0.1"

    def __init__(
        self, monkeypatch, home, profile, *, token_seen_by_owner=None, bridged=True
    ):
        import httpx

        from linkedin_mcp_server import daemon_descriptor, daemon_liveness
        from linkedin_mcp_server.daemon_owner import create_owner_server
        from linkedin_mcp_server.session_state import auth_root_dir, get_runtime_id

        self.events: list[tuple] = []
        self.requests: list[tuple[str, str, bytes]] = []
        self.turnover: list[str] = []
        self.answer = None
        self.instance = daemon_descriptor.new_instance_id()
        self.token = daemon_descriptor.new_token()
        self.port = 49152
        profile.mkdir(parents=True, exist_ok=True)
        auth_root = auth_root_dir(profile)
        daemon_descriptor.publish(
            auth_root,
            daemon_descriptor.build(
                instance_id=self.instance,
                package_version="4.20.0",
                runtime_id=get_runtime_id(),
                profile=profile,
                host=self.HOST,
                port=self.port,
                path="/mcp",
                token=self.token,
                # Another configuration than the client's: retirement is about
                # the browser, not about whether this client could use it.
                config=AppConfig(),
                log_path=auth_root / "daemon.log",
            ),
            self.token,
        )
        self.liveness = daemon_liveness.get_liveness()
        self.liveness.serving_as(self.instance)

        def stand_down() -> None:
            # What `run_owner`'s own callback does.
            self.liveness.retire("turnover")
            self.turnover.append("asked")

        self.app = create_owner_server(
            config=AppConfig(),
            token=token_seen_by_owner or self.token,
            host=self.HOST,
            port=self.port,
            stand_down=stand_down,
        ).config.app
        owner = self

        class Bridge(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                body = request.read()
                owner.events.append(("request",))
                owner.requests.append((request.method, request.url.path, body))
                if owner.answer is not None:
                    return owner.answer(request)
                return owner._through_the_app(request, body)

        if bridged:
            monkeypatch.setattr(
                "linkedin_mcp_server.daemon_owner.direct_http_client",
                lambda *, timeout: httpx.Client(
                    transport=Bridge(), trust_env=False, timeout=timeout
                ),
            )

    def _through_the_app(self, request, body):
        import asyncio

        import httpx

        async def forward():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=f"http://{self.HOST}:{self.port}",
                trust_env=False,
            ) as client:
                response = await client.request(
                    request.method,
                    request.url.path,
                    headers={
                        key: value
                        for key, value in request.headers.items()
                        if key.lower() != "host"
                    },
                    content=body,
                )
                return response.status_code, response.headers, response.content

        status, headers, content = asyncio.run(forward())
        return httpx.Response(
            status,
            headers={"content-type": headers.get("content-type", "")},
            content=content,
        )

    def sent_only_the_idle_only_request(self) -> bool:
        from linkedin_mcp_server.daemon_owner import STAND_DOWN_PATH

        return [(m, path, json.loads(body)) for m, path, body in self.requests] == [
            (
                "POST",
                STAND_DOWN_PATH,
                {
                    "only_if_idle": True,
                    "protocol": _protocol(),
                    "instance": self.instance,
                },
            )
        ]


def _protocol() -> int:
    from linkedin_mcp_server.daemon_descriptor import PROTOCOL_VERSION

    return PROTOCOL_VERSION


class TestRetiringASharedBrowser:
    """--logout, --login and --import-from-browser under a shared browser.

    The contract: ask only after the user confirms, retire only an owner with
    nothing in flight or queued, never fall back to the unconditional
    stand-down, never describe a request that may have been sent as unsent,
    and change the profile only once its lease is held.
    """

    @pytest.fixture(autouse=True)
    def _machine(self, monkeypatch, tmp_path, isolate_profile_dir):
        from linkedin_mcp_server import daemon_descriptor
        from linkedin_mcp_server.storage_class import Classification, StorageClass

        # Every daemon file under a home of this test's own, never the real one.
        self.home = tmp_path / "home"
        self.home.mkdir()
        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: self.home)
        monkeypatch.setattr(
            "linkedin_mcp_server.storage_class.classify",
            lambda path: Classification(StorageClass.LOCAL, "local test filesystem"),
        )
        self.profile = isolate_profile_dir
        self.config = AppConfig()
        self.config.is_interactive = True
        self.config.server.daemon_enabled = True
        self.config.browser.user_data_dir = str(self.profile)
        monkeypatch.setattr(cli_main, "get_config", lambda: self.config)
        monkeypatch.setattr(cli_main, "configure_logging", lambda **_kwargs: None)
        monkeypatch.setattr(cli_main, "get_version", lambda: "4.0.0")
        monkeypatch.setattr(cli_main, "set_headless", lambda _x: None)
        monkeypatch.setattr(cli_main, "configure_browser_environment", lambda: None)
        self.events: list[tuple] = []
        self.monkeypatch = monkeypatch

    # -- helpers ----------------------------------------------------------- #

    def _seed_session(self) -> None:
        from linkedin_mcp_server.session_state import source_state_path

        (self.profile / "Default").mkdir(parents=True, exist_ok=True)
        (self.profile / "Default" / "Cookies").write_text("placeholder")
        source_state_path(self.profile).write_text("{}")

    def _session_intact(self) -> bool:
        from linkedin_mcp_server.session_state import source_state_path

        return (self.profile / "Default" / "Cookies").exists() and source_state_path(
            self.profile
        ).exists()

    def _answers(self, *answers, events=None) -> list[str]:
        prompts: list[str] = []
        queue = list(answers)
        log = self.events if events is None else events

        def ask(prompt: str = "") -> str:
            prompts.append(prompt)
            log.append(("prompt", prompt))
            answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            log.append(("answer", answer))
            return answer

        self.monkeypatch.setattr("builtins.input", ask)
        return prompts

    def _owner(self, **kwargs) -> _Owner:
        owner = _Owner(self.monkeypatch, self.home, self.profile, **kwargs)
        self.events = owner.events
        return owner

    def _logout(self) -> object:
        with pytest.raises(SystemExit) as exit_info:
            cli_main.clear_profile_and_exit()
        return exit_info.value.code

    def _login(self) -> tuple[object, MagicMock]:
        creation = MagicMock(return_value=True)
        self.monkeypatch.setattr(cli_main, "run_profile_creation", creation)
        with pytest.raises(SystemExit) as exit_info:
            cli_main.get_profile_and_exit()
        return exit_info.value.code, creation

    def _import(self) -> tuple[object, AsyncMock]:
        self.config.server.import_from_browser = "chrome"
        run = AsyncMock(return_value=True)
        self.monkeypatch.setattr(
            "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
            run,
        )
        with pytest.raises(SystemExit) as exit_info:
            cli_main.import_from_browser_and_exit()
        return exit_info.value.code, run

    @staticmethod
    def _retirement_prompts(prompts: list[str]) -> list[str]:
        return [p for p in prompts if "retire" in p]

    # -- step 0: eligibility ----------------------------------------------- #

    def test_a_process_that_would_not_share_never_looks(self, capsys):
        # Nothing read, nothing created: not even the daemon state directory.
        self.config.server.daemon_enabled = False
        self._seed_session()
        owner = self._owner()
        looked = MagicMock(side_effect=AssertionError("looked for an owner"))
        self.monkeypatch.setattr("linkedin_mcp_server.daemon.look_up_owner", looked)
        entries_before = sorted(p.name for p in self.home.rglob("*"))
        prompts = self._answers("y")

        assert self._logout() == 0

        looked.assert_not_called()
        assert self._retirement_prompts(prompts) == []
        assert owner.requests == []
        assert not self._session_intact()
        assert sorted(p.name for p in self.home.rglob("*")) == entries_before

    # -- step 1: the lookup ------------------------------------------------ #

    @pytest.mark.parametrize("recorded", ["nothing", "another profile", "runtime"])
    def test_no_owner_of_this_profile_leaves_the_command_as_it_was(
        self, recorded, capsys
    ):
        self._seed_session()
        owner = None
        if recorded != "nothing":
            owner = self._owner()
            from linkedin_mcp_server import daemon_descriptor
            from linkedin_mcp_server.session_state import auth_root_dir

            path = daemon_descriptor.descriptor_path(auth_root_dir(self.profile))
            raw = json.loads(path.read_text())
            if recorded == "another profile":
                sibling = self.profile.parent / "sibling"
                sibling.mkdir()
                raw["profile_identity"] = daemon_descriptor.profile_identity(sibling)
            else:
                raw["runtime_id"] = "docker-abc123"
            path.write_text(json.dumps(raw))
        prompts = self._answers("y")

        assert self._logout() == 0

        assert self._retirement_prompts(prompts) == []
        assert owner is None or owner.requests == []
        assert not self._session_intact()
        assert "shared browser" not in capsys.readouterr().out

    @pytest.mark.parametrize("damage", ["corrupt", "token", "raises"])
    def test_an_unreadable_record_is_said_once_and_the_lease_decides(
        self, damage, capsys
    ):
        from linkedin_mcp_server import daemon_descriptor
        from linkedin_mcp_server.session_state import auth_root_dir

        self._seed_session()
        owner = self._owner()
        auth_root = auth_root_dir(self.profile)
        if damage == "corrupt":
            daemon_descriptor.descriptor_path(auth_root).write_text("{not json")
        elif damage == "token":
            daemon_descriptor.token_path(auth_root, owner.instance).write_text("x")
        else:
            self.monkeypatch.setattr(
                "linkedin_mcp_server.daemon.look_up_owner",
                MagicMock(side_effect=OSError("state storage went away")),
            )
        prompts = self._answers("y")

        assert self._logout() == 0

        out = capsys.readouterr().out
        assert out.count("its record could not be read") == 1
        assert self._retirement_prompts(prompts) == []
        assert owner.requests == []
        assert not self._session_intact()

    def test_an_owner_of_another_protocol_is_never_asked(self, capsys):
        from linkedin_mcp_server import daemon_descriptor
        from linkedin_mcp_server.session_state import auth_root_dir

        self._seed_session()
        owner = self._owner()
        path = daemon_descriptor.descriptor_path(auth_root_dir(self.profile))
        raw = json.loads(path.read_text())
        raw["protocol_version"] = _protocol() - 1
        path.write_text(json.dumps(raw))
        prompts = self._answers("y")

        assert self._logout() == 0

        out = capsys.readouterr().out
        assert "speaks another protocol" in out
        # A record is not a process: the line may not claim it is running.
        assert "is running" not in out
        assert self._retirement_prompts(prompts) == []
        assert owner.requests == []

    # -- step 2: the confirmation ------------------------------------------ #

    def test_nothing_is_sent_before_the_user_answers(self):
        self._seed_session()
        owner = self._owner()
        self._answers("y", "y")

        assert self._logout() == 0
        assert owner.sent_only_the_idle_only_request()

        asked = next(
            i
            for i, event in enumerate(self.events)
            if event[0] == "prompt" and "retire" in event[1]
        )
        assert self.events[asked + 1] == ("answer", "y")
        assert ("request",) in self.events
        assert ("request",) not in self.events[: asked + 2]

    @pytest.mark.parametrize(
        "answer",
        ["n", "", KeyboardInterrupt(), EOFError()],
        ids=["no", "enter", "interrupt", "eof"],
    )
    def test_declining_sends_nothing_and_changes_nothing(self, answer, capsys):
        self._seed_session()
        owner = self._owner()
        prompts = self._answers("y", answer)

        assert self._logout() == 0

        assert len(self._retirement_prompts(prompts)) == 1
        assert owner.requests == []
        assert owner.liveness.retiring is False
        assert self._session_intact()
        assert "cancelled" in capsys.readouterr().out.lower()

    def test_logout_still_asks_about_deleting_first(self, capsys):
        # Agreeing to retire a browser is not agreeing to delete a session.
        self._seed_session()
        owner = self._owner()
        prompts = self._answers("n")

        assert self._logout() == 0

        assert self._retirement_prompts(prompts) == []
        assert owner.requests == []
        assert self._session_intact()

    @pytest.mark.parametrize("command", ["logout", "login", "import"])
    def test_without_a_terminal_the_retirement_is_refused(self, command, capsys):
        self.config.is_interactive = False
        self._seed_session()
        owner = self._owner()
        self._answers("y")

        if command == "logout":
            code = self._logout()
            ran = not self._session_intact()
        elif command == "login":
            code, creation = self._login()
            ran = creation.called
        else:
            code, run = self._import()
            ran = run.called

        assert code == 1
        assert not ran
        assert owner.requests == []
        assert "needs an interactive terminal" in capsys.readouterr().out

    @pytest.mark.parametrize("command", ["login", "import"])
    def test_without_an_owner_nothing_is_asked_even_without_a_terminal(self, command):
        self.config.is_interactive = False
        prompts = self._answers()

        if command == "login":
            code, creation = self._login()
            assert creation.called
        else:
            code, run = self._import()
            assert run.await_args is not None
            assert run.await_args.kwargs["profile_wait_seconds"] == 0.0

        assert code == 0
        assert prompts == []

    # -- steps 3 to 5 against the real route ------------------------------- #

    def test_an_idle_owner_retires_and_the_profile_is_cleared_once_held(self):
        from linkedin_mcp_server.profile_lease import ProfileLease, get_profile_lease

        self._seed_session()
        owner = self._owner()
        self._answers("y", "y")
        taken: list[int] = []
        real_try_acquire = ProfileLease.try_acquire

        def counted(lease):
            granted = real_try_acquire(lease)
            if granted:
                taken.append(lease._refs)
            return granted

        self.monkeypatch.setattr(ProfileLease, "try_acquire", counted)

        assert self._logout() == 0

        assert owner.sent_only_the_idle_only_request()
        assert owner.liveness.retiring is True
        assert owner.liveness.retire_reason == "retire"
        assert owner.turnover == ["asked"]
        # One reference, taken once, in the one place the logout takes it.
        assert taken == [1]
        assert not self._session_intact()
        assert not get_profile_lease(self.profile).held

    @pytest.mark.parametrize("busy_with", ["running", "queued"])
    def test_a_busy_owner_is_left_alone_and_nothing_changes(self, busy_with, capsys):
        import os

        self._seed_session()
        owner = self._owner()
        if busy_with == "running":
            owner.liveness.watch("v1." + "a" * 32, MagicMock())
        owner.liveness.call_started()
        self._answers("y", "y")

        assert self._logout() == 1

        out = capsys.readouterr().out
        assert "busy with another client's call" in out
        # The owner's pid is on record (the descriptor was built in this
        # process), and no line about it may name it.
        said = [line for line in out.splitlines() if "shared browser" in line]
        assert said and not any(str(os.getpid()) in line for line in said)
        assert owner.sent_only_the_idle_only_request()
        assert owner.liveness.retiring is False
        assert owner.turnover == []
        assert self._session_intact()

    def test_a_busy_owner_blocks_login_and_import_too(self, capsys):
        import os

        owner = self._owner()
        owner.liveness.call_started()
        self._answers("y", "y")

        login_code, creation = self._login()
        import_code, run = self._import()

        assert (login_code, import_code) == (1, 1)
        assert not creation.called
        assert not run.called
        # Nothing else is printed before the refusal on these two paths.
        assert str(os.getpid()) not in capsys.readouterr().out

    def test_login_starts_after_an_idle_owner_retires(self):
        owner = self._owner()
        self._answers("y")

        code, creation = self._login()

        assert code == 0
        assert owner.sent_only_the_idle_only_request()
        creation.assert_called_once()

    def test_import_waits_for_the_profile_after_an_idle_owner_retires(self):
        from linkedin_mcp_server.config.schema import PROFILE_HANDOVER_WAIT_SECONDS

        owner = self._owner()
        self._answers("y")

        code, run = self._import()

        assert code == 0
        assert owner.sent_only_the_idle_only_request()
        assert run.await_args is not None
        assert run.await_args.kwargs["profile_wait_seconds"] == (
            PROFILE_HANDOVER_WAIT_SECONDS
        )

    def test_credentials_the_owner_rejects(self, capsys):
        self._seed_session()
        owner = self._owner(token_seen_by_owner="a-token-it-was-never-given")
        self._answers("y", "y")

        assert self._logout() == 1

        assert "did not accept this client's credentials" in capsys.readouterr().out
        assert owner.sent_only_the_idle_only_request()
        assert owner.liveness.retiring is False
        assert self._session_intact()

    # -- step 4: every other answer ---------------------------------------- #

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (200, {"standing_down": True}),
            (200, {"standing_down": True, "instance": "@"}),
            (200, {"standing_down": True, "retiring": False, "instance": "@"}),
            (200, {"retiring": True, "instance": "@"}),
            (200, {"standing_down": True, "retiring": True, "instance": "another"}),
            (200, {"standing_down": True, "retiring": True}),
            (200, b"not json"),
            (200, b"[]"),
            (409, {"standing_down": False}),
            (409, {"daemon": "retiring"}),
            (400, {"error": "unsupported stand-down request"}),
            (404, b"Not Found"),
            (405, b"Method Not Allowed"),
            (403, b""),
            (500, b"Internal Server Error"),
            (302, b""),
        ],
        ids=[
            "200 from an owner that ignored the body",
            "200 without retiring",
            "200 not retiring",
            "200 not standing down",
            "200 for another instance",
            "200 naming no instance",
            "200 not json",
            "200 not an object",
            "409 not busy",
            "409 retiring marker",
            "400",
            "404",
            "405",
            "403",
            "500",
            "302",
        ],
    )
    def test_an_answer_this_build_does_not_recognise(self, status, body, capsys):
        import httpx

        self._seed_session()
        owner = self._owner()

        def answer(request):
            # "@" is the instance the request named, so a row that fails only
            # on another field is not also refused for naming another owner.
            if not isinstance(body, dict):
                return httpx.Response(status, content=body)
            named = json.loads(request.content)["instance"]
            return httpx.Response(
                status,
                json={k: named if v == "@" else v for k, v in body.items()},
            )

        owner.answer = answer
        self._answers("y", "y")

        assert self._logout() == 1

        out = capsys.readouterr().out
        assert "does not recognise" in out
        assert "may have begun retiring" in out
        assert owner.sent_only_the_idle_only_request(), "fell back to another request"
        assert self._session_intact()

    @pytest.mark.parametrize(
        "failure",
        [
            "ReadTimeout",
            "RemoteProtocolError",
            "ReadError",
            "WriteError",
            "PoolTimeout",
        ],
    )
    def test_a_lost_answer_is_never_reported_as_unsent(self, failure, capsys):
        import httpx

        self._seed_session()
        owner = self._owner()

        def lost(request):
            raise getattr(httpx, failure)("gone", request=request)

        owner.answer = lost
        self._answers("y", "y")

        assert self._logout() == 1

        out = capsys.readouterr().out
        assert "got no answer" in out
        assert "may be retiring now" in out
        assert "not sent" not in out and "unsent" not in out
        assert owner.sent_only_the_idle_only_request()
        assert self._session_intact()

    def test_an_interrupt_after_sending_says_it_may_be_retiring(self, capsys):
        self._seed_session()
        owner = self._owner()

        def interrupted(request):
            raise KeyboardInterrupt

        owner.answer = interrupted
        self._answers("y", "y")

        assert self._logout() == 130

        assert "may be retiring now" in capsys.readouterr().out
        assert self._session_intact()

    def test_an_owner_that_is_not_listening_leaves_the_command_as_it_was(self, capsys):
        # An owner that exited leaves its record behind, and every command
        # after it would otherwise be refused until something replaced it.
        # Refused to connect proves nothing was sent; the lease still decides.
        import socket

        self._seed_session()
        # The real client, against a real closed port.
        self._owner(bridged=False)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        from linkedin_mcp_server import daemon_descriptor
        from linkedin_mcp_server.session_state import auth_root_dir

        path = daemon_descriptor.descriptor_path(auth_root_dir(self.profile))
        raw = json.loads(path.read_text())
        raw["port"] = closed
        path.write_text(json.dumps(raw))
        self._answers("y", "y")

        assert self._logout() == 0

        assert "is not listening" in capsys.readouterr().out
        assert not self._session_intact()

    # -- step 5: the profile ------------------------------------------------ #

    def _hold_the_profile(self):
        from linkedin_mcp_server.profile_lease import (
            _release_locked_fd,
            acquire_locked_fd,
            get_profile_lease,
        )

        fd = acquire_locked_fd(
            get_profile_lease(self.profile)._lease_path, exclusive=True
        )
        assert fd is not None
        released: list[bool] = []

        def release() -> None:
            if not released:
                released.append(True)
                _release_locked_fd(fd)

        return release

    def test_a_profile_that_does_not_come_free_is_left_untouched(self, capsys):
        # A successor may win the lease between the reply and the wait, and
        # then admit a real call. Timing out safely is an allowed outcome.
        from linkedin_mcp_server.profile_lease import get_profile_lease

        self._seed_session()
        owner = self._owner()
        self.monkeypatch.setattr(cli_main, "PROFILE_HANDOVER_WAIT_SECONDS", 0.3)
        release = self._hold_the_profile()
        self._answers("y", "y")
        try:
            assert self._logout() == 1
        finally:
            release()

        assert "in use by another process" in capsys.readouterr().out
        assert owner.liveness.retiring is True
        assert self._session_intact()
        assert not get_profile_lease(self.profile).held

    def test_a_profile_the_retiring_owner_lets_go_of_is_cleared(self):
        import threading

        self._seed_session()
        self._owner()
        release = self._hold_the_profile()
        timer = threading.Timer(0.3, release)
        timer.start()
        self._answers("y", "y")
        try:
            assert self._logout() == 0
        finally:
            timer.cancel()
            release()

        assert not self._session_intact()

    @pytest.mark.parametrize("command", ["logout", "login", "import"])
    def test_an_interrupt_while_waiting_says_it_may_be_retiring(self, command, capsys):
        self._seed_session()
        self._owner()
        self._answers("y", "y")
        if command == "logout":
            self.monkeypatch.setattr(
                "linkedin_mcp_server.session_state.clear_auth_state",
                MagicMock(side_effect=KeyboardInterrupt),
            )
            code = self._logout()
        elif command == "login":
            self.monkeypatch.setattr(
                cli_main,
                "run_profile_creation",
                MagicMock(side_effect=KeyboardInterrupt),
            )
            with pytest.raises(SystemExit) as exit_info:
                cli_main.get_profile_and_exit()
            code = exit_info.value.code
        else:
            self.config.server.import_from_browser = "chrome"
            self.monkeypatch.setattr(
                "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
                AsyncMock(side_effect=KeyboardInterrupt),
            )
            with pytest.raises(SystemExit) as exit_info:
                cli_main.import_from_browser_and_exit()
            code = exit_info.value.code

        assert code == 130
        assert "may be retiring now" in capsys.readouterr().out

    def test_an_import_whose_profile_stays_busy_is_refused_plainly(self, capsys):
        from linkedin_mcp_server.exceptions import BrowserBusyError

        self._owner()
        self._answers("y")
        self.config.server.import_from_browser = "chrome"
        self.monkeypatch.setattr(
            "linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser",
            AsyncMock(side_effect=BrowserBusyError("Another LinkedIn MCP client")),
        )

        with pytest.raises(SystemExit) as exit_info:
            cli_main.import_from_browser_and_exit()

        assert exit_info.value.code == 1
        assert "Another LinkedIn MCP client" in capsys.readouterr().out
