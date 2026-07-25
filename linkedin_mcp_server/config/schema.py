"""
Configuration schema definitions for LinkedIn MCP Server.

Defines the dataclass schemas that represent the application's configuration
structure with type-safe configuration objects and default values.
"""

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

DEFAULT_TOOL_TIMEOUT_SECONDS: float = 180.0
DEFAULT_LOGIN_TIMEOUT_SECONDS: float = 1800.0  # 30 min; 0 = no limit
DEFAULT_LOGIN_INLINE_WAIT_SECONDS: float = 25.0  # bounded inline wait
# Clamp ceiling: scrape time stacks on top of the inline wait inside one tool
# call and the smallest MCP client timeout is ~60s, so the wait alone must stay
# well under that floor.
MAX_LOGIN_INLINE_WAIT_SECONDS: float = 45.0


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
    # Manual-login wait timeout in seconds; 0 = unlimited
    login_timeout_seconds: float = DEFAULT_LOGIN_TIMEOUT_SECONDS
    # Bounded inline wait before the pending signal; 0 = immediate return
    login_inline_wait_seconds: float = DEFAULT_LOGIN_INLINE_WAIT_SECONDS
    # Auto-import a LinkedIn session from a locally logged-in browser on the
    # first no-session tool call, before falling back to manual login. On by
    # default: None ("auto") and True both enable it across interactive and
    # non-interactive desktop runs on every platform. False disables it. No
    # effect under Docker (no host browser/keychain) or on a non-loopback HTTP
    # bind (a network-exposed server must not read the host browser cookie).
    # Note the non-loopback gate covers network-exposed HTTP only, not
    # stdio-over-SSH: a non-console session simply fails to read the local
    # user's keychain and degrades to manual login, and no cookie crosses the
    # network.
    auto_import_from_browser: bool | None = None
    # Install full Chrome for Testing up front during background setup instead
    # of lazily on the first headed login. Off by default: the headless scrape +
    # auto-import path needs only the headless shell, so a headless-only operator
    # never downloads the larger full-chromium binary unless interactive login is
    # actually triggered. Set True to pre-warm the headed login fallback.
    eager_full_chromium: bool = False

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
        # 0 is a valid sentinel for both (unlimited login wait / no inline wait),
        # so these use >= 0 rather than the > 0 check tool_timeout_seconds uses.
        if not (
            math.isfinite(self.login_timeout_seconds)
            and self.login_timeout_seconds >= 0
        ):
            raise ConfigurationError(
                "login_timeout_seconds must be a non-negative finite number, "
                f"got {self.login_timeout_seconds}"
            )
        if not (
            math.isfinite(self.login_inline_wait_seconds)
            and self.login_inline_wait_seconds >= 0
        ):
            raise ConfigurationError(
                "login_inline_wait_seconds must be a non-negative finite number, "
                f"got {self.login_inline_wait_seconds}"
            )
        # Clamp (do not reject) so a misconfigured large value can never alone
        # approach the client timeout floor once scrape time is added on top.
        if self.login_inline_wait_seconds > MAX_LOGIN_INLINE_WAIT_SECONDS:
            logger.warning(
                "login_inline_wait_seconds %.1f exceeds the %.1fs ceiling; "
                "clamping (scrape time stacks on top of the wait inside one "
                "tool call).",
                self.login_inline_wait_seconds,
                MAX_LOGIN_INLINE_WAIT_SECONDS,
            )
            self.login_inline_wait_seconds = MAX_LOGIN_INLINE_WAIT_SECONDS
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
    # Browser key or "auto"; triggers import-from-browser-and-exit.
    import_from_browser: str | None = None
    # HTTP transport configuration
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    def validate(self) -> None:
        """Validate server configuration values."""
        if not (
            math.isfinite(self.tool_timeout_seconds) and self.tool_timeout_seconds > 0
        ):
            raise ConfigurationError(
                f"tool_timeout_seconds must be a positive finite number, got {self.tool_timeout_seconds}"
            )
        if self.import_from_browser is not None:
            # Import the submodule, NOT the package, to avoid a config ->
            # browser_import -> drivers.browser -> config import cycle.
            from linkedin_mcp_server.browser_import.discovery import SUPPORTED_BROWSERS

            allowed = set(SUPPORTED_BROWSERS) | {"auto"}
            if self.import_from_browser not in allowed:
                raise ConfigurationError(
                    "import_from_browser "
                    f"'{self.import_from_browser}' is not supported. "
                    f"Choose one of: {', '.join(sorted(allowed))}"
                )


@dataclass
class AppConfig:
    """Main application configuration."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    is_interactive: bool = field(default=False)

    def validate(self) -> None:
        """Validate all configuration values. Call after modifying config."""
        self.browser.validate()
        self.server.validate()
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
        if self.server.host in ("0.0.0.0", "::"):
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
