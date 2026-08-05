import logging

import pytest

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
        assert config.eager_full_chromium is False

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
    """The endpoint has no authentication, so the bind address is the guard.

    The warning used to fire only for a literal 0.0.0.0 or ::, which meant the
    most likely way to expose a session by accident, naming a LAN address
    outright, happened in silence.
    """

    @staticmethod
    def _validate_with_host(host, caplog):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.host = host
        with caplog.at_level(
            logging.WARNING, logger="linkedin_mcp_server.config.schema"
        ):
            config.validate()
        return caplog.text

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_binds_are_silent(self, host, caplog):
        assert self._validate_with_host(host, caplog) == ""

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "::",
            "192.168.1.5",  # a LAN address warned about nothing before
            "10.0.0.7",
            "example.internal",
        ],
    )
    def test_reachable_binds_warn(self, host, caplog):
        text = self._validate_with_host(host, caplog)
        assert host in text
        assert "no authentication" in text

    def test_stdio_never_warns_about_the_host(self, caplog):
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
    def test_get_config_returns_same_instance(self, monkeypatch):
        # Mock sys.argv to prevent argparse from parsing pytest's arguments
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        from linkedin_mcp_server.config import get_config

        assert get_config() is get_config()

    def test_reset_config_clears_singleton(self, monkeypatch):
        # Mock sys.argv to prevent argparse from parsing pytest's arguments
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
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

        assert load_from_env(AppConfig()).browser.user_agent is None

    def test_cli_user_agent_refuses_to_start(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--user-agent", "CustomAgent/1.0"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(ConfigurationError, match="--user-agent"):
            load_from_args(AppConfig())


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

    def test_load_from_args_tool_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--tool-timeout", "7.5"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.server.tool_timeout_seconds == 7.5

    @pytest.mark.parametrize("bad_value", ["0", "-1", "abc", "nan", "inf"])
    def test_load_from_args_invalid_tool_timeout(self, monkeypatch, bad_value):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--tool-timeout", bad_value]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(AppConfig())

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
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--login-timeout", "900"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.login_timeout_seconds == 900.0

    def test_load_from_args_login_inline_wait(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--login-inline-wait", "12"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.login_inline_wait_seconds == 12.0

    def test_load_from_args_login_inline_wait_zero(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--login-inline-wait", "0"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
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
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", flag, "8"])
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
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
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--no-auto-import"])
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.auto_import_from_browser is False

    def test_load_from_args_auto_import(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--auto-import"])
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.auto_import_from_browser is True

    def test_args_no_auto_import_overrides_env_true(self, monkeypatch):
        monkeypatch.setenv("AUTO_IMPORT_FROM_BROWSER", "true")
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--no-auto-import"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.browser.auto_import_from_browser is False

    def test_absent_args_keep_env_false(self, monkeypatch):
        monkeypatch.setenv("AUTO_IMPORT_FROM_BROWSER", "false")
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.browser.auto_import_from_browser is False

    def test_absent_args_and_env_keep_none(self, monkeypatch):
        monkeypatch.delenv("AUTO_IMPORT_FROM_BROWSER", raising=False)
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.browser.auto_import_from_browser is None

    def test_eager_full_chromium_default_is_false(self):
        assert BrowserConfig().eager_full_chromium is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("false", False), ("true", True), ("0", False), ("1", True)],
    )
    def test_load_from_env_eager_full_chromium(self, monkeypatch, value, expected):
        monkeypatch.setenv("EAGER_FULL_CHROMIUM", value)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.browser.eager_full_chromium is expected

    def test_load_from_args_eager_full_chromium(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--eager-full-chromium"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.eager_full_chromium is True

    def test_load_from_args_no_eager_full_chromium(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--no-eager-full-chromium"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = AppConfig()
        config.browser.eager_full_chromium = True
        config = load_from_args(config)
        assert config.browser.eager_full_chromium is False

    def test_no_eager_flag_overrides_env_true(self, monkeypatch):
        monkeypatch.setenv("EAGER_FULL_CHROMIUM", "true")
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--no-eager-full-chromium"]
        )
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.browser.eager_full_chromium is False

    def test_eager_full_chromium_absent_keeps_default(self, monkeypatch):
        monkeypatch.delenv("EAGER_FULL_CHROMIUM", raising=False)
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.browser.eager_full_chromium is False

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
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--daemon"])
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.server.daemon_enabled is True

    def test_load_from_args_no_daemon(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--no-daemon"])
        from linkedin_mcp_server.config.loaders import load_from_args

        config = AppConfig()
        config.server.daemon_enabled = True
        config = load_from_args(config)
        assert config.server.daemon_enabled is False

    def test_no_daemon_flag_overrides_env_true(self, monkeypatch):
        # The way out for someone whose environment enables the daemon and who
        # needs one process back on its own browser.
        monkeypatch.setenv("DAEMON_ENABLED", "true")
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server", "--no-daemon"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
        assert config.server.daemon_enabled is False

    def test_daemon_enabled_absent_keeps_default(self, monkeypatch):
        monkeypatch.delenv("DAEMON_ENABLED", raising=False)
        monkeypatch.setattr("sys.argv", ["linkedin-mcp-server"])
        from linkedin_mcp_server.config import load_config

        config = load_config()
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

    def test_load_from_env_import_from_browser(self, monkeypatch):
        monkeypatch.setenv("IMPORT_FROM_BROWSER", "brave")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.import_from_browser == "brave"

    def test_load_from_args_import_from_browser_value(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--import-from-browser", "chrome"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.server.import_from_browser == "chrome"

    def test_load_from_args_import_from_browser_bare_flag_is_auto(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--import-from-browser"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.server.import_from_browser == "auto"

    def test_load_from_args_import_from_browser_empty_is_auto(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--import-from-browser="]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
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
        monkeypatch.setattr(
            "sys.argv",
            [
                "linkedin-mcp-server",
                "--proxy-server",
                "http://cli.example:3128",
                "--proxy-username",
                "cliuser",
                "--proxy-bypass",
                ".internal",
            ],
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        config = load_from_args(AppConfig())
        assert config.browser.proxy_server == "http://cli.example:3128"
        assert config.browser.proxy_username == "cliuser"
        assert config.browser.proxy_bypass == ".internal"

    def test_args_override_env(self, monkeypatch):
        monkeypatch.setenv("PROXY_SERVER", "http://env.example:7000")
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--proxy-server", "http://cli:3128"]
        )
        from linkedin_mcp_server.config.loaders import load_from_args, load_from_env

        config = load_from_args(load_from_env(AppConfig()))
        assert config.browser.proxy_server == "http://cli:3128"

    def test_cli_rejects_embedded_credentials(self, monkeypatch, capsys):
        # There is no --proxy-password flag because argv is world-readable;
        # accepting a credential URL here would hand that exposure back.
        monkeypatch.setattr(
            "sys.argv",
            ["linkedin-mcp-server", "--proxy-server", f"http://u:{self.SECRET}@h:8080"],
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(AppConfig())
        assert self.SECRET not in capsys.readouterr().err


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
        monkeypatch.setattr(
            "sys.argv", ["linkedin-mcp-server", "--proxy-server", self.ENCODED]
        )
        from linkedin_mcp_server.config.loaders import load_from_args

        with pytest.raises(SystemExit):
            load_from_args(AppConfig())
        assert "user%3Apass" not in capsys.readouterr().err

    def test_an_ordinary_address_is_unaffected(self):
        config = BrowserConfig(proxy_server="http://gate.example:7000")
        config.validate()
        assert config.proxy_server == "http://gate.example:7000"
