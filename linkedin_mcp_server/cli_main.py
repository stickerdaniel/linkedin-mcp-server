"""
LinkedIn MCP Server - Main CLI application entry point.

Implements a simplified two-phase startup:
1. Authentication Check - Verify browser profile is available
2. Server Runtime - MCP server startup with transport selection
"""

import asyncio
import io
import logging
import sys
from typing import Literal

import inquirer

from linkedin_scraper import is_logged_in
from linkedin_scraper.core.exceptions import AuthenticationError, RateLimitError

from linkedin_mcp_server.authentication import (
    clear_profile,
    get_authentication_source,
)
from linkedin_mcp_server.cli import print_claude_config
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    get_or_create_browser,
    get_profile_dir,
    profile_exists,
    set_headless,
)
from linkedin_mcp_server.exceptions import CredentialsNotFoundError
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.setup import run_interactive_setup, run_profile_creation

logger = logging.getLogger(__name__)


def choose_transport_interactive() -> Literal["stdio", "streamable-http"]:
    """Prompt user for transport mode using inquirer."""
    questions = [
        inquirer.List(
            "transport",
            message="Choose mcp transport mode",
            choices=[
                ("stdio (Default CLI mode)", "stdio"),
                ("streamable-http (HTTP server mode)", "streamable-http"),
            ],
            default="stdio",
        )
    ]
    answers = inquirer.prompt(questions)

    if not answers:
        raise KeyboardInterrupt("Transport selection cancelled by user")

    return answers["transport"]


def clear_profile_and_exit() -> None:
    """Clear LinkedIn browser profile and exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Profile Clear mode")

    profile_dir = get_profile_dir()

    if not profile_exists(profile_dir):
        print("ℹ️  No browser profile found")
        print("Nothing to clear.")
        sys.exit(0)

    print(f"🔑 Clear LinkedIn browser profile from {profile_dir}?")

    try:
        confirmation = (
            input("Are you sure you want to clear the profile? (y/N): ").strip().lower()
        )
        if confirmation not in ("y", "yes"):
            print("❌ Operation cancelled")
            sys.exit(0)
    except KeyboardInterrupt:
        print("\n❌ Operation cancelled")
        sys.exit(0)

    if clear_profile(profile_dir):
        print("✅ LinkedIn browser profile cleared successfully!")
    else:
        print("❌ Failed to clear profile")
        sys.exit(1)

    sys.exit(0)


def get_profile_and_exit() -> None:
    """Create profile interactively and exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Creation mode")

    user_data_dir = config.browser.user_data_dir
    success = run_profile_creation(user_data_dir)

    sys.exit(0 if success else 1)


def profile_info_and_exit() -> None:
    """Check profile validity and display info, then exit."""
    config = get_config()

    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Session Info mode")

    # Check if profile directory exists first
    profile_dir = get_profile_dir()
    if not profile_exists(profile_dir):
        print(f"❌ No browser profile found at {profile_dir}")
        print("   Run with --get-session to create a profile")
        sys.exit(1)

    # Check if session is valid by testing login status
    async def check_session() -> bool:
        try:
            set_headless(True)  # Always check headless
            browser = await get_or_create_browser()
            valid = await is_logged_in(browser.page)
            return valid
        except AuthenticationError:
            return False
        except Exception as e:
            logger.exception(f"Unexpected error checking session: {e}")
            raise
        finally:
            await close_browser()

    try:
        valid = asyncio.run(check_session())
    except Exception as e:
        print(f"❌ Could not validate session: {e}")
        print("   Check logs and browser configuration.")
        sys.exit(1)

    if valid:
        print(f"✅ Session is valid (profile: {profile_dir})")
        sys.exit(0)
    else:
        print(f"❌ Session expired or invalid (profile: {profile_dir})")
        print("   Run with --get-session to re-authenticate")
        sys.exit(1)


