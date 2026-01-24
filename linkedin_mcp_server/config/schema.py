"""
Configuration schema definitions for LinkedIn MCP Server.

Defines the dataclass schemas that represent the application's configuration
structure with type-safe configuration objects and default values.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""

    pass


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
    get_session: bool = False
    session_output_path: str | None = None
    session_info: bool = False  # Check session validity and exit
    clear_session: bool = False
    # HTTP transport configuration
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    # Cookie authentication
    linkedin_cookie: str | None = None


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
