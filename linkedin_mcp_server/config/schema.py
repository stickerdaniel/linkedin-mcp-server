"""
Configuration schema definitions for LinkedIn MCP Server.

Defines the dataclass schemas that represent the application's configuration
structure with type-safe configuration objects and default values.
"""

import ipaddress
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

# How long a tool call waits for another process to hand over the browser. Same
# budget and ceiling as the login inline wait, for the same reason: the wait is
# spent inside one tool call, ahead of the scrape itself.
DEFAULT_BROWSER_WAIT_SECONDS: float = 25.0
MAX_BROWSER_WAIT_SECONDS: float = 45.0
# Shortest time an owner keeps the browser before honouring a handoff request.
# Every handoff costs a reopen, and a reopen re-validates /feed/, so handing over
# on literally every call would multiply LinkedIn requests. Matched to the wait
# budget: a longer window would push waiters past their own timeout.
DEFAULT_BROWSER_MIN_HOLD_SECONDS: float = 20.0
# Slack between the end of the hold window and the waiter's deadline: the owner
# notices on a one-second poll and then has to tear Chromium down (~0.7s
# measured). Without it a waiter gives up moments before the handover lands.
BROWSER_HANDOFF_MARGIN_SECONDS: float = 3.0
# Close an idle browser and release the profile after this long with no calls.
# A backstop only — the handoff signal does the real work — so it is deliberately
# long: a reopen costs one more LinkedIn request. 0 disables it.
DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS: float = 600.0


# Names that resolve only on this machine. Compared case-insensitively and
# without a trailing root dot, both of which are the same name to a resolver.
LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"localhost"})


def is_loopback_host(host: str) -> bool:
    """Whether *host* is reachable only from this machine.

    Fails closed: anything this cannot positively identify as loopback counts
    as reachable from elsewhere, whether that is a wildcard bind, a LAN
    address, or a name that needs DNS to answer. Erring that way costs a
    warning and a skipped cookie import; erring the other way would hand a
    logged-in session to the network.

    Address literals go through :mod:`ipaddress` rather than a spelling list,
    so the whole 127/8 range and IPv4-mapped forms are recognised for what they
    are instead of being judged on how they were typed.
    """
    name = host.strip().rstrip(".").lower()
    if not name:
        return False
    if name in LOOPBACK_HOSTNAMES:
        return True

    literal = name
    if literal.startswith("[") and literal.endswith("]"):
        literal = literal[1:-1]  # bracketed IPv6, as it appears in a URL
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        # A name only DNS can resolve, and DNS can point anywhere.
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


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
    # Wait for another process to hand over the browser; 0 = report busy at once
    browser_wait_seconds: float = DEFAULT_BROWSER_WAIT_SECONDS
    # Shortest ownership before a handoff request is honoured; 0 = hand over
    # after every call
    browser_min_hold_seconds: float = DEFAULT_BROWSER_MIN_HOLD_SECONDS
    # Close an idle browser after this long; 0 = keep it until the process exits
    browser_idle_timeout_seconds: float = DEFAULT_BROWSER_IDLE_TIMEOUT_SECONDS
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
        for name in (
            "browser_wait_seconds",
            "browser_min_hold_seconds",
            "browser_idle_timeout_seconds",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ConfigurationError(
                    f"{name} must be a non-negative finite number, got {value}"
                )
        # Clamped for the same reason as the login inline wait: the wait happens
        # inside a tool call, ahead of the scrape.
        if self.browser_wait_seconds > MAX_BROWSER_WAIT_SECONDS:
            logger.warning(
                "browser_wait_seconds %.1f exceeds the %.1fs ceiling; clamping.",
                self.browser_wait_seconds,
                MAX_BROWSER_WAIT_SECONDS,
            )
            self.browser_wait_seconds = MAX_BROWSER_WAIT_SECONDS
        # The hold window has to end far enough inside the wait budget that the
        # owner still notices and finishes closing before the waiter gives up.
        # Equal values are not enough: the owner polls on an interval and then
        # has to tear Chromium down, so it would answer just after the deadline.
        latest_useful_hold = max(
            0.0, self.browser_wait_seconds - BROWSER_HANDOFF_MARGIN_SECONDS
        )
        if self.browser_min_hold_seconds > latest_useful_hold:
            logger.warning(
                "browser_min_hold_seconds %.1f leaves no room inside the %.1fs "
                "wait budget for the owner to notice and close; clamping to "
                "%.1f.",
                self.browser_min_hold_seconds,
                self.browser_wait_seconds,
                latest_useful_hold,
            )
            self.browser_min_hold_seconds = latest_useful_hold
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
        if not is_loopback_host(self.server.host):
            logger.warning(
                "HTTP transport is binding to %s, which is reachable from "
                "outside this machine. The MCP endpoint has no authentication, "
                "so anyone who can reach that address can use your LinkedIn "
                "session. Use 127.0.0.1 (default) unless you understand the "
                "risk.",
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
