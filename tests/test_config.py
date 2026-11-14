import ast
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import pytest

from linkedin_mcp_server.config.loaders import (
    IGNORED_OPTIONS,
    SETTINGS,
    EnvironmentKeys,
    Option,
    Setting,
    Switch,
    SwitchPair,
    build_parser,
    load_from_args,
    load_from_env,
)
from linkedin_mcp_server.config.schema import (
    AppConfig,
    BROWSER_HANDOFF_MARGIN_SECONDS,
    BrowserConfig,
    ConfigurationError,
    DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS,
    DEFAULT_BROWSER_MIN_HOLD_SECONDS,
    DEFAULT_BROWSER_WAIT_SECONDS,
    MAX_BROWSER_WAIT_SECONDS,
    MAX_LOGIN_INLINE_WAIT_SECONDS,
    ServerConfig,
    is_loopback_host,
)


class TestBrowserConfig:
    def test_defaults(self):
        config = BrowserConfig()
        assert config.headless is True
        assert config.default_timeout == 5000
        assert config.user_data_dir == "~/.linkedin-mcp/profile"
        assert config.login_timeout_seconds == 1800.0
        assert config.login_inline_wait_seconds == 25.0
        assert config.auto_import_from_browser is None

    def test_validate_passes(self):
        BrowserConfig().validate()  # No error

    def test_validate_negative_timeout(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(default_timeout=-1).validate()

    def test_validate_negative_slow_mo(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(slow_mo=-1).validate()

    def test_validate_login_timeout_zero_allowed(self):
        BrowserConfig(login_timeout_seconds=0).validate()  # No error

    def test_validate_login_inline_wait_zero_allowed(self):
        BrowserConfig(login_inline_wait_seconds=0).validate()  # No error

    @pytest.mark.parametrize(
        "bad_value", [-1.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_validate_invalid_login_timeout(self, bad_value):
        with pytest.raises(ConfigurationError):
            BrowserConfig(login_timeout_seconds=bad_value).validate()

    @pytest.mark.parametrize(
        "bad_value", [-1.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_validate_invalid_login_inline_wait(self, bad_value):
        with pytest.raises(ConfigurationError):
            BrowserConfig(login_inline_wait_seconds=bad_value).validate()

    def test_validate_clamps_login_inline_wait(self):
        config = BrowserConfig(login_inline_wait_seconds=120)
        config.validate()  # Clamps, does not raise
        assert config.login_inline_wait_seconds == MAX_LOGIN_INLINE_WAIT_SECONDS


class TestProfileSharingConfig:
    """The wait/hold/idle values that govern cross-process browser handoff.

    The clamping here is not cosmetic. A hold window that outlasts the waiter's
    budget produces spurious busy errors, and a wait budget above the ceiling
    eats into the client's own request timeout.
    """

    def test_defaults(self):
        config = BrowserConfig()
        assert config.browser_wait_seconds == DEFAULT_BROWSER_WAIT_SECONDS
        assert config.browser_min_hold_seconds == DEFAULT_BROWSER_MIN_HOLD_SECONDS
        assert (
            config.browser_idle_timeout_seconds == DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS
        )

    def test_defaults_leave_room_for_a_handover(self):
        """The shipped defaults must satisfy their own clamp, unchanged."""
        config = BrowserConfig()
        config.validate()

        assert config.browser_wait_seconds == DEFAULT_BROWSER_WAIT_SECONDS
        assert config.browser_min_hold_seconds == DEFAULT_BROWSER_MIN_HOLD_SECONDS
        assert (
            config.browser_min_hold_seconds
            <= config.browser_wait_seconds - BROWSER_HANDOFF_MARGIN_SECONDS
        )

    def test_zero_is_allowed(self):
        """0 is a meaningful sentinel for all three, not a missing value.

        Wait 0 reports busy at once, hold 0 hands over after every call, idle 0
        keeps the browser until the process exits.
        """
        BrowserConfig(
            browser_wait_seconds=0,
            browser_min_hold_seconds=0,
            browser_idle_timeout_seconds=0,
        ).validate()  # No error

    @pytest.mark.parametrize(
        "bad_value", [-1.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_rejects_a_negative_or_non_finite_wait(self, bad_value):
        with pytest.raises(ConfigurationError, match="browser_wait_seconds"):
            BrowserConfig(browser_wait_seconds=bad_value).validate()

    @pytest.mark.parametrize(
        "bad_value", [-1.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_rejects_a_negative_or_non_finite_hold(self, bad_value):
        with pytest.raises(ConfigurationError, match="browser_min_hold_seconds"):
            BrowserConfig(browser_min_hold_seconds=bad_value).validate()

    @pytest.mark.parametrize(
        "bad_value", [-1.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_rejects_a_negative_or_non_finite_idle_timeout(self, bad_value):
        with pytest.raises(ConfigurationError, match="browser_idle_timeout_seconds"):
            BrowserConfig(browser_idle_timeout_seconds=bad_value).validate()

    def test_clamps_wait_to_the_ceiling(self):
        config = BrowserConfig(browser_wait_seconds=120, browser_min_hold_seconds=0)
        config.validate()  # Clamps, does not raise
        assert config.browser_wait_seconds == MAX_BROWSER_WAIT_SECONDS

    def test_clamps_hold_below_the_wait_budget(self):
        """A hold window as long as the wait budget would time every waiter out.

        The owner notices on a one-second poll and then has to tear Chromium
        down, so the window has to end a margin *before* the waiter's deadline,
        not at it.
        """
        config = BrowserConfig(browser_wait_seconds=10, browser_min_hold_seconds=10)
        config.validate()

        assert config.browser_min_hold_seconds == 10 - BROWSER_HANDOFF_MARGIN_SECONDS

    def test_clamping_the_wait_also_reins_in_the_hold(self):
        """The hold clamp reads the wait *after* it was itself clamped.

        Ordering matters: against the raw 600s the hold would look fine, and a
        180s hold would survive a 45s budget.
        """
        config = BrowserConfig(browser_wait_seconds=600, browser_min_hold_seconds=180)
        config.validate()

        assert config.browser_wait_seconds == MAX_BROWSER_WAIT_SECONDS
        assert (
            config.browser_min_hold_seconds
            == MAX_BROWSER_WAIT_SECONDS - BROWSER_HANDOFF_MARGIN_SECONDS
        )

    def test_hold_never_clamps_below_zero(self):
        """A wait budget under the margin must not produce a negative window."""
        config = BrowserConfig(browser_wait_seconds=1, browser_min_hold_seconds=30)
        config.validate()

        assert config.browser_min_hold_seconds == 0.0

    def test_zero_wait_disables_the_hold_window(self):
        """Reporting busy immediately leaves nothing for a hold window to buy."""
        config = BrowserConfig(browser_wait_seconds=0, browser_min_hold_seconds=25)
        config.validate()

        assert config.browser_min_hold_seconds == 0.0

    def test_hold_inside_the_budget_is_left_alone(self):
        config = BrowserConfig(browser_wait_seconds=45, browser_min_hold_seconds=5)
        config.validate()

        assert config.browser_min_hold_seconds == 5

    def test_a_long_idle_timeout_is_not_clamped(self):
        """The idle timer is a backstop, not part of the request path."""
        config = BrowserConfig(browser_idle_timeout_seconds=86_400)
        config.validate()

        assert config.browser_idle_timeout_seconds == 86_400


class TestServerConfig:
    def test_defaults(self):
        config = ServerConfig()
        assert config.transport == "stdio"
        assert config.port == 8000
        assert config.tool_timeout_seconds == 180.0

    def test_validate_passes(self):
        ServerConfig().validate()  # No error

    @pytest.mark.parametrize(
        "bad_value", [-1.0, 0.0, float("nan"), float("inf"), float("-inf")]
    )
    def test_validate_invalid_tool_timeout(self, bad_value):
        with pytest.raises(ConfigurationError):
            ServerConfig(tool_timeout_seconds=bad_value).validate()


class TestAppConfig:
    def test_validate_invalid_port(self):
        config = AppConfig()
        config.server.port = 99999
        with pytest.raises(ConfigurationError):
            config.validate()


class TestExposedBindWarning:
    """Beyond loopback, HTTP needs a client token or an explicit opt-out.

    Judged by the bind address, which once warned only for a literal 0.0.0.0 or
    ::, so naming a LAN address outright, the likeliest accident, passed in
    silence.
    """

    @staticmethod
    def _validate_with_host(host, caplog, **server):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.host = host
        for name, value in server.items():
            setattr(config.server, name, value)
        with caplog.at_level(
            logging.WARNING, logger="linkedin_mcp_server.config.schema"
        ):
            config.validate()
        return caplog.text

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_binds_need_no_token(self, host, caplog):
        assert self._validate_with_host(host, caplog) == ""

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "::",
            "192.168.1.5",
            "10.0.0.7",
            "example.internal",
        ],
    )
    def test_reachable_binds_without_a_token_are_refused(self, host, caplog):
        with pytest.raises(ConfigurationError) as refused:
            self._validate_with_host(host, caplog)

        message = str(refused.value)
        assert host in message
        assert "LINKEDIN_MCP_HTTP_TOKEN" in message
        assert "--host 127.0.0.1" in message
        assert "LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP=true" in message
        assert "unsafe" in message

    def test_a_reachable_bind_with_a_token_starts_silently(self, caplog):
        text = self._validate_with_host("0.0.0.0", caplog, http_token="t" * 32)

        assert text == ""

    def test_the_opt_out_starts_and_warns(self, caplog):
        text = self._validate_with_host(
            "0.0.0.0", caplog, allow_unauthenticated_http=True
        )

        assert "0.0.0.0" in text
        assert "no authentication" in text

    def test_stdio_never_judges_the_host(self, caplog):
        """The host field is meaningless without an HTTP listener."""
        config = AppConfig()
        config.server.transport = "stdio"
        config.server.host = "0.0.0.0"
        with caplog.at_level(logging.WARNING):
            config.validate()

        assert "no authentication" not in caplog.text

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            # Reachable only from this machine, however it was spelled.
            ("127.0.0.1", True),
            ("::1", True),
            ("localhost", True),
            ("LOCALHOST", True),  # case is not part of a hostname
            ("  localhost  ", True),
            ("localhost.", True),  # the root dot is the same name
            ("127.0.0.2", True),  # the whole 127/8 range is loopback
            ("127.255.255.254", True),
            ("::ffff:127.0.0.1", True),  # IPv4-mapped loopback
            ("[::1]", True),  # bracketed, as it appears in a URL
            # Reachable from elsewhere, or not decidable without DNS.
            ("0.0.0.0", False),
            ("::", False),
            ("192.168.1.5", False),
            ("10.0.0.7", False),
            ("example.internal", False),
            ("localhost.evil.example", False),  # a name DNS points anywhere
            ("", False),
            ("   ", False),
        ],
    )
    def test_is_loopback_host_fails_closed(self, host, expected):
        """Anything not positively loopback counts as reachable."""
        assert is_loopback_host(host) is expected


class TestConfigSingleton:
    def test_get_config_ignores_host_process_arguments(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["host-process", "--foreign-option"])
        from linkedin_mcp_server.config import get_config, reset_config

        reset_config()
        config = get_config()

        assert config.server.transport == "stdio"

    def test_get_config_returns_same_instance(self):
        from linkedin_mcp_server.config import get_config

        assert get_config() is get_config()

    def test_reset_config_clears_singleton(self):
        from linkedin_mcp_server.config import get_config, reset_config

        first = get_config()
        reset_config()
        second = get_config()
        assert first is not second


class TestUserAgentRefusal:
    """Both ways of setting a user agent must stop the server, not be ignored.

    Someone who set this wanted the browser to present itself differently.
    Starting anyway would leave them believing it still applies while the
    browser reports something else entirely, so the failure is loud and says
    what to remove.
    """

    def test_env_user_agent_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("USER_AGENT", "CustomAgent/1.0")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="USER_AGENT"):
            load_from_env(AppConfig())

    def test_empty_env_user_agent_is_not_a_setting(self, monkeypatch):
        """An empty value is nobody's intent, so it must not block startup."""
        monkeypatch.setenv("USER_AGENT", "")
        from linkedin_mcp_server.config.loaders import load_from_env

        load_from_env(AppConfig())

    def test_cli_user_agent_refuses_to_start(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(ConfigurationError, match="--user-agent"):
            load_from_args(AppConfig(), ["--user-agent", "CustomAgent/1.0"])


class TestRetiredChromiumFlags:
    """Once documented, now ignored: a saved command line must still start."""

    @pytest.mark.parametrize(
        "flag", ["--eager-full-chromium", "--no-eager-full-chromium"]
    )
    def test_a_retired_flag_is_accepted_and_changes_nothing(self, flag):
        from linkedin_mcp_server.config.loaders import load_from_args

        assert load_from_args(AppConfig(), [flag]) == load_from_args(AppConfig(), [])


class TestLoaders:
    def test_load_from_env_headless_false(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "false")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is False

    def test_load_from_env_headless_true(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "true")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is True

    def test_load_from_env_headless_true_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "  TrUe ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is True

    def test_load_from_env_headless_false_with_off_alias(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "off")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is False

    def test_load_from_env_headless_false_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "  FaLsE ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is False

    def test_load_from_env_headless_true_with_on_alias(self, monkeypatch):
        monkeypatch.setenv("HEADLESS", "on")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is True

    def test_load_from_env_log_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.log_level == "DEBUG"

    def test_load_from_env_log_level_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "  dEbUg  ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.log_level == "DEBUG"

    @pytest.mark.parametrize("bad_value", ["chatty", "CRITICAL", "NOTSET"])
    def test_load_from_env_invalid_log_level(self, monkeypatch, bad_value):
        monkeypatch.setenv("LOG_LEVEL", bad_value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid LOG_LEVEL"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize(
        "key", ["HEADLESS", "AUTO_IMPORT_FROM_BROWSER", "DAEMON_ENABLED"]
    )
    def test_load_from_env_refuses_an_unreadable_flag(self, monkeypatch, key):
        monkeypatch.setenv(key, "flase")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match=f"Invalid {key}: 'flase'"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize(
        "key", ["HEADLESS", "AUTO_IMPORT_FROM_BROWSER", "DAEMON_ENABLED"]
    )
    def test_load_from_env_reads_a_blank_flag_as_unset(self, monkeypatch, key):
        from linkedin_mcp_server.config.loaders import load_from_env

        monkeypatch.delenv(key, raising=False)
        unset = load_from_env(AppConfig())
        monkeypatch.setenv(key, "  ")
        assert load_from_env(AppConfig()) == unset

    def test_load_from_env_defaults(self, monkeypatch):
        # Clear env vars
        for var in ["HEADLESS", "LOG_LEVEL"]:
            monkeypatch.delenv(var, raising=False)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.headless is True  # default

    def test_load_from_env_transport(self, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "streamable-http")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.transport == "streamable-http"
        assert config.server.transport_explicitly_set is True

    def test_load_from_env_transport_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "  StReAmAbLe-HtTp ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.transport == "streamable-http"
        assert config.server.transport_explicitly_set is True

    def test_load_from_env_transport_stdio_with_whitespace_and_case(self, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "  StDiO  ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.transport == "stdio"
        assert config.server.transport_explicitly_set is True

    def test_load_from_env_invalid_transport(self, monkeypatch):
        monkeypatch.setenv("TRANSPORT", "invalid")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid TRANSPORT"):
            load_from_env(AppConfig())

    def test_load_from_env_timeout(self, monkeypatch):
        monkeypatch.setenv("TIMEOUT", "10000")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.default_timeout == 10000

    def test_load_from_env_invalid_timeout(self, monkeypatch):
        monkeypatch.setenv("TIMEOUT", "invalid")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid TIMEOUT"):
            load_from_env(AppConfig())

    def test_load_from_env_tool_timeout(self, monkeypatch):
        monkeypatch.setenv("TOOL_TIMEOUT", "120.5")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.tool_timeout_seconds == 120.5

    def test_load_from_env_invalid_tool_timeout_non_numeric(self, monkeypatch):
        monkeypatch.setenv("TOOL_TIMEOUT", "abc")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid TOOL_TIMEOUT"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize("bad_value", ["0", "-5", "nan", "inf", "-inf"])
    def test_load_from_env_invalid_tool_timeout_non_finite_or_non_positive(
        self, monkeypatch, bad_value
    ):
        monkeypatch.setenv("TOOL_TIMEOUT", bad_value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid TOOL_TIMEOUT"):
            load_from_env(AppConfig())

    def test_load_from_args_tool_timeout(self):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--tool-timeout", "7.5"])
        assert config.server.tool_timeout_seconds == 7.5

    def test_explicit_args_override_environment(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        from linkedin_mcp_server.config import load_config

        config = load_config(["--log-level", "DEBUG"])

        assert config.server.log_level == "DEBUG"

    def test_zero_port_flag_is_refused_like_the_environment(self):
        # PORT=0 has always been refused, but `--port 0` was dropped as if it
        # had never been given, so the same invalid value behaved two ways.
        from linkedin_mcp_server.config import load_config

        with pytest.raises(ConfigurationError, match="Port 0 is not in valid range"):
            load_config(["--port", "0"])

    def test_zero_port_environment_is_refused(self, monkeypatch):
        monkeypatch.setenv("PORT", "0")
        from linkedin_mcp_server.config import load_config

        with pytest.raises(ConfigurationError, match="Port 0 is not in valid range"):
            load_config([])

    def test_unknown_explicit_arg_exits_with_status_two(self):
        from linkedin_mcp_server.config import load_config

        with pytest.raises(SystemExit) as exc_info:
            load_config(["--foreign-option"])

        assert exc_info.value.code == 2

    def test_claim_profile_root_defaults_off(self):
        """Taking over an occupied directory is never the default."""
        from linkedin_mcp_server.config.loaders import load_from_args

        assert load_from_args(AppConfig(), []).server.claim_profile_root is False

    def test_claim_profile_root_is_reachable_from_the_command_line(self):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--claim-profile-root"])
        assert config.server.claim_profile_root is True

    def test_claim_profile_root_is_not_part_of_the_owner_fingerprint(self):
        """It decides whether a marker may be written, not what the browser is.

        Listing it would stop a client that claimed the root from sharing the
        owner every other client of that profile uses.
        """
        from linkedin_mcp_server.daemon.descriptor import SHARED_CONFIG_FIELDS

        assert "claim_profile_root" not in SHARED_CONFIG_FIELDS

    @pytest.mark.parametrize("bad_value", ["0", "-1", "abc", "nan", "inf"])
    def test_load_from_args_invalid_tool_timeout(self, monkeypatch, bad_value):
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(AppConfig(), ["--tool-timeout", bad_value])

    def test_load_from_env_login_timeout(self, monkeypatch):
        monkeypatch.setenv("LOGIN_TIMEOUT", "600")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.login_timeout_seconds == 600.0

    def test_load_from_env_login_inline_wait(self, monkeypatch):
        monkeypatch.setenv("LOGIN_INLINE_WAIT", "10")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.login_inline_wait_seconds == 10.0

    def test_load_from_env_login_inline_wait_zero(self, monkeypatch):
        monkeypatch.setenv("LOGIN_INLINE_WAIT", "0")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.login_inline_wait_seconds == 0.0

    def test_load_from_env_invalid_login_timeout_non_numeric(self, monkeypatch):
        monkeypatch.setenv("LOGIN_TIMEOUT", "abc")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid LOGIN_TIMEOUT"):
            load_from_env(AppConfig())

    def test_load_from_env_invalid_login_inline_wait_non_numeric(self, monkeypatch):
        monkeypatch.setenv("LOGIN_INLINE_WAIT", "abc")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid LOGIN_INLINE_WAIT"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize("bad_value", ["-5", "nan", "inf", "-inf"])
    def test_load_from_env_invalid_login_timeout_non_finite_or_negative(
        self, monkeypatch, bad_value
    ):
        monkeypatch.setenv("LOGIN_TIMEOUT", bad_value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid LOGIN_TIMEOUT"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize("bad_value", ["-5", "nan", "inf", "-inf"])
    def test_load_from_env_invalid_login_inline_wait_non_finite_or_negative(
        self, monkeypatch, bad_value
    ):
        monkeypatch.setenv("LOGIN_INLINE_WAIT", bad_value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid LOGIN_INLINE_WAIT"):
            load_from_env(AppConfig())

    def test_load_from_args_login_timeout(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--login-timeout", "900"])
        assert config.browser.login_timeout_seconds == 900.0

    def test_load_from_args_login_inline_wait(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--login-inline-wait", "12"])
        assert config.browser.login_inline_wait_seconds == 12.0

    def test_load_from_args_login_inline_wait_zero(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--login-inline-wait", "0"])
        assert config.browser.login_inline_wait_seconds == 0.0

    def test_login_inline_wait_clamped_at_validate(self, monkeypatch):
        """Loader leaves the value as-is; validate() clamps to the ceiling."""
        monkeypatch.setenv("LOGIN_INLINE_WAIT", "99")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        # Loader does not clamp.
        assert config.browser.login_inline_wait_seconds == 99.0
        # validate() clamps in one place for both env and CLI paths.
        config.validate()
        assert config.browser.login_inline_wait_seconds == MAX_LOGIN_INLINE_WAIT_SECONDS

    @pytest.mark.parametrize(
        ("env_key", "attribute"),
        [
            ("BROWSER_WAIT", "browser_wait_seconds"),
            ("BROWSER_MIN_HOLD", "browser_min_hold_seconds"),
            ("BROWSER_IDLE_TIMEOUT", "browser_idle_timeout_seconds"),
        ],
    )
    def test_load_from_env_profile_sharing(self, monkeypatch, env_key, attribute):
        monkeypatch.setenv(env_key, "12.5")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert getattr(config.browser, attribute) == 12.5

    @pytest.mark.parametrize(
        ("env_key", "attribute"),
        [
            ("BROWSER_WAIT", "browser_wait_seconds"),
            ("BROWSER_MIN_HOLD", "browser_min_hold_seconds"),
            ("BROWSER_IDLE_TIMEOUT", "browser_idle_timeout_seconds"),
        ],
    )
    def test_load_from_env_profile_sharing_zero(self, monkeypatch, env_key, attribute):
        """0 has to survive the loader: it is a sentinel, not an empty value."""
        monkeypatch.setenv(env_key, "0")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert getattr(config.browser, attribute) == 0.0

    @pytest.mark.parametrize(
        "env_key", ["BROWSER_WAIT", "BROWSER_MIN_HOLD", "BROWSER_IDLE_TIMEOUT"]
    )
    @pytest.mark.parametrize("bad_value", ["abc", "-5", "nan", "inf", "-inf"])
    def test_load_from_env_invalid_profile_sharing(
        self, monkeypatch, env_key, bad_value
    ):
        monkeypatch.setenv(env_key, bad_value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match=f"Invalid {env_key}"):
            load_from_env(AppConfig())

    @pytest.mark.parametrize(
        ("flag", "attribute"),
        [
            ("--browser-wait", "browser_wait_seconds"),
            ("--browser-min-hold", "browser_min_hold_seconds"),
            ("--browser-idle-timeout", "browser_idle_timeout_seconds"),
        ],
    )
    def test_load_from_args_profile_sharing(self, monkeypatch, flag, attribute):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), [flag, "8"])
        assert getattr(config.browser, attribute) == 8.0

    def test_profile_sharing_clamped_at_validate_not_in_the_loader(self, monkeypatch):
        """One clamp, applied after both loaders, so CLI and env agree."""
        monkeypatch.setenv("BROWSER_WAIT", "99")
        monkeypatch.setenv("BROWSER_MIN_HOLD", "99")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.browser_wait_seconds == 99.0
        assert config.browser.browser_min_hold_seconds == 99.0

        config.validate()
        assert config.browser.browser_wait_seconds == MAX_BROWSER_WAIT_SECONDS
        assert (
            config.browser.browser_min_hold_seconds
            == MAX_BROWSER_WAIT_SECONDS - BROWSER_HANDOFF_MARGIN_SECONDS
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("false", False), ("true", True), ("0", False), ("1", True)],
    )
    def test_load_from_env_auto_import(self, monkeypatch, value, expected):
        monkeypatch.setenv("AUTO_IMPORT_FROM_BROWSER", value)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.auto_import_from_browser is expected

    def test_auto_import_default_is_none(self):
        assert BrowserConfig().auto_import_from_browser is None

    def test_load_from_args_no_auto_import(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--no-auto-import"])
        assert config.browser.auto_import_from_browser is False

    def test_load_from_args_auto_import(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--auto-import"])
        assert config.browser.auto_import_from_browser is True

    def test_args_no_auto_import_overrides_env_true(self, monkeypatch):
        monkeypatch.setenv("AUTO_IMPORT_FROM_BROWSER", "true")
        from linkedin_mcp_server.config import load_config

        config = load_config(["--no-auto-import"])
        assert config.browser.auto_import_from_browser is False

    def test_slow_mo_zero_overrides_env(self, monkeypatch):
        monkeypatch.setenv("SLOW_MO", "500")
        from linkedin_mcp_server.config import load_config

        assert load_config(["--slow-mo", "0"]).browser.slow_mo == 0

    def test_absent_slow_mo_keeps_env(self, monkeypatch):
        monkeypatch.setenv("SLOW_MO", "500")
        from linkedin_mcp_server.config import load_config

        assert load_config([]).browser.slow_mo == 500

    def test_absent_args_keep_env_false(self, monkeypatch):
        monkeypatch.setenv("AUTO_IMPORT_FROM_BROWSER", "false")
        from linkedin_mcp_server.config import load_config

        config = load_config([])
        assert config.browser.auto_import_from_browser is False

    def test_absent_args_and_env_keep_none(self, monkeypatch):
        monkeypatch.delenv("AUTO_IMPORT_FROM_BROWSER", raising=False)
        from linkedin_mcp_server.config import load_config

        config = load_config([])
        assert config.browser.auto_import_from_browser is None

    def test_daemon_enabled_default_is_false(self):
        # Supervision and liveness are unfinished, so a shared browser-owning
        # process is something you ask for, never something you get.
        assert ServerConfig().daemon_enabled is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("false", False), ("true", True), ("0", False), ("1", True)],
    )
    def test_load_from_env_daemon_enabled(self, monkeypatch, value, expected):
        monkeypatch.setenv("DAEMON_ENABLED", value)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.daemon_enabled is expected

    def test_load_from_args_daemon(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--daemon"])
        assert config.server.daemon_enabled is True

    def test_load_from_args_no_daemon(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = AppConfig()
        config.server.daemon_enabled = True
        config = load_from_args(config, ["--no-daemon"])
        assert config.server.daemon_enabled is False

    def test_no_daemon_flag_overrides_env_true(self, monkeypatch):
        # The way out for someone whose environment enables the daemon and who
        # needs one process back on its own browser.
        monkeypatch.setenv("DAEMON_ENABLED", "true")
        from linkedin_mcp_server.config import load_config

        config = load_config(["--no-daemon"])
        assert config.server.daemon_enabled is False

    def test_daemon_enabled_absent_keeps_default(self, monkeypatch):
        monkeypatch.delenv("DAEMON_ENABLED", raising=False)
        from linkedin_mcp_server.config import load_config

        config = load_config([])
        assert config.server.daemon_enabled is False

    def test_load_from_env_port(self, monkeypatch):
        monkeypatch.setenv("PORT", "9000")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.port == 9000

    def test_load_from_env_slow_mo(self, monkeypatch):
        monkeypatch.setenv("SLOW_MO", "100")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.slow_mo == 100

    def test_load_from_env_viewport(self, monkeypatch):
        monkeypatch.setenv("VIEWPORT", "1920x1080")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.viewport_width == 1920
        assert config.browser.viewport_height == 1080

    def test_load_from_env_invalid_viewport(self, monkeypatch):
        monkeypatch.setenv("VIEWPORT", "invalid")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid VIEWPORT"):
            load_from_env(AppConfig())

    def test_load_from_env_user_data_dir(self, monkeypatch):
        monkeypatch.setenv("USER_DATA_DIR", "/custom/profile")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.user_data_dir == "/custom/profile"

    def test_load_from_env_installer_temp_dir(self, monkeypatch):
        monkeypatch.setenv("INSTALLER_TEMP_DIR", "/custom/installer/temp")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.installer_temp_dir == "/custom/installer/temp"

    def test_load_from_args_installer_temp_dir(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(
            AppConfig(), ["--installer-temp-dir", "/custom/installer/temp"]
        )
        assert config.browser.installer_temp_dir == "/custom/installer/temp"

    def test_load_from_args_installer_temp_dir_overrides_env(self, monkeypatch):
        monkeypatch.setenv("INSTALLER_TEMP_DIR", "/env/installer/temp")
        from linkedin_mcp_server.config.loaders import load_from_args, load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.installer_temp_dir == "/env/installer/temp"
        config = load_from_args(config, ["--installer-temp-dir", "/cli/installer/temp"])
        assert config.browser.installer_temp_dir == "/cli/installer/temp"

    def test_load_from_env_import_from_browser(self, monkeypatch):
        monkeypatch.setenv("IMPORT_FROM_BROWSER", "brave")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.import_from_browser == "brave"

    def test_load_from_args_import_from_browser_value(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--import-from-browser", "chrome"])
        assert config.server.import_from_browser == "chrome"

    def test_load_from_args_import_from_browser_bare_flag_is_auto(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--import-from-browser"])
        assert config.server.import_from_browser == "auto"

    def test_load_from_args_import_from_browser_empty_is_auto(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig(), ["--import-from-browser="])
        assert config.server.import_from_browser == "auto"


class TestImportFromBrowserValidation:
    def test_valid_browser_passes(self):
        ServerConfig(import_from_browser="chrome").validate()  # no error

    def test_auto_passes(self):
        ServerConfig(import_from_browser="auto").validate()  # no error

    def test_invalid_value_raises(self):
        with pytest.raises(ConfigurationError, match="not supported"):
            ServerConfig(import_from_browser="firefox").validate()


class TestProxyConfig:
    """Proxy parsing, validation and the guarantee that no secret escapes."""

    SECRET = "s3cr3t-p@ss"

    def test_defaults_are_unset(self):
        config = BrowserConfig()
        assert config.proxy_server is None
        assert config.proxy_username is None
        assert config.proxy_password is None
        assert config.proxy_bypass is None
        assert config.proxy_settings() is None

    def test_server_only(self):
        config = BrowserConfig(proxy_server="http://proxy.example:8080")
        config.validate()
        assert config.proxy_settings() == {"server": "http://proxy.example:8080"}

    def test_scheme_less_server_means_http(self):
        config = BrowserConfig(proxy_server="proxy.example:8080")
        config.validate()
        assert config.proxy_server == "http://proxy.example:8080"

    def test_ipv6_server_keeps_brackets(self):
        config = BrowserConfig(proxy_server="http://[::1]:8080")
        config.validate()
        assert config.proxy_server == "http://[::1]:8080"

    def test_separate_credentials_and_bypass(self):
        config = BrowserConfig(
            proxy_server="https://proxy.example:443",
            proxy_username="user",
            proxy_password=self.SECRET,
            proxy_bypass=".internal,localhost",
        )
        config.validate()
        assert config.proxy_settings() == {
            "server": "https://proxy.example:443",
            "username": "user",
            "password": self.SECRET,
            "bypass": ".internal,localhost",
        }

    def test_embedded_credentials_are_split_out(self):
        # Patchright drops userinfo from the server URL, so leaving it embedded
        # would authenticate with nothing.
        config = BrowserConfig(proxy_server=f"http://user:{self.SECRET}@gate:7000")
        config.validate()
        assert config.proxy_server == "http://gate:7000"
        assert config.proxy_settings() == {
            "server": "http://gate:7000",
            "username": "user",
            "password": self.SECRET,
        }

    def test_embedded_credentials_are_percent_decoded(self):
        config = BrowserConfig(proxy_server="http://user:p%40ss@gate:7000")
        config.validate()
        assert config.proxy_password == "p@ss"

    def test_socks_without_credentials_is_allowed(self):
        config = BrowserConfig(proxy_server="socks5://127.0.0.1:1080")
        config.validate()
        assert config.proxy_settings() == {"server": "socks5://127.0.0.1:1080"}

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"proxy_username": "u"}, "without proxy_server"),
            ({"proxy_bypass": ".internal"}, "without proxy_server"),
            ({"proxy_server": "ftp://host:21"}, "is not supported"),
            ({"proxy_server": "http://host"}, "explicit port"),
            ({"proxy_server": "http://host:8080/path"}, "path, query or fragment"),
            ({"proxy_server": "http://host:8080?a=1"}, "path, query or fragment"),
            (
                {"proxy_server": "http://host:8080", "proxy_password": "p"},
                "without a username",
            ),
            (
                {
                    "proxy_server": "http://u:p@host:8080",
                    "proxy_username": "other",
                    "proxy_password": "other",
                },
                "one or the other",
            ),
            (
                {
                    "proxy_server": "socks5://host:1080",
                    "proxy_username": "u",
                    "proxy_password": "p",
                },
                "cannot authenticate",
            ),
        ],
    )
    def test_invalid_configurations_are_rejected(self, kwargs, match):
        with pytest.raises(ConfigurationError, match=match):
            BrowserConfig(**kwargs).validate()

    def test_rejection_never_echoes_the_secret(self):
        # Validation errors reach the console, so they must not quote the value.
        with pytest.raises(ConfigurationError) as excinfo:
            BrowserConfig(
                proxy_server=f"socks5://user:{self.SECRET}@host:1080"
            ).validate()
        assert self.SECRET not in str(excinfo.value)

    def test_password_never_appears_in_repr(self):
        # cli_main logs the whole config at DEBUG level, and users paste those
        # logs into issue reports.
        config = BrowserConfig(
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password=self.SECRET,
        )
        assert self.SECRET not in repr(config)
        assert self.SECRET not in repr(AppConfig(browser=config))

    def test_server_stays_visible_in_repr(self):
        # It carries no secret after normalization and is the field you need to
        # diagnose a proxy problem.
        config = BrowserConfig(proxy_server="http://proxy.example:8080")
        assert "proxy.example" in repr(config)


class TestProxyLoaders:
    SECRET = "env-s3cr3t"

    def test_load_from_env_all_fields(self, monkeypatch):
        monkeypatch.setenv("PROXY_SERVER", "http://gate.example:7000")
        monkeypatch.setenv("PROXY_USERNAME", "envuser")
        monkeypatch.setenv("PROXY_PASSWORD", self.SECRET)
        monkeypatch.setenv("PROXY_BYPASS", ".internal")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.proxy_server == "http://gate.example:7000"
        assert config.browser.proxy_username == "envuser"
        assert config.browser.proxy_password == self.SECRET
        assert config.browser.proxy_bypass == ".internal"

    def test_env_accepts_a_provider_url_with_credentials(self, monkeypatch):
        # The environment is not world-readable the way argv is, so the
        # convenience form providers hand out is accepted here.
        monkeypatch.setenv("PROXY_SERVER", f"http://envuser:{self.SECRET}@gate:7000")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_server == "http://gate:7000"
        assert config.browser.proxy_password == self.SECRET

    def test_load_from_args_proxy_flags(self, monkeypatch):
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(
            AppConfig(),
            [
                "--proxy-server",
                "http://cli.example:3128",
                "--proxy-username",
                "cliuser",
                "--proxy-bypass",
                ".internal",
            ],
        )
        assert config.browser.proxy_server == "http://cli.example:3128"
        assert config.browser.proxy_username == "cliuser"
        assert config.browser.proxy_bypass == ".internal"

    def test_args_override_env(self, monkeypatch):
        monkeypatch.setenv("PROXY_SERVER", "http://env.example:7000")
        from linkedin_mcp_server.config.loaders import load_from_args, load_from_env

        config = load_from_args(
            load_from_env(AppConfig()), ["--proxy-server", "http://cli:3128"]
        )
        assert config.browser.proxy_server == "http://cli:3128"

    def test_cli_rejects_embedded_credentials(self, monkeypatch, capsys):
        # There is no --proxy-password flag because argv is world-readable;
        # accepting a credential URL here would hand that exposure back.
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(
                AppConfig(), ["--proxy-server", f"http://u:{self.SECRET}@h:8080"]
            )
        assert self.SECRET not in capsys.readouterr().err


class TestProxyMcpbPlaceholders:
    """An MCPB host that left ``${user_config.NAME}`` in place means "blank".

    Claude Desktop builds its substitution map from the manifest's ``default``
    values and the user's answers. A field with neither is missing from that
    map, so the placeholder reaches the process verbatim. The manifest now
    declares a ``default`` for each optional field, which is what stops this at
    the source; these cover the bundles installed before that and any host that
    resolves user configuration differently.
    """

    PLACEHOLDERS = {
        "PROXY_SERVER": "${user_config.proxy_server}",
        "PROXY_USERNAME": "${user_config.proxy_username}",
        "PROXY_PASSWORD": "${user_config.proxy_password}",
        "PROXY_BYPASS": "${user_config.proxy_bypass}",
    }

    def test_all_four_placeholders_leave_no_proxy_configured(self, monkeypatch):
        # The reported crash: validate() read the literal as an address, found
        # no port, and took the whole server down at startup.
        for key, value in self.PLACEHOLDERS.items():
            monkeypatch.setenv(key, value)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_settings() is None

    @pytest.mark.parametrize("key", sorted(PLACEHOLDERS))
    def test_each_placeholder_alone_leaves_no_proxy_configured(
        self, monkeypatch, key: str
    ):
        # Each one on its own is fatal too: without a server the other three
        # raise "is set without proxy_server" instead.
        monkeypatch.setenv(key, self.PLACEHOLDERS[key])
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_settings() is None

    def test_a_configured_proxy_is_not_given_placeholder_credentials(self, monkeypatch):
        # The quieter half. With only the server filled in, the remaining three
        # placeholders used to reach Chromium as a username, a password and a
        # bypass list. That does not crash; it fails as a 407 the browser
        # retries until the page times out, which reads as an expired session.
        monkeypatch.setenv("PROXY_SERVER", "http://gate.example:7000")
        for key in ("PROXY_USERNAME", "PROXY_PASSWORD", "PROXY_BYPASS"):
            monkeypatch.setenv(key, self.PLACEHOLDERS[key])
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_settings() == {"server": "http://gate.example:7000"}

    def test_dropped_placeholders_are_named_once(self, monkeypatch, caplog):
        # Silence would be the wrong default here: if a host ever failed to
        # substitute a field the user did fill in, the browser would go out on
        # the real address with no trace of why.
        for key, value in self.PLACEHOLDERS.items():
            monkeypatch.setenv(key, value)
        from linkedin_mcp_server.config.loaders import load_from_env

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.config"):
            load_from_env(AppConfig())

        warnings = [
            record
            for record in caplog.records
            if "unsubstituted MCPB placeholder" in record.getMessage()
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        for key in self.PLACEHOLDERS:
            assert key in message

    def test_a_blank_chrome_path_placeholder_keeps_the_bundled_browser(
        self, monkeypatch
    ):
        # A literal reaching chrome_path would launch "${user_config.chrome_path}"
        # as an executable instead of falling back to the bundled Chromium.
        monkeypatch.setenv("CHROME_PATH", "${user_config.chrome_path}")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.chrome_path is None

    def test_a_chrome_path_from_the_bundle_settings_is_used(self, monkeypatch):
        monkeypatch.setenv("CHROME_PATH", "/usr/bin/google-chrome")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.chrome_path == "/usr/bin/google-chrome"

    def test_a_real_setting_that_merely_resembles_one_is_kept(self, monkeypatch):
        # Anything that is not the exact literal stays a value and reaches
        # validation, which can explain it.
        monkeypatch.setenv("PROXY_SERVER", "http://user-config.example:7000")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_server == "http://user-config.example:7000"

    def test_a_password_shaped_like_another_placeholder_survives(self, monkeypatch):
        # Why the comparison is against one exact string per variable and not
        # against the shape of a placeholder. "${user_config.token}" is a legal
        # password, and a host that never wrote it cannot be the reason it is
        # there. Dropping it would leave the browser authenticating with
        # nothing against a proxy that requires it.
        secret = "${user_config.token}"
        monkeypatch.setenv("PROXY_SERVER", "http://gate.example:7000")
        monkeypatch.setenv("PROXY_USERNAME", "envuser")
        monkeypatch.setenv("PROXY_PASSWORD", secret)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_password == secret

    def test_a_password_keeps_its_surrounding_whitespace(self, monkeypatch):
        # Guards the decision not to strip: trimming a password would be a
        # wrong credential with nothing in the logs to show for it.
        monkeypatch.setenv("PROXY_SERVER", "http://gate.example:7000")
        monkeypatch.setenv("PROXY_USERNAME", "envuser")
        monkeypatch.setenv("PROXY_PASSWORD", "  padded  ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        config.validate()
        assert config.browser.proxy_password == "  padded  "


class TestProxyEmptyPassword:
    """A username with an empty password is a legitimate proxy credential.

    Playwright supports it explicitly for key-style accounts, so it must not be
    rejected, and proxy_settings() must still send it rather than dropping it
    on truthiness.
    """

    def test_username_without_a_password_is_allowed(self):
        config = BrowserConfig(proxy_server="http://host:8080", proxy_username="user")
        config.validate()
        assert config.proxy_settings() == {
            "server": "http://host:8080",
            "username": "user",
        }

    def test_empty_password_is_preserved(self):
        config = BrowserConfig(
            proxy_server="http://host:8080", proxy_username="user", proxy_password=""
        )
        config.validate()
        settings = config.proxy_settings()
        assert settings is not None
        assert settings["password"] == ""


class TestProxyEncodedUserinfo:
    """A percent-encoded '@' hides credentials from the URL parser.

    urlsplit reads "user%3Apass%40host" as a plain hostname, so the credentials
    are neither split out nor hidden from the logs while staying trivially
    decodable. Patchright cannot parse it either and falls back to a nonsense
    host, so the browser would not reach the intended proxy anyway.
    """

    ENCODED = "http://user%3Apass%40gate.example:7000"

    def test_schema_rejects_it(self):
        with pytest.raises(ConfigurationError, match="percent-encoded"):
            BrowserConfig(proxy_server=self.ENCODED).validate()

    def test_schema_rejects_the_scheme_less_form(self):
        with pytest.raises(ConfigurationError, match="percent-encoded"):
            BrowserConfig(proxy_server="user%3Apass%40gate.example:7000").validate()

    def test_the_rejection_does_not_echo_the_value(self):
        with pytest.raises(ConfigurationError) as excinfo:
            BrowserConfig(proxy_server=self.ENCODED).validate()
        assert "user%3Apass" not in str(excinfo.value)

    def test_cli_rejects_it(self, monkeypatch, capsys):
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(AppConfig(), ["--proxy-server", self.ENCODED])
        assert "user%3Apass" not in capsys.readouterr().err

    def test_an_ordinary_address_is_unaffected(self):
        config = BrowserConfig(proxy_server="http://gate.example:7000")
        config.validate()
        assert config.proxy_server == "http://gate.example:7000"


# ---------------------------------------------------------------------------
# The settings table, case by case.
#
# Every setting in ``loaders.SETTINGS`` needs an entry in ``_CASES``, written
# by hand: the expected fields are literals, never computed through a parser.
# A setting added without one fails the mandatory-cases test, and once it has
# one, every generic test below runs on it.


_UNTOUCHED = "<untouched>"


#: Stands for a private token file in the cases below, which are literals and
#: test ids. ``_real_argv`` writes the file into the test's own directory: the
#: repository can only record mode 0644, which the loader refuses for a token.
_HTTP_TOKEN_FILE = "<private token file>"


def _real_argv(argv: tuple[str, ...], directory: Path) -> list[str]:
    """*argv* with ``_HTTP_TOKEN_FILE`` replaced by a real file in *directory*.

    The file holds a token as an editor saves it, with one trailing newline.
    """
    if _HTTP_TOKEN_FILE not in argv:
        return list(argv)
    path = directory / "token"
    path.write_text("fixture-http-token-0123456789abcdef0123\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return [str(path) if arg == _HTTP_TOKEN_FILE else arg for arg in argv]


class _Keep:
    """Expected: the fields still hold what they held before this source."""

    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()


@dataclass(frozen=True)
class _Refused:
    error: type[BaseException]
    label: str

    def __repr__(self) -> str:
        return self.label


#: A bad environment value, or a CLI value refused after parsing.
REFUSED = _Refused(ConfigurationError, "REFUSED")
#: argparse refuses it and exits with status 2.
EXIT_2 = _Refused(SystemExit, "EXIT_2")


@dataclass(frozen=True)
class _Cases:
    """The cases for one setting.

    *kind* decides which case classes are mandatory: ``text`` takes any value
    at load time, ``number`` has 0 as its falsy value, ``flag`` has False,
    ``switch`` is a CLI-only flag without a value, and ``other`` refuses
    nonsense without a falsy value of its own. A tuple expectation lists the
    *fields* in order; a scalar stands for the one field.
    """

    kind: Literal["text", "number", "flag", "choice", "switch", "other"]
    fields: tuple[str, ...]
    env: dict[str | None, object] | None = None
    cli: dict[tuple[str, ...], object] | None = None


_CASES: dict[str, _Cases] = {
    "HEADLESS": _Cases(
        "flag",
        ("browser.headless",),
        env={
            None: KEEP,
            "": KEEP,
            "  ": KEEP,
            "true": True,
            " FaLsE ": False,
            "0": False,
            "on": True,
            "flase": REFUSED,
        },
        # Only the negative direction exists on the command line.
        cli={(): KEEP, ("--no-headless",): False},
    ),
    "LOG_LEVEL": _Cases(
        "choice",
        ("server.log_level",),
        env={
            None: KEEP,
            "": KEEP,
            " ": REFUSED,
            "  dEbUg ": "DEBUG",
            "ERROR": "ERROR",
            "CRITICAL": REFUSED,
        },
        cli={
            (): KEEP,
            ("--log-level", "DEBUG"): "DEBUG",
            ("--log-level", ""): EXIT_2,
            # Case-sensitive on the command line, unlike the variable.
            ("--log-level", "debug"): EXIT_2,
        },
    ),
    "TRANSPORT": _Cases(
        "choice",
        ("server.transport", "server.transport_explicitly_set"),
        env={
            None: KEEP,
            "": KEEP,
            " ": REFUSED,
            " StDiO ": ("stdio", True),
            "streamable-http": ("streamable-http", True),
            "http": REFUSED,
        },
        cli={
            (): KEEP,
            ("--transport", "stdio"): ("stdio", True),
            ("--transport", "streamable-http"): ("streamable-http", True),
            ("--transport", ""): EXIT_2,
            ("--transport", "STDIO"): EXIT_2,
        },
    ),
    "HOST": _Cases(
        "text",
        ("server.host",),
        env={None: KEEP, "": KEEP, " ": " ", "localhost": "localhost"},
        cli={(): KEEP, ("--host", ""): KEEP, ("--host", "0.0.0.0"): "0.0.0.0"},
    ),
    "PORT": _Cases(
        "number",
        ("server.port",),
        # 0 reaches AppConfig.validate(), which refuses it for both sources.
        env={None: KEEP, "": KEEP, "0": 0, "9000": 9000, "80.5": REFUSED},
        cli={
            (): KEEP,
            ("--port", "0"): 0,
            ("--port", "9100"): 9100,
            ("--port", ""): EXIT_2,
            ("--port", "http"): EXIT_2,
        },
    ),
    "HTTP_PATH": _Cases(
        "text",
        ("server.path",),
        env={None: KEEP, "": KEEP, "/env": "/env"},
        cli={(): KEEP, ("--path", ""): KEEP, ("--path", "/cli"): "/cli"},
    ),
    # Taken as given and judged by AppConfig.validate() for HTTP only, so a
    # malformed variable cannot block a file that replaces it. The file wins.
    "LINKEDIN_MCP_HTTP_TOKEN": _Cases(
        "text",
        ("server.http_token",),
        env={None: KEEP, "": KEEP, " ": " ", "short": "short", "a" * 32: "a" * 32},
        cli={
            (): KEEP,
            ("--http-token-file", ""): KEEP,
            ("--http-token-file", _HTTP_TOKEN_FILE): (
                "fixture-http-token-0123456789abcdef0123"
            ),
            ("--http-token-file", "/nonexistent/linkedin-mcp-token"): REFUSED,
        },
    ),
    "LINKEDIN_MCP_ALLOW_UNAUTHENTICATED_HTTP": _Cases(
        "flag",
        ("server.allow_unauthenticated_http",),
        env={
            None: KEEP,
            "": KEEP,
            " ": KEEP,
            "false": False,
            "true": True,
            "ture": REFUSED,
        },
    ),
    "SLOW_MO": _Cases(
        "number",
        ("browser.slow_mo",),
        env={None: KEEP, "": KEEP, "0": 0, "250": 250, "fast": REFUSED},
        cli={
            (): KEEP,
            ("--slow-mo", "0"): 0,
            ("--slow-mo", "100"): 100,
            ("--slow-mo", ""): EXIT_2,
            ("--slow-mo", "1.5"): EXIT_2,
        },
    ),
    "USER_AGENT": _Cases(
        "other",
        (),
        env={None: KEEP, "": KEEP, " ": REFUSED, "CustomAgent/1.0": REFUSED},
        cli={
            (): KEEP,
            ("--user-agent", ""): KEEP,
            ("--user-agent", "CustomAgent/1.0"): REFUSED,
        },
    ),
    "VIEWPORT": _Cases(
        "other",
        ("browser.viewport_width", "browser.viewport_height"),
        # 0x0 reaches BrowserConfig.validate(), which refuses it.
        env={
            None: KEEP,
            "": KEEP,
            "1920X1080": (1920, 1080),
            "0x0": (0, 0),
            "wide": REFUSED,
            "1x2x3": REFUSED,
        },
        cli={
            (): KEEP,
            ("--viewport", ""): KEEP,
            ("--viewport", "800x600"): (800, 600),
            ("--viewport", "0x0"): (0, 0),
            ("--viewport", "wide"): REFUSED,
        },
    ),
    "TIMEOUT": _Cases(
        "number",
        ("browser.default_timeout",),
        # The variable converts only, so TIMEOUT=0 can be repaired by
        # --timeout; the flag refuses 0 itself.
        env={None: KEEP, "": KEEP, "0": 0, "8000": 8000, "slow": REFUSED},
        cli={
            (): KEEP,
            ("--timeout", "10"): 10,
            ("--timeout", "0"): EXIT_2,
            ("--timeout", ""): EXIT_2,
        },
    ),
    "TOOL_TIMEOUT": _Cases(
        "number",
        ("server.tool_timeout_seconds",),
        env={
            None: KEEP,
            "": KEEP,
            "0": REFUSED,
            "90.5": 90.5,
            "inf": REFUSED,
            "abc": REFUSED,
        },
        cli={
            (): KEEP,
            ("--tool-timeout", "7.5"): 7.5,
            ("--tool-timeout", "0"): EXIT_2,
            ("--tool-timeout", ""): EXIT_2,
        },
    ),
    **{
        key: _Cases(
            "number",
            (field,),
            env={
                None: KEEP,
                "": KEEP,
                "0": 0.0,
                "99": 99.0,  # clamped in validate(), not here
                "-5": REFUSED,
                "nan": REFUSED,
            },
            cli={
                (): KEEP,
                (flag, "0"): 0.0,
                (flag, "12.5"): 12.5,
                (flag, ""): EXIT_2,
                (flag, "-1"): EXIT_2,
            },
        )
        for key, flag, field in (
            ("LOGIN_TIMEOUT", "--login-timeout", "browser.login_timeout_seconds"),
            (
                "LOGIN_INLINE_WAIT",
                "--login-inline-wait",
                "browser.login_inline_wait_seconds",
            ),
            ("BROWSER_WAIT", "--browser-wait", "browser.browser_wait_seconds"),
            (
                "BROWSER_MIN_HOLD",
                "--browser-min-hold",
                "browser.browser_min_hold_seconds",
            ),
            (
                "BROWSER_IDLE_TIMEOUT",
                "--browser-idle-timeout",
                "browser.browser_idle_timeout_seconds",
            ),
        )
    },
    "CHROME_PATH": _Cases(
        "text",
        ("browser.chrome_path",),
        env={
            None: KEEP,
            "": KEEP,
            "${user_config.chrome_path}": KEEP,
            "/usr/bin/chrome": "/usr/bin/chrome",
        },
        cli={
            (): KEEP,
            ("--chrome-path", ""): KEEP,
            ("--chrome-path", "/opt/chrome"): "/opt/chrome",
        },
    ),
    "PROXY_SERVER": _Cases(
        "text",
        ("browser.proxy_server",),
        # The variable may carry credentials; the flag refuses them.
        env={
            None: KEEP,
            "": KEEP,
            "${user_config.proxy_server}": KEEP,
            "http://u:p@gate:7000": "http://u:p@gate:7000",
        },
        cli={
            (): KEEP,
            ("--proxy-server", ""): KEEP,
            ("--proxy-server", "http://cli:3128"): "http://cli:3128",
            ("--proxy-server", "http://u:p@gate:7000"): EXIT_2,
        },
    ),
    "PROXY_USERNAME": _Cases(
        "text",
        ("browser.proxy_username",),
        env={
            None: KEEP,
            "": KEEP,
            "${user_config.proxy_username}": KEEP,
            " envuser ": " envuser ",
        },
        cli={
            (): KEEP,
            ("--proxy-username", ""): KEEP,
            ("--proxy-username", "cliuser"): "cliuser",
        },
    ),
    "PROXY_BYPASS": _Cases(
        "text",
        ("browser.proxy_bypass",),
        env={
            None: KEEP,
            "": KEEP,
            "${user_config.proxy_bypass}": KEEP,
            ".internal": ".internal",
        },
        cli={
            (): KEEP,
            ("--proxy-bypass", ""): KEEP,
            ("--proxy-bypass", ".cli"): ".cli",
        },
    ),
    # Environment only, and never stripped.
    "PROXY_PASSWORD": _Cases(
        "text",
        ("browser.proxy_password",),
        env={
            None: KEEP,
            "": KEEP,
            "${user_config.proxy_password}": KEEP,
            "  padded  ": "  padded  ",
            "${user_config.token}": "${user_config.token}",
        },
    ),
    **{
        flag: _Cases("switch", (field,), cli={(): KEEP, (flag,): True})
        for flag, field in (
            ("--login", "server.login"),
            ("--login-viewer", "server.login_viewer"),
            ("--status", "server.status"),
            ("--logout", "server.logout"),
            ("--claim-profile-root", "server.claim_profile_root"),
        )
    },
    "USER_DATA_DIR": _Cases(
        "text",
        ("browser.user_data_dir",),
        env={None: KEEP, "": KEEP, "/env/profile": "/env/profile"},
        cli={
            (): KEEP,
            ("--user-data-dir", ""): KEEP,
            ("--user-data-dir", "/cli/profile"): "/cli/profile",
        },
    ),
    "INSTALLER_TEMP_DIR": _Cases(
        "text",
        ("browser.installer_temp_dir",),
        env={None: KEEP, "": KEEP, "/env/temp": "/env/temp"},
        cli={
            (): KEEP,
            ("--installer-temp-dir", ""): KEEP,
            ("--installer-temp-dir", "/cli/temp"): "/cli/temp",
        },
    ),
    "IMPORT_FROM_BROWSER": _Cases(
        "text",
        ("server.import_from_browser",),
        # An empty variable is unset; an explicit empty flag means auto.
        # Unknown browsers are refused by ServerConfig.validate().
        env={
            None: KEEP,
            "": KEEP,
            " ": "auto",
            " Brave ": "brave",
            "firefox": "firefox",
        },
        cli={
            (): KEEP,
            ("--import-from-browser",): "auto",
            ("--import-from-browser=",): "auto",
            ("--import-from-browser", ""): "auto",
            ("--import-from-browser", " Chrome "): "chrome",
        },
    ),
    **{
        key: _Cases(
            "flag",
            (field,),
            env={
                None: KEEP,
                "": KEEP,
                " ": KEEP,
                "false": False,
                "1": True,
                "flase": REFUSED,
            },
            cli={
                (): KEEP,
                (on,): True,
                (off,): False,
                (on, off): EXIT_2,
            },
        )
        for key, on, off, field in (
            (
                "AUTO_IMPORT_FROM_BROWSER",
                "--auto-import",
                "--no-auto-import",
                "browser.auto_import_from_browser",
            ),
            ("DAEMON_ENABLED", "--daemon", "--no-daemon", "server.daemon_enabled"),
        )
    },
    # Checked at startup only; the module that uses each reads it at runtime.
    "LINKEDIN_TRACE_MODE": _Cases(
        "other",
        (),
        env={
            None: KEEP,
            "": KEEP,
            "  ": KEEP,
            "on_error": KEEP,
            " Always ": KEEP,
            "off": KEEP,
            "false": KEEP,
            "alwyas": REFUSED,
        },
    ),
    "LINKEDIN_MCP_CHECK_FOR_UPDATES": _Cases(
        "other",
        (),
        env={
            None: KEEP,
            "": KEEP,
            " ": KEEP,
            "off": KEEP,
            "1": KEEP,
            "disabled": REFUSED,
        },
    ),
}


def _name(setting: Setting) -> str:
    """The environment variable, or the flag of a command-line-only setting."""
    if setting.env is not None:
        return setting.env.key
    assert isinstance(setting.cli, Switch)
    return setting.cli.flag


def _field(config: AppConfig, path: str) -> object:
    section, name = path.split(".")
    return getattr(getattr(config, section), name)


def _with(fields: tuple[str, ...], values: tuple[object, ...]) -> AppConfig:
    config = AppConfig()
    for path, value in zip(fields, values, strict=True):
        section, name = path.split(".")
        setattr(getattr(config, section), name, value)
    return config


def _values(expected: object) -> tuple[object, ...]:
    return expected if isinstance(expected, tuple) else (expected,)


def _untouched(cases: _Cases) -> AppConfig:
    """A configuration whose fields for *cases* hold a marker no source writes.

    So dropping a value cannot pass by coinciding with the default.
    """
    return _with(cases.fields, (_UNTOUCHED,) * len(cases.fields))


def _channels(source: str) -> list[tuple[str, object, object]]:
    return [
        (name, given, expected)
        for name, cases in _CASES.items()
        for given, expected in ((getattr(cases, source) or {}).items())
    ]


def _set_env(monkeypatch: pytest.MonkeyPatch, values: dict[str, str | None]) -> None:
    for name, key in vars(EnvironmentKeys).items():
        if not name.startswith("_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(key, value)


def _id(value: object) -> str:
    return value if isinstance(value, str) else repr(value)


def _assert_result(
    load: Callable[[], AppConfig],
    fields: tuple[str, ...],
    expected: object,
    *,
    before: tuple[object, ...],
    source: str,
) -> None:
    """Load, and compare the whole configuration, so a stray write shows too.

    A refusal has to name *source*, or exit with status 2 from argparse.
    """
    if isinstance(expected, _Refused):
        with pytest.raises(expected.error) as excinfo:
            load()
        if isinstance(excinfo.value, SystemExit):
            assert excinfo.value.code == 2
        else:
            assert source in str(excinfo.value)
        return
    values = before if expected is KEEP else _values(expected)
    assert asdict(load()) == asdict(_with(fields, values))


class TestSettingsTable:
    """Every setting, through every channel it has, against written cases."""

    def test_every_environment_key_belongs_to_a_setting(self):
        keys = {
            value
            for name, value in vars(EnvironmentKeys).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        assert keys == {setting.env.key for setting in SETTINGS if setting.env}

    def test_every_option_belongs_to_a_setting_or_is_ignored(self):
        flags = set(IGNORED_OPTIONS)
        for setting in SETTINGS:
            match setting.cli:
                case SwitchPair(on=on, off=off):
                    flags |= {on, off}
                case Option(flag=flag) | Switch(flag=flag):
                    flags.add(flag)
        parsed = {
            option
            for option in build_parser()._option_string_actions
            if option.startswith("--")
        }
        assert parsed == flags

    @pytest.mark.parametrize("setting", SETTINGS, ids=_name)
    def test_every_setting_has_the_mandatory_cases(self, setting: Setting):
        cases = _CASES.get(_name(setting))
        assert cases is not None, f"{_name(setting)} has no entry in _CASES"
        assert (cases.env is not None) == (setting.env is not None)
        assert (cases.cli is not None) == (setting.cli is not None)

        default = tuple(_field(AppConfig(), path) for path in cases.fields)

        def values(table: dict) -> list[tuple[object, ...]]:
            return [
                _values(expected)
                for expected in table.values()
                if expected is not KEEP and not isinstance(expected, _Refused)
            ]

        def refuses(table: dict) -> bool:
            return any(isinstance(e, _Refused) for e in table.values())

        if cases.env is not None:
            assert {None, ""} <= set(cases.env), "absence and the empty value"
            if cases.fields:
                assert any(v != default for v in values(cases.env)), "a value"
            if cases.kind == "number":
                assert "0" in cases.env
            if cases.kind == "flag":
                assert (False,) in values(cases.env)
            if cases.kind != "text":
                assert refuses(cases.env)
        if cases.cli is not None:
            assert () in cases.cli, "absence"
            if cases.fields:
                assert any(v != default for v in values(cases.cli)), "a value"
            if isinstance(setting.cli, Option):
                flag = setting.cli.flag
                assert (flag, "") in cases.cli, "the explicit empty value"
                if cases.kind == "number":
                    assert (flag, "0") in cases.cli
                if cases.kind != "text":
                    assert refuses(cases.cli)
            if isinstance(setting.cli, SwitchPair):
                assert (False,) in values(cases.cli)

    @pytest.mark.parametrize(
        ("name", "raw", "expected"),
        _channels("env"),
        ids=_id,
    )
    def test_environment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        raw: str | None,
        expected: object,
    ):
        cases = _CASES[name]
        _set_env(monkeypatch, {name: raw})
        _assert_result(
            lambda: load_from_env(_untouched(cases)),
            cases.fields,
            expected,
            before=(_UNTOUCHED,) * len(cases.fields),
            source=name,
        )

    @pytest.mark.parametrize(
        ("name", "argv", "expected"),
        _channels("cli"),
        ids=_id,
    )
    def test_command_line(
        self, tmp_path: Path, name: str, argv: tuple[str, ...], expected: object
    ):
        cases = _CASES[name]
        _assert_result(
            lambda: load_from_args(_untouched(cases), _real_argv(argv, tmp_path)),
            cases.fields,
            expected,
            before=(_UNTOUCHED,) * len(cases.fields),
            source=argv[0] if argv else name,
        )

    @pytest.mark.parametrize(
        ("name", "argv", "expected"),
        [
            (name, argv, expected)
            for name, argv, expected in _channels("cli")
            if _CASES[name].env is not None
            and _CASES[name].fields
            and not isinstance(expected, _Refused)
        ],
        ids=_id,
    )
    def test_the_command_line_wins_over_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        argv: tuple[str, ...],
        expected: object,
    ):
        """A given value overrides the variable; absence and ignored empties do not.

        Paired with an environment value that differs from what the flag sets,
        so a flag that was given but dropped (0, False) cannot pass.
        """
        cases = _CASES[name]
        assert cases.env is not None
        flag_values = None if expected is KEEP else _values(expected)
        raw, from_env = next(
            (raw, _values(result))
            for raw, result in cases.env.items()
            if result is not KEEP
            and not isinstance(result, _Refused)
            and _values(result) != flag_values
        )
        _set_env(monkeypatch, {name: raw})
        _assert_result(
            lambda: load_from_args(
                load_from_env(_untouched(cases)), _real_argv(argv, tmp_path)
            ),
            cases.fields,
            expected,
            before=from_env,
            source=name,
        )

    @pytest.mark.parametrize(
        "setting",
        [s for s in SETTINGS if s.env and _CASES[_name(s)].kind != "text"],
        ids=_name,
    )
    def test_the_environment_refuses_nonsense(
        self, monkeypatch: pytest.MonkeyPatch, setting: Setting
    ):
        assert setting.env is not None
        _set_env(monkeypatch, {setting.env.key: "nonsense"})
        with pytest.raises(ConfigurationError, match=setting.env.key):
            load_from_env(AppConfig())

    @pytest.mark.parametrize(
        "setting",
        [
            s
            for s in SETTINGS
            if isinstance(s.cli, Option) and _CASES[_name(s)].kind != "text"
        ],
        ids=_name,
    )
    def test_the_command_line_refuses_nonsense_in_its_declared_phase(
        self, setting: Setting
    ):
        """In the phase the cases name: argparse's exit 2, or ConfigurationError."""
        assert isinstance(setting.cli, Option)
        cli = _CASES[_name(setting)].cli
        assert cli is not None
        (phase,) = {e.error for e in cli.values() if isinstance(e, _Refused)}
        with pytest.raises(phase):
            load_from_args(AppConfig(), [setting.cli.flag, "nonsense"])


_ENVIRONMENT_NAMES = {"environ", "getenv", "environb", "getenvb"}


def _raw_read_nodes(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Every node in *tree* that reaches the process environment, with its owner.

    Import-aware, so ``import os as x`` then ``x.getenv(...)`` and ``from os
    import environ as e`` count as much as ``os.environ``. The owner is the
    enclosing function, or ``<module>`` for a read outside any function.
    """
    modules = {"os"}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "os"}
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            names |= {
                a.asname or a.name for a in node.names if a.name in _ENVIRONMENT_NAMES
            }
    found: list[tuple[ast.AST, str]] = []

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = node.name
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _ENVIRONMENT_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            found.append((node, owner))
        elif isinstance(node, ast.Name) and node.id in names:
            found.append((node, owner))
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            if {a.name for a in node.names} & _ENVIRONMENT_NAMES:
                found.append((node, owner))
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return found


def _raw_reads(source: str) -> set[str]:
    """Functions in *source* that touch the process environment."""
    return {owner for _, owner in _raw_read_nodes(ast.parse(source))}


def _placeholder_keys_read(source: str) -> bool:
    """Whether ``_warn_about_placeholders`` asks the environment only for
    the keys of ``_MCPB_PLACEHOLDERS``.

    Every environment read inside it, in any spelling the detector above
    knows, has to be the ``.environ`` of an ``<os>.environ.get(key)`` call
    whose ``key`` iterates ``_MCPB_PLACEHOLDERS``. The reads come from the
    whole module, so an alias imported at module level is resolved inside
    the function too.
    """
    tree = ast.parse(source)
    (function,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_warn_about_placeholders"
    ]
    keys = {
        generator.target.elts[0].id
        for generator in ast.walk(function)
        if isinstance(generator, ast.comprehension)
        and ast.unparse(generator.iter) == "_MCPB_PLACEHOLDERS.items()"
        and isinstance(generator.target, ast.Tuple)
        and isinstance(generator.target.elts[0], ast.Name)
    }
    inside = {id(node) for node in ast.walk(function)}
    reads = {id(node) for node, _ in _raw_read_nodes(tree)} & inside
    allowed = {
        id(node.func.value)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in keys
    }
    return bool(reads) and reads == allowed


class TestTheEnvironmentIsReadInOnePlace:
    """``_env`` is the one raw read, so every setting gets its placeholder rule.

    The placeholder warning is the one other reader, and it may ask only for
    the keys of ``_MCPB_PLACEHOLDERS``.
    """

    ALLOWED = {"_env", "_warn_about_placeholders"}

    @staticmethod
    def _source() -> str:
        from linkedin_mcp_server.config import loaders

        return Path(loaders.__file__).read_text(encoding="utf-8")

    def test_the_loader_reads_the_environment_only_through_env(self):
        assert _raw_reads(self._source()) == self.ALLOWED

    def test_the_placeholder_warning_reads_only_placeholder_keys(self):
        source = self._source()
        assert _placeholder_keys_read(source)

        for extra, imported in (
            ("os.environ.get('HOST')", ""),
            ("os.getenv('HOST')", ""),
            ("environment.getenv('HOST')", "\nimport os as environment\n"),
        ):
            widened = (
                source.replace(
                    "if os.environ.get(key) == literal",
                    f"if os.environ.get(key) == literal or {extra}",
                )
                + imported
            )
            assert widened != source
            assert not _placeholder_keys_read(widened), extra

    @pytest.mark.parametrize(
        "added",
        [
            "def _extra():\n    return os.environ.get('HOST')\n",
            "def _extra():\n    return os.environ['HOST']\n",
            "def _extra():\n    return os.getenv('HOST')\n",
            "def _extra():\n    from os import environ\n    return environ\n",
            "VALUE = os.environ.copy()\n",
            "import os as environment\n\ndef _extra():\n"
            "    return environment.getenv('PROXY_SERVER')\n",
            "def _extra():\n    from os import getenv as read\n"
            "    return read('HOST')\n",
        ],
    )
    def test_a_second_raw_read_is_caught(self, added: str):
        assert _raw_reads(self._source() + "\n" + added) - self.ALLOWED
