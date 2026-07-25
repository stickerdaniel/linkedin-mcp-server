import pytest

from linkedin_mcp_server.config.schema import (
    AppConfig,
    BrowserConfig,
    ConfigurationError,
    MAX_LOGIN_INLINE_WAIT_SECONDS,
    ServerConfig,
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
