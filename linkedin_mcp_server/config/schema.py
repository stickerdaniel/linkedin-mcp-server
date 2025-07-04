# src/linkedin_mcp_server/config/schema.py
from dataclasses import dataclass, field
from typing import Optional, List, Literal


@dataclass
class ChromeConfig:
    """Configuration for Chrome driver."""

    headless: bool = True
    chromedriver_path: Optional[str] = None
    browser_args: List[str] = field(default_factory=list)
    non_interactive: bool = False


@dataclass
class LinkedInConfig:
    """LinkedIn connection configuration."""

    email: Optional[str] = None
    password: Optional[str] = None
    use_keyring: bool = True


@dataclass
class ServerConfig:
    """MCP server configuration."""

    transport: Literal["stdio", "sse"] = "stdio"
    lazy_init: bool = True
    debug: bool = False
    setup: bool = True


@dataclass
class AppConfig:
    """Main application configuration."""

    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    linkedin: LinkedInConfig = field(default_factory=LinkedInConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
