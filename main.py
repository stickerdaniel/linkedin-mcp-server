# main.py
"""
LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration.

Clean architecture with clear phase separation:
1. Authentication Setup Phase
2. Driver Management Phase
3. Server Runtime Phase
"""

import logging
import sys
from typing import Literal

import inquirer  # type: ignore
from linkedin_scraper.exceptions import (
    CaptchaRequiredError,
    InvalidCredentialsError,
    LoginTimeoutError,
    RateLimitError,
    SecurityChallengeError,
    TwoFactorAuthError,
)

from linkedin_mcp_server.authentication import (
    ensure_authentication,
    has_authentication,
)
from linkedin_mcp_server.cli import print_claude_config
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.chrome import close_all_drivers, get_or_create_driver
from linkedin_mcp_server.exceptions import CredentialsNotFoundError, LinkedInMCPError
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler
from linkedin_mcp_server.setup import run_cookie_extraction_setup, run_interactive_setup

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


def get_cookie_and_exit() -> None:
    """Get LinkedIn cookie and exit (for Docker setup)."""
    config = get_config()

    # Configure logging - prioritize debug mode over non_interactive
    configure_logging(
        debug=config.server.debug,
        json_format=config.chrome.non_interactive and not config.server.debug,
    )

    logger.info("LinkedIn MCP Server - Cookie Extraction mode started")

    try:
        # Run cookie extraction setup
        cookie = run_cookie_extraction_setup()

        logger.info("Cookie extraction successful")
        print("✅ Login successful!")
        print("🍪 LinkedIn Cookie extracted:")
        print(cookie)

        # Try to copy to clipboard
        try:
            import pyperclip

            pyperclip.copy(cookie)
            print(
                "📋 Cookie copied to clipboard! Now you can set the LINKEDIN_COOKIE environment variable in your configuration"
            )
        except Exception as e:
            logger.warning(f"Could not copy to clipboard: {e}")
            print("⚠️  Copy the cookie above manually")

    except Exception as e:
        logger.error(f"Error getting cookie: {e}")

        # Provide specific guidance for security challenges
        error_msg = str(e).lower()
        if "security challenge" in error_msg or "captcha" in error_msg:
            print("❌ LinkedIn security challenge detected")
            print("💡 Try one of these solutions:")
            print(
                "   1. Use an existing LinkedIn cookie from your browser instead (see instructions below)"
            )
            print(
                "   2. Use --no-headless flag (manual installation required, does not work with Docker) and solve the security challenge manually"
            )
            print("\n🍪 To get your LinkedIn cookie manually:")
            print("   1. Login to LinkedIn in your browser")
            print("   2. Open Developer Tools (F12)")
            print("   3. Go to Application/Storage > Cookies > linkedin.com")
            print("   4. Copy the 'li_at' cookie value")
            print("   5. Set LINKEDIN_COOKIE environment variable or use --cookie flag")
        elif "invalid credentials" in error_msg:
            print("❌ Invalid LinkedIn credentials")
            print("💡 Please check your email and password")
        else:
            print("❌ Failed to obtain cookie - check your credentials")
        sys.exit(1)

    sys.exit(0)


def ensure_authentication_ready() -> str:
    """
    Phase 1: Ensure authentication is ready before any drivers are created.

    Returns:
        str: Valid LinkedIn session cookie

    Raises:
        CredentialsNotFoundError: If authentication setup fails
    """
    config = get_config()

    # Check if authentication already exists
    if has_authentication():
        try:
            return ensure_authentication()
        except CredentialsNotFoundError:
            # Authentication exists but might be invalid, continue to setup
            pass

    # If in non-interactive mode and no auth, fail immediately
    if config.chrome.non_interactive:
        raise CredentialsNotFoundError(
            "No LinkedIn authentication found. Please provide cookie via "
            "environment variable (LINKEDIN_COOKIE) or run with --get-cookie to obtain one."
        )

    # Run interactive setup
    logger.info("Setting up LinkedIn authentication...")
    return run_interactive_setup()


