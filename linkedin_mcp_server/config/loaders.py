"""Configuration schema, loading, and argument parsing for LinkedIn MCP Server."""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TRUTHY_VALUES = ("1", "true", "True", "yes", "Yes")
FALSY_VALUES = ("0", "false", "False", "no", "No")


class ConfigurationError(Exception):
    """Raised when configuration validation fails."""


@dataclass
class BrowserConfig:
    """Browser settings."""

    headless: bool = True
    slow_mo: int = 0
    user_agent: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 720
    default_timeout: int = 5000
    chrome_path: str | None = None
    user_data_dir: str = "~/.linkedin-mcp/profile"

    def validate(self) -> None:
        if self.slow_mo < 0:
            raise ConfigurationError(f"slow_mo must be non-negative, got {self.slow_mo}")
        if self.default_timeout <= 0:
            raise ConfigurationError(
                f"default_timeout must be positive, got {self.default_timeout}"
            )
        if self.viewport_width <= 0 or self.viewport_height <= 0:
            raise ConfigurationError(
                f"viewport must be positive, got {self.viewport_width}x{self.viewport_height}"
            )
        if self.chrome_path:
            path = Path(self.chrome_path)
            if not path.exists():
                raise ConfigurationError(f"chrome_path '{self.chrome_path}' does not exist")
            if not path.is_file():
                raise ConfigurationError(f"chrome_path '{self.chrome_path}' is not a file")


@dataclass
class ServerConfig:
    """MCP server configuration."""

    transport: Literal["stdio", "streamable-http"] = "stdio"
    transport_explicitly_set: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    login: bool = False
    login_serve: bool = False
    status: bool = False
    logout: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"


