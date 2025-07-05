# main.py
"""
LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration.
"""

import logging
import sys
from typing import Literal

import inquirer  # type: ignore

from linkedin_mcp_server.cli import print_claude_config

# Import the new centralized configuration
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.chrome import initialize_driver
from linkedin_mcp_server.exceptions import LinkedInMCPError
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_scraper.exceptions import (
    CaptchaRequiredError,
    InvalidCredentialsError,
    LoginTimeoutError,
    RateLimitError,
    SecurityChallengeError,
    TwoFactorAuthError,
)
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler


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
    return answers["transport"]


def main() -> None:
    """Initialize and run the LinkedIn MCP server."""
    print("🔗 LinkedIn MCP Server 🔗")
    print("=" * 40)

    # Get configuration using the new centralized system
    config = get_config()

    # Configure logging
    configure_logging(
        debug=config.server.debug,
        json_format=config.chrome.non_interactive,  # Use JSON format in non-interactive mode
    )

    logger = logging.getLogger("linkedin_mcp_server")
    logger.debug(f"Server configuration: {config}")

    # Initialize the driver with configuration (initialize driver checks for lazy init options)
    try:
        initialize_driver()
    except (
        LinkedInMCPError,
        CaptchaRequiredError,
        InvalidCredentialsError,
        SecurityChallengeError,
        TwoFactorAuthError,
        RateLimitError,
        LoginTimeoutError,
    ) as e:
        logger.error(
            f"Failed to initialize driver: {str(e)}",
            extra={"error_type": type(e).__name__, "error_details": str(e)},
        )

        # Always terminate if login fails and we're not using lazy initialization
        if not config.server.lazy_init:
            print(f"\n❌ {str(e)}")
            sys.exit(1)

        # In lazy init mode with non-interactive, still exit on error
        if config.chrome.non_interactive:
            sys.exit(1)
        else:
            print(f"\n❌ Error: {str(e)}")
            print("💡 Tip: Check your credentials and try again.")
            sys.exit(1)

    # Decide transport
    transport = config.server.transport
    if config.server.setup:
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


def exit_gracefully(exit_code: int = 0) -> None:
    """Exit the application gracefully, cleaning up resources."""
    print("\n👋 Shutting down LinkedIn MCP server...")
    shutdown_handler()
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        exit_gracefully(0)
    except Exception as e:
        print(f"❌ Error running MCP server: {e}")
        exit_gracefully(1)
