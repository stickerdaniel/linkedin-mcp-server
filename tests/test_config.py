import pytest

from linkedin_mcp_server.config.schema import (
    AppConfig,
    BrowserConfig,
    ConfigurationError,
    OAuthConfig,
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


class TestAppConfig:
    def test_validate_invalid_port(self):
        config = AppConfig()
        config.server.port = 99999
        with pytest.raises(ConfigurationError):
            config.validate()


class TestOAuthConfig:
    def test_defaults(self):
        config = OAuthConfig()
        assert config.enabled is False
        assert config.base_url is None
        assert config.password is None

    def test_validate_requires_base_url_when_enabled(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(enabled=True, password="secret")
        with pytest.raises(ConfigurationError, match="OAUTH_BASE_URL"):
            config.validate()

    def test_validate_requires_password_when_enabled(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(enabled=True, base_url="https://example.com")
        with pytest.raises(ConfigurationError, match="OAUTH_PASSWORD"):
            config.validate()

    def test_validate_passes_when_fully_configured(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(
            enabled=True, base_url="https://example.com", password="secret"
        )
        config.validate()  # No error

    def test_validate_requires_streamable_http_transport(self):
        config = AppConfig()
        config.server.transport = "stdio"
        config.server.oauth = OAuthConfig(
            enabled=True, base_url="https://example.com", password="secret"
        )
        with pytest.raises(ConfigurationError, match="streamable-http"):
            config.validate()

    def test_validate_rejects_http_base_url(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(
            enabled=True, base_url="http://example.com", password="secret"
        )
        with pytest.raises(ConfigurationError, match="HTTPS"):
            config.validate()

    def test_validate_rejects_base_url_with_path(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(
            enabled=True, base_url="https://example.com/api", password="secret"
        )
        with pytest.raises(ConfigurationError, match="path component"):
            config.validate()

    def test_validate_accepts_base_url_with_trailing_slash(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.oauth = OAuthConfig(
            enabled=True, base_url="https://example.com/", password="secret"
        )
        config.validate()  # No error — trailing slash is fine

    def test_validate_passes_when_disabled(self):
        config = AppConfig()
        config.server.oauth = OAuthConfig(enabled=False)
        config.validate()  # No error

    @pytest.mark.parametrize("flag", ["login", "status", "logout"])
    def test_validate_skips_oauth_in_command_only_modes(self, flag):
        """OAuth validation should not block --login, --status, --logout."""
        config = AppConfig()
        config.server.oauth = OAuthConfig(enabled=True)  # Missing base_url + password
        setattr(config.server, flag, True)
        config.validate()  # No error — skipped for command-only modes


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

    def test_load_from_env_log_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
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

    def test_load_from_env_oauth_enabled(self, monkeypatch):
        monkeypatch.setenv("AUTH", "oauth")
        monkeypatch.setenv("OAUTH_BASE_URL", "https://example.com")
        monkeypatch.setenv("OAUTH_PASSWORD", "secret123")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.oauth.enabled is True
        assert config.server.oauth.base_url == "https://example.com"
        assert config.server.oauth.password == "secret123"

    def test_load_from_env_oauth_disabled_by_default(self, monkeypatch):
        for var in ["AUTH", "OAUTH_BASE_URL", "OAUTH_PASSWORD"]:
            monkeypatch.delenv(var, raising=False)
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.oauth.enabled is False

    def test_load_from_env_invalid_auth_mode(self, monkeypatch):
        monkeypatch.setenv("AUTH", "invalid")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid AUTH"):
            load_from_env(AppConfig())
