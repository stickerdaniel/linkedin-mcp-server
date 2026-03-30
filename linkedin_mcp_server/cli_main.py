"""LinkedIn MCP Server CLI entry point."""

import asyncio
import logging
import sys
from typing import Literal

import inquirer

from linkedin_mcp_server.authentication import clear_auth_state
from linkedin_mcp_server.bootstrap import configure_browser_environment
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core import AuthenticationError
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    get_or_create_browser,
    get_profile_dir,
    profile_exists,
    set_headless,
)
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.session_state import (
    load_source_state,
    portable_cookie_path,
    source_state_path,
)
from linkedin_mcp_server.setup import run_profile_creation

logger = logging.getLogger(__name__)


def _configure_and_log(config, mode: str) -> str:
    """Common setup for CLI subcommands. Returns version string."""
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    version = _get_version()
    logger.info("LinkedIn MCP Server v%s - %s", version, mode)
    return version


def clear_profile_and_exit() -> None:
    """Clear LinkedIn browser profile and exit."""
    config = get_config()
    _configure_and_log(config, "Profile Clear")

    profile_dir = get_profile_dir()
    if not (
        profile_exists(profile_dir)
        or portable_cookie_path(profile_dir).exists()
        or source_state_path(profile_dir).exists()
    ):
        print("No authentication state found.")
        sys.exit(0)

    print(f"Clear LinkedIn auth state from {profile_dir.parent}?")
    try:
        if input("Are you sure? (y/N): ").strip().lower() not in ("y", "yes"):
            sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)

    if clear_auth_state(profile_dir):
        print("Authentication state cleared.")
        sys.exit(0)
    else:
        print("Failed to clear authentication state.")
        sys.exit(1)


def get_profile_and_exit() -> None:
    """Create profile interactively and exit."""
    config = get_config()
    _configure_and_log(config, "Session Creation")
    sys.exit(0 if run_profile_creation(config.browser.user_data_dir) else 1)


def profile_info_and_exit() -> None:
    """Check profile validity and display info."""
    config = get_config()
    _configure_and_log(config, "Session Info")

    profile_dir = get_profile_dir()
    source_state = load_source_state(profile_dir)

    if not source_state or not profile_exists(profile_dir):
        print(f"No valid session at {profile_dir}. Run with --login.")
        sys.exit(1)

    print(f"Profile: {profile_dir}")
    print(f"Login generation: {source_state.login_generation}")

    async def check() -> bool:
        try:
            set_headless(True)
            browser = await get_or_create_browser()
            return browser.is_authenticated
        except AuthenticationError:
            return False
        finally:
            await close_browser()

    try:
        valid = asyncio.run(check())
    except Exception as e:
        print(f"Could not validate session: {e}")
        sys.exit(1)

    if valid:
        print(f"Session valid ({profile_dir})")
        sys.exit(0)
    print("Session expired. Run with --login.")
    sys.exit(1)


def _get_version() -> str:
    """Version from installed metadata or pyproject.toml."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        for name in ("linkedin-scraper-mcp", "linkedin-mcp-server"):
            try:
                return version(name)
            except PackageNotFoundError:
                continue
    except Exception:
        pass
    try:
        import os
        import tomllib

        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(path, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "unknown"


def main() -> None:
    """Main entry point."""
    config = get_config()
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )
    version = _get_version()

    if config.is_interactive:
        print(f"LinkedIn MCP Server v{version}")

    logger.info("LinkedIn MCP Server v%s", version)
    configure_browser_environment()
    set_headless(config.browser.headless)

    if config.server.logout:
        clear_profile_and_exit()
    if config.server.login:
        get_profile_and_exit()
    if config.server.login_serve:
        pass  # login happens inside the MCP server lifespan handler
    if config.server.status:
        profile_info_and_exit()

    try:
        transport: Literal["stdio", "streamable-http"] = config.server.transport
        if config.is_interactive and not config.server.transport_explicitly_set:
            answers = inquirer.prompt(
                [
                    inquirer.List(
                        "transport",
                        message="Choose transport mode",
                        choices=[("stdio", "stdio"), ("streamable-http", "streamable-http")],
                        default="stdio",
                    )
                ]
            )
            if not answers:
                raise KeyboardInterrupt
            transport = answers["transport"]

        mcp = create_mcp_server()
        if transport == "streamable-http":
            mcp.run(
                transport=transport,
                host=config.server.host,
                port=config.server.port,
                path=config.server.path,
            )
        else:
            mcp.run(transport=transport)
    except KeyboardInterrupt:
        _exit(0)
    except Exception as e:
        logger.exception("Server error: %s", e)
        _exit(1)


def _exit(code: int = 0) -> None:
    """Exit with browser cleanup."""
    try:
        asyncio.run(close_browser())
    except Exception:
        pass
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _exit(0)
    except Exception as e:
        logger.exception("Fatal: %s", e)
        _exit(1)
