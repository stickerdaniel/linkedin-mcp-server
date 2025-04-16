# main.py
"""
LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration.

This is the main entry point that runs the LinkedIn MCP server.
"""

import sys
import logging
from typing import NoReturn

from linkedin_mcp_server.arguments import parse_arguments
from linkedin_mcp_server.cli import print_claude_config
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler


def main() -> None:
    """Initialize and run the LinkedIn MCP server."""
    print("🔗 LinkedIn MCP Server 🔗")
    print("=" * 40)

    # Parse command-line arguments
    args = parse_arguments()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.ERROR
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger("linkedin_mcp_server")
    logger.debug(f"Server arguments: {args}")

    # Print configuration for Claude if in setup mode
    if args.setup:
        print_claude_config()

    # Create and run the MCP server
    mcp = create_mcp_server()
    print("\n🚀 Running LinkedIn MCP server...")
    mcp.run(transport="stdio")


def exit_gracefully(exit_code: int = 0) -> NoReturn:
    """
    Exit the application gracefully, cleaning up resources.

    Args:
        exit_code: The exit code to use when terminating
    """
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
