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
        assert config.mcp_auth_mode == "none"
        assert config.mcp_auth_enabled is False
        assert config.mcp_bearer_token is None


class TestAppConfig:
    def test_validate_invalid_port(self):
        config = AppConfig()
        config.server.port = 99999
        with pytest.raises(ConfigurationError):
            config.validate()

    def test_validate_streamable_http_auth_requires_token(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.mcp_auth_mode = "bearer"
        config.server.mcp_bearer_token = None
        with pytest.raises(ConfigurationError, match="MCP_BEARER_TOKEN"):
            config.validate()

    def test_validate_streamable_http_oauth_requires_settings(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.mcp_auth_mode = "oauth"
        with pytest.raises(ConfigurationError, match="MCP_OAUTH_BASE_URL"):
            config.validate()

    def test_validate_streamable_http_oauth_requires_redirect_uris(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.mcp_auth_mode = "oauth"
        config.server.mcp_oauth_base_url = "https://example.com"
        config.server.mcp_oauth_client_id = "cid"
        config.server.mcp_oauth_client_secret = "secret"
        with pytest.raises(
            ConfigurationError,
            match="MCP_OAUTH_ALLOWED_REDIRECT_URIS",
        ):
            config.validate()

    def test_validate_oauth_refresh_ttl_must_be_gte_access_ttl(self):
        config = AppConfig()
        config.server.transport = "streamable-http"
        config.server.mcp_auth_mode = "oauth"
        config.server.mcp_oauth_base_url = "https://example.com"
        config.server.mcp_oauth_client_id = "cid"
        config.server.mcp_oauth_client_secret = "secret"
        config.server.mcp_oauth_allowed_redirect_uris = [
            "https://claude.ai/api/mcp/auth_callback"
        ]
        config.server.mcp_oauth_token_ttl_seconds = 86400
        config.server.mcp_oauth_refresh_token_ttl_seconds = 3600
        with pytest.raises(
            ConfigurationError,
            match="MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS",
        ):
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

    def test_load_from_env_mcp_auth_enabled_true(self, monkeypatch):
        monkeypatch.delenv("MCP_AUTH_MODE", raising=False)
        monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.mcp_auth_enabled is True
        assert config.server.mcp_auth_mode == "bearer"

    def test_load_from_env_mcp_auth_enabled_false(self, monkeypatch):
        monkeypatch.delenv("MCP_AUTH_MODE", raising=False)
        monkeypatch.setenv("MCP_AUTH_ENABLED", "off")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.mcp_auth_enabled is False
        assert config.server.mcp_auth_mode == "none"

    def test_load_from_env_invalid_mcp_auth_enabled(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTH_ENABLED", "sometimes")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(
            ConfigurationError,
            match="Invalid MCP_AUTH_ENABLED",
        ):
            load_from_env(AppConfig())

    def test_load_from_env_mcp_bearer_token(self, monkeypatch):
        monkeypatch.setenv("MCP_BEARER_TOKEN", "  super-secret  ")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.mcp_bearer_token == "super-secret"

    def test_load_from_env_mcp_auth_mode_oauth(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.mcp_auth_mode == "oauth"
        assert config.server.mcp_auth_enabled is True

    def test_load_from_env_invalid_mcp_auth_mode(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTH_MODE", "legacy")
        from linkedin_mcp_server.config.loaders import load_from_env

        with pytest.raises(ConfigurationError, match="Invalid MCP_AUTH_MODE"):
            load_from_env(AppConfig())

    def test_load_from_env_mcp_oauth_settings(self, monkeypatch):
        monkeypatch.setenv("MCP_AUTH_MODE", "oauth")
        monkeypatch.setenv("MCP_OAUTH_BASE_URL", "https://example.com")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_ID", "cid")
        monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("MCP_OAUTH_TOKEN_TTL_SECONDS", "1200")
        monkeypatch.setenv("MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "604800")
        monkeypatch.setenv(
            "MCP_OAUTH_ALLOWED_REDIRECT_URIS",
            "https://claude.ai/api/mcp/auth_callback, https://example.com/cb",
        )
        from linkedin_mcp_server.config.loaders import load_from_env

        config = load_from_env(AppConfig())
        assert config.server.mcp_oauth_base_url == "https://example.com"
        assert config.server.mcp_oauth_client_id == "cid"
        assert config.server.mcp_oauth_client_secret == "secret"
        assert config.server.mcp_oauth_token_ttl_seconds == 1200
        assert config.server.mcp_oauth_refresh_token_ttl_seconds == 604800
        assert config.server.mcp_oauth_allowed_redirect_uris == [
            "https://claude.ai/api/mcp/auth_callback",
            "https://example.com/cb",
        ]