@dataclass
class AppConfig:
    """Main application configuration."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    is_interactive: bool = field(default=False)

    def validate(self) -> None:
        self.browser.validate()
        if self.server.transport == "streamable-http":
            if not self.server.host:
                raise ConfigurationError("HTTP transport requires a valid host")
            if not self.server.port:
                raise ConfigurationError("HTTP transport requires a valid port")
            if not self.server.path.startswith("/") or len(self.server.path) < 2:
                raise ConfigurationError(
                    f"HTTP path '{self.server.path}' must start with '/' and be >= 2 chars"
                )
        if not (1 <= self.server.port <= 65535):
            raise ConfigurationError(f"Port {self.server.port} not in range 1-65535")


def load_from_env(config: AppConfig) -> AppConfig:
    """Load configuration from environment variables."""
    if log_level_env := os.environ.get("LOG_LEVEL"):
        log_level_upper = log_level_env.upper()
        if log_level_upper in ("DEBUG", "INFO", "WARNING", "ERROR"):
            config.server.log_level = cast(
                "Literal['DEBUG', 'INFO', 'WARNING', 'ERROR']", log_level_upper
            )

    if os.environ.get("HEADLESS") in FALSY_VALUES:
        config.browser.headless = False
    elif os.environ.get("HEADLESS") in TRUTHY_VALUES:
        config.browser.headless = True

    if transport_env := os.environ.get("TRANSPORT"):
        config.server.transport_explicitly_set = True
        if transport_env in ("stdio", "streamable-http"):
            config.server.transport = cast("Literal['stdio', 'streamable-http']", transport_env)
        else:
            raise ConfigurationError(
                f"Invalid TRANSPORT: '{transport_env}'. Must be 'stdio' or 'streamable-http'."
            )

    if user_data_dir := os.environ.get("USER_DATA_DIR"):
        config.browser.user_data_dir = user_data_dir

    if timeout_env := os.environ.get("TIMEOUT"):
        try:
            config.browser.default_timeout = int(timeout_env)
        except ValueError as err:
            raise ConfigurationError(
                f"Invalid TIMEOUT: '{timeout_env}'. Must be an integer."
            ) from err

    if user_agent_env := os.environ.get("USER_AGENT"):
        config.browser.user_agent = user_agent_env

    if host_env := os.environ.get("HOST"):
        config.server.host = host_env

    if port_env := os.environ.get("PORT"):
        try:
            config.server.port = int(port_env)
        except ValueError as err:
            raise ConfigurationError(f"Invalid PORT: '{port_env}'. Must be an integer.") from err

    if path_env := os.environ.get("HTTP_PATH"):
        config.server.path = path_env

    if slow_mo_env := os.environ.get("SLOW_MO"):
        try:
            config.browser.slow_mo = int(slow_mo_env)
        except ValueError as err:
            raise ConfigurationError(
                f"Invalid SLOW_MO: '{slow_mo_env}'. Must be an integer."
            ) from err

    if viewport_env := os.environ.get("VIEWPORT"):
        try:
            width, height = viewport_env.lower().split("x")
            config.browser.viewport_width = int(width)
            config.browser.viewport_height = int(height)
        except ValueError as err:
            raise ConfigurationError(
                f"Invalid VIEWPORT: '{viewport_env}'. Must be WxH (e.g., 1280x720)."
            ) from err

    if chrome_path_env := os.environ.get("CHROME_PATH"):
        config.browser.chrome_path = chrome_path_env

    return config


def load_from_args(config: AppConfig) -> AppConfig:
    """Load configuration from command line arguments."""
    parser = argparse.ArgumentParser(
        description="LinkedIn MCP Server - Model Context Protocol server for LinkedIn"
    )
    parser.add_argument("--no-headless", action="store_true", help="Show browser window")
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level"
    )
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default=None, help="Transport mode"
    )
    parser.add_argument("--host", type=str, default=None, help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: 8000)")
    parser.add_argument("--path", type=str, default=None, help="HTTP path (default: /mcp)")
    parser.add_argument("--slow-mo", type=int, default=0, metavar="MS", help="Action delay (ms)")
    parser.add_argument("--user-agent", type=str, default=None, help="Custom user agent")
    parser.add_argument("--viewport", type=str, default=None, metavar="WxH", help="Viewport size")
    parser.add_argument("--timeout", type=int, default=None, metavar="MS", help="Timeout (ms)")
    parser.add_argument("--chrome-path", type=str, default=None, metavar="PATH", help="Chrome path")
    parser.add_argument("--login", action="store_true", help="Login interactively")
    parser.add_argument(
        "--login-serve",
        action="store_true",
        help="Login interactively then start MCP server with same browser",
    )
    parser.add_argument("--status", action="store_true", help="Check session validity")
    parser.add_argument("--logout", action="store_true", help="Clear browser profile")
    parser.add_argument(
        "--user-data-dir", type=str, default=None, metavar="PATH", help="Profile directory"
    )

    args = parser.parse_args()

    if args.no_headless:
        config.browser.headless = False
    if args.log_level:
        config.server.log_level = args.log_level
    if args.transport:
        config.server.transport = args.transport
        config.server.transport_explicitly_set = True
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.path:
        config.server.path = args.path
    if args.slow_mo:
        config.browser.slow_mo = args.slow_mo
    if args.user_agent:
        config.browser.user_agent = args.user_agent
    if args.viewport:
        try:
            width, height = args.viewport.lower().split("x")
            config.browser.viewport_width = int(width)
            config.browser.viewport_height = int(height)
        except ValueError as err:
            raise ConfigurationError(
                f"Invalid --viewport: '{args.viewport}'. Must be WxH (e.g., 1280x720)."
            ) from err
    if args.timeout is not None:
        config.browser.default_timeout = args.timeout
    if args.chrome_path:
        config.browser.chrome_path = args.chrome_path
    if args.login:
        config.server.login = True
    if args.login_serve:
        config.server.login_serve = True
    if args.status:
        config.server.status = True
    if args.logout:
        config.server.logout = True
    if args.user_data_dir:
        config.browser.user_data_dir = args.user_data_dir

    return config


def load_config() -> AppConfig:
    """Load configuration: defaults -> env vars -> CLI args."""
    config = AppConfig()

    try:
        config.is_interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        config.is_interactive = False

    config = load_from_env(config)
    config = load_from_args(config)
    config.validate()
    return config