def initialize_driver_with_auth(authentication: str) -> None:
    """
    Phase 2: Initialize driver using existing authentication.

    Args:
        authentication: LinkedIn session cookie

    Raises:
        Various exceptions if driver creation or login fails
    """
    config = get_config()

    if config.server.lazy_init:
        logger.info(
            "Using lazy initialization - driver will be created on first tool call"
        )
        return

    logger.info("Initializing Chrome WebDriver and logging in...")

    try:
        # Create driver and login with provided authentication
        get_or_create_driver(authentication)
        logger.info("✅ Web driver initialized and authenticated successfully")

    except Exception as e:
        logger.error(f"Failed to initialize driver: {e}")
        raise e


def main() -> None:
    """Main application entry point with clear phase separation."""
    logger.info("🔗 LinkedIn MCP Server 🔗")
    print("🔗 LinkedIn MCP Server 🔗")
    print("=" * 40)

    # Get configuration
    config = get_config()

    # Handle --get-cookie flag immediately
    if config.server.get_cookie:
        get_cookie_and_exit()

    # Configure logging - prioritize debug mode over non_interactive
    configure_logging(
        debug=config.server.debug,
        json_format=config.chrome.non_interactive and not config.server.debug,
    )

    logger.debug(f"Server configuration: {config}")

    # Phase 1: Ensure Authentication is Ready
    try:
        authentication = ensure_authentication_ready()
        print("✅ Authentication ready")
        logger.info("Authentication ready")
    except CredentialsNotFoundError as e:
        logger.error(f"Authentication setup failed: {e}")
        print(
            "\n❌ Authentication required - please provide LinkedIn cookie or credentials"
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n👋 Setup cancelled by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error during authentication setup: {e}")
        print("\n❌ Setup failed - please try again")
        sys.exit(1)

    # Phase 2: Initialize Driver (if not lazy)
    try:
        initialize_driver_with_auth(authentication)
    except InvalidCredentialsError as e:
        logger.error(f"Driver initialization failed with invalid credentials: {e}")

        # Cookie was already cleared in driver layer
        # In interactive mode, try setup again
        if not config.chrome.non_interactive and config.server.setup:
            print(f"\n❌ {str(e)}")
            print("🔄 Starting interactive setup for new authentication...")
            try:
                new_authentication = run_interactive_setup()
                # Try again with new authentication
                initialize_driver_with_auth(new_authentication)
                logger.info("✅ Successfully authenticated with new credentials")
            except Exception as setup_error:
                logger.error(f"Setup failed: {setup_error}")
                print(f"\n❌ Setup failed: {setup_error}")
                sys.exit(1)
        else:
            print(f"\n❌ {str(e)}")
            if not config.server.lazy_init:
                sys.exit(1)
    except (
        LinkedInMCPError,
        CaptchaRequiredError,
        SecurityChallengeError,
        TwoFactorAuthError,
        RateLimitError,
        LoginTimeoutError,
    ) as e:
        logger.error(f"Driver initialization failed: {e}")
        print(f"\n❌ {str(e)}")
        if not config.server.lazy_init:
            sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error during driver initialization: {e}")
        print(f"\n❌ Driver initialization failed: {e}")
        if not config.server.lazy_init:
            sys.exit(1)

    # Phase 3: Server Runtime
    try:
        # Decide transport
        transport = config.server.transport
        if config.server.setup:
            print("\n🚀 Server ready! Choose transport mode:")
            transport = choose_transport_interactive()

        # Print configuration for Claude if in setup mode and using stdio transport
        if config.server.setup and transport == "stdio":
            print_claude_config()

        # Create and run the MCP server
        mcp = create_mcp_server()

        # Start server
        print(f"\n🚀 Running LinkedIn MCP server ({transport.upper()} mode)...")
        if transport == "streamable-http":
            print(
                f"📡 HTTP server will be available at http://{config.server.host}:{config.server.port}{config.server.path}"
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
        print("\n\n👋 Server stopped by user")
        exit_gracefully(0)
    except Exception as e:
        logger.error(f"Server runtime error: {e}")
        print(f"\n❌ Server error: {e}")
        exit_gracefully(1)


def exit_gracefully(exit_code: int = 0) -> None:
    """Exit the application gracefully, cleaning up resources."""
    print("\n👋 Shutting down LinkedIn MCP server...")

    # Clean up drivers
    close_all_drivers()

    # Clean up server
    shutdown_handler()

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