def ensure_authentication_ready() -> None:
    """
    Phase 1: Ensure authentication is ready.

    Checks for existing browser profile.
    If not found, runs interactive setup in interactive mode.

    Raises:
        CredentialsNotFoundError: If authentication setup fails
    """
    config = get_config()

    # Check for existing profile
    try:
        get_authentication_source()
        return

    except CredentialsNotFoundError:
        pass

    # No authentication found - try interactive setup if possible
    if not config.is_interactive:
        raise CredentialsNotFoundError(
            "No LinkedIn profile found.\n"
            "Options:\n"
            "  1. Run with --get-session to create a profile\n"
            "  2. Run with --no-headless to login interactively"
        )

    # Run interactive setup
    logger.info("No authentication found, starting interactive setup...")
    success = run_interactive_setup()

    if not success:
        raise CredentialsNotFoundError("Interactive setup was cancelled or failed")


def get_version() -> str:
    """Get version from pyproject.toml."""
    try:
        import os
        import tomllib

        pyproject_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "pyproject.toml"
        )
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            return data["project"]["version"]
    except Exception:
        return "unknown"


def main() -> None:
    """Main application entry point."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    config = get_config()

    # Configure logging
    configure_logging(
        log_level=config.server.log_level,
        json_format=not config.is_interactive and config.server.log_level != "DEBUG",
    )

    version = get_version()

    # Print banner in interactive mode
    if config.is_interactive:
        print(f"🔗 LinkedIn MCP Server v{version} 🔗")
        print("=" * 40)

    logger.info(f"LinkedIn MCP Server v{version}")

    # Set headless mode from config
    set_headless(config.browser.headless)

    # Handle --clear-session flag
    if config.server.clear_session:
        clear_profile_and_exit()

    # Handle --get-session flag
    if config.server.get_session:
        get_profile_and_exit()

    # Handle --session-info flag
    if config.server.session_info:
        profile_info_and_exit()

    logger.debug(f"Server configuration: {config}")

    # Phase 1: Ensure Authentication is Ready
    try:
        ensure_authentication_ready()
        print("✅ Authentication ready")
        logger.info("Authentication ready")

    except CredentialsNotFoundError as e:
        logger.error(f"Authentication setup failed: {e}")
        if config.is_interactive:
            print("\n❌ Authentication required")
            print(str(e))
        else:
            print("\n❌ Authentication required for non-interactive mode")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\n👋 Setup cancelled by user")
        sys.exit(0)

    except (AuthenticationError, RateLimitError) as e:
        logger.error(f"LinkedIn error during setup: {e}")
        print(f"\n❌ {str(e)}")
        sys.exit(1)

    except Exception as e:
        logger.error(f"Unexpected error during authentication setup: {e}")
        print(f"\n❌ Setup failed: {e}")
        sys.exit(1)

    # Phase 2: Server Runtime
    try:
        transport = config.server.transport

        # Prompt for transport in interactive mode if not explicitly set
        if config.is_interactive and not config.server.transport_explicitly_set:
            print("\n🚀 Server ready! Choose transport mode:")
            transport = choose_transport_interactive()

        # Print Claude config in interactive stdio mode
        if config.is_interactive and transport == "stdio":
            print_claude_config()

        # Create and run the MCP server
        mcp = create_mcp_server()

        print(f"\n🚀 Running LinkedIn MCP server ({transport.upper()} mode)...")
        if transport == "streamable-http":
            print(
                f"📡 HTTP server at http://{config.server.host}:{config.server.port}{config.server.path}"
            )
            mcp.run(
                transport=transport,
                host=config.server.host,
                port=config.server.port,
                path=config.server.path,
            )
        else:
            mcp.run(transport=transport)

    except KeyboardInterrupt:
        print("\n⏹️  Server stopped by user")
        exit_gracefully(0)

    except Exception as e:
        logger.error(f"Server runtime error: {e}")
        print(f"\n❌ Server error: {e}")
        exit_gracefully(1)


def exit_gracefully(exit_code: int = 0) -> None:
    """Exit the application gracefully with browser cleanup."""
    print("👋 Shutting down LinkedIn MCP server...")
    try:
        asyncio.run(close_browser())
    except Exception:
        pass  # Best effort cleanup
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_gracefully(0)
    except Exception as e:
        logger.error(
            f"Error running MCP server: {e}",
            extra={"exception_type": type(e).__name__, "exception_message": str(e)},
        )
        print(f"❌ Error running MCP server: {e}")
        exit_gracefully(1)
