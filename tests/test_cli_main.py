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
        cli_main.main()

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
        cli_main.main()

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

    cli_main.main()

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
        cli_main.main()

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
            cli_main.main()

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
            cli_main.main()

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
            cli_main.main()

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
            cli_main.main()

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

    def test_the_elected_owner_is_handed_back_rather_than_discarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        # The mutation every test that stubs this helper would miss: before this
        # PR the election ran and its result was thrown away, which looks
        # identical from the outside until nothing forwards.
        attachment = MagicMock(name="attachment")
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

        cli_main.main()

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

        cli_main.main()

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

        cli_main.main()

        assert called == [], "an HTTP server must not elect a daemon"


class TestConfigurationErrorAtStartup:
    """A bad setting has to name itself, not arrive as a stack trace.

    Under a stdio host the process has no console: stderr is the log file and
    stdout is the protocol. An unhandled ConfigurationError put eleven frames
    of this package into that log and the actual problem on the last line,
    behind a "Server disconnected" the host reports for any early exit.
    """

    def _raise(self, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
        def boom() -> AppConfig:
            raise ConfigurationError(message)

        monkeypatch.setattr("linkedin_mcp_server.cli_main.get_config", boom)

    def test_it_exits_with_the_message_and_no_traceback(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._raise(monkeypatch, "proxy_server needs a host and an explicit port")

        with pytest.raises(SystemExit) as exit_info:
            cli_main.main()

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
            cli_main.main()

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
            cli_main.main()

        assert capsys.readouterr().out == ""
