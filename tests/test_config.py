import pytest

from linkedin_mcp_server.config.schema import (
    AppConfig,
    BrowserConfig,
    ConfigurationError,
    ServerConfig,
)


class TestBrowserConfig:
    def test_defaults(self):
        config = BrowserConfig()
        assert config.headless is True
        assert config.default_timeout == 5000
        assert config.user_data_dir == "~/.linkedin-mcp/profile"

    def test_validate_passes(self):
        BrowserConfig().validate()  # No error

    def test_validate_negative_timeout(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(default_timeout=-1).validate()

    def test_validate_negative_slow_mo(self):
        with pytest.raises(ConfigurationError):
            BrowserConfig(slow_mo=-1).validate()


class TestServerConfig:
    def test_defaults(self):
        config = ServerConfig()
        assert config.transport == "stdio"
        assert config.port == 8000
        assert config.tool_timeout_seconds == 90.0

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
