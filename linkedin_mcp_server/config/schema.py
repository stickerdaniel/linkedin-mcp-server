"""
Configuration schema definitions for LinkedIn MCP Server.

Defines the dataclass schemas that represent the application's configuration
structure with type-safe configuration objects and default values.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""


@dataclass
class BrowserConfig:
    """Configuration for browser settings."""

    headless: bool = True
    slow_mo: int = 0  # Milliseconds between browser actions (debugging)
    user_agent: str | None = None  # Custom browser user agent
    viewport_width: int = 1280
    viewport_height: int = 720
    default_timeout: int = 5000  # Milliseconds for page operations
    chrome_path: str | None = None  # Path to Chrome/Chromium executable
    user_data_dir: str = "~/.linkedin-mcp/profile"  # Persistent browser profile

    def validate(self) -> None:
        """Validate browser configuration values."""
        if self.slow_mo < 0:
            raise ConfigurationError(
                f"slow_mo must be non-negative, got {self.slow_mo}"
            )
        if self.default_timeout <= 0:
            raise ConfigurationError(
                f"default_timeout must be positive, got {self.default_timeout}"
            )
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ConfigurationError(
                f"viewport dimensions must be positive, got {self.viewport_width}x{self.viewport_height}"
            )
        if self.chrome_path:
            chrome_path = Path(self.chrome_path)
            if not chrome_path.exists():
                raise ConfigurationError(
                    f"chrome_path '{self.chrome_path}' does not exist"
                )
            if not chrome_path.is_file():
                raise ConfigurationError(
                    f"chrome_path '{self.chrome_path}' is not a file"
                )


@dataclass
class ServerConfig:
    """MCP server configuration."""

    transport: Literal["stdio", "streamable-http"] = "stdio"
    transport_explicitly_set: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    login: bool = False
    status: bool = False  # Check session validity and exit
    logout: bool = False
    # HTTP transport configuration
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    mcp_auth_mode: Literal["none", "bearer", "oauth", "multi"] = "none"
    mcp_auth_enabled: bool = False
    mcp_bearer_token: str | None = None
    mcp_oauth_base_url: str | None = None
    mcp_oauth_client_id: str | None = None
    mcp_oauth_client_secret: str | None = None
    mcp_oauth_token_ttl_seconds: int = 86400
    mcp_oauth_refresh_token_ttl_seconds: int = 604800
    mcp_oauth_allowed_redirect_uris: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    """Main application configuration."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    is_interactive: bool = field(default=False)

    def validate(self) -> None:
        """Validate all configuration values. Call after modifying config."""
        self.browser.validate()
        if self.server.transport == "streamable-http":
            self._validate_transport_config()
            self._validate_path_format()
        self._validate_port_range()

    def _validate_transport_config(self) -> None:
        """Validate transport configuration is consistent."""
        if not self.server.host:
            raise ConfigurationError("HTTP transport requires a valid host")
        if not self.server.port:
            raise ConfigurationError("HTTP transport requires a valid port")
        if self.server.mcp_auth_mode in {"bearer", "multi"} and (
            not self.server.mcp_bearer_token
        ):
            raise ConfigurationError(
                f"MCP_AUTH_MODE={self.server.mcp_auth_mode} requires "
                "MCP_BEARER_TOKEN to be set."
            )
        if self.server.mcp_auth_mode in {"oauth", "multi"}:
            if not self.server.mcp_oauth_base_url:
                raise ConfigurationError(
                    f"MCP_AUTH_MODE={self.server.mcp_auth_mode} requires "
                    "MCP_OAUTH_BASE_URL to be set."
                )
            if not self.server.mcp_oauth_client_id:
                raise ConfigurationError(
                    f"MCP_AUTH_MODE={self.server.mcp_auth_mode} requires "
                    "MCP_OAUTH_CLIENT_ID to be set."
                )
            if not self.server.mcp_oauth_client_secret:
                raise ConfigurationError(
                    f"MCP_AUTH_MODE={self.server.mcp_auth_mode} requires "
                    "MCP_OAUTH_CLIENT_SECRET to be set."
                )
            if not self.server.mcp_oauth_allowed_redirect_uris:
                raise ConfigurationError(
                    f"MCP_AUTH_MODE={self.server.mcp_auth_mode} requires "
                    "MCP_OAUTH_ALLOWED_REDIRECT_URIS to be set."
                )
            if self.server.mcp_oauth_token_ttl_seconds <= 0:
                raise ConfigurationError(
                    "MCP_OAUTH_TOKEN_TTL_SECONDS must be positive."
                )
            if self.server.mcp_oauth_refresh_token_ttl_seconds <= 0:
                raise ConfigurationError(
                    "MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS must be positive."
                )
            if (
                self.server.mcp_oauth_refresh_token_ttl_seconds
                < self.server.mcp_oauth_token_ttl_seconds
            ):
                raise ConfigurationError(
                    "MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS must be >= "
                    "MCP_OAUTH_TOKEN_TTL_SECONDS."
                )
        if self.server.host in ("0.0.0.0", "::") and (
            self.server.mcp_auth_mode == "none"
        ):
            logger.warning(
                "HTTP transport is binding to %s which exposes the server to "
                "all network interfaces. The MCP endpoint has no authentication "
                "— anyone on your network can use your LinkedIn session. "
                "Use 127.0.0.1 (default) unless you understand the risk.",
                self.server.host,
            )

    def _validate_port_range(self) -> None:
        """Validate port is in valid range."""
        if not (1 <= self.server.port <= 65535):
            raise ConfigurationError(
                f"Port {self.server.port} is not in valid range (1-65535)"
            )

    def _validate_path_format(self) -> None:
        """Validate path format for HTTP transport."""
        if not self.server.path.startswith("/"):
            raise ConfigurationError(
                f"HTTP path '{self.server.path}' must start with '/'"
            )
        if len(self.server.path) < 2:
            raise ConfigurationError(
                f"HTTP path '{self.server.path}' must be at least 2 characters"
            )
