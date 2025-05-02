# main.py
"""
LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration.

This is the main entry point that runs the LinkedIn MCP server.
"""

import sys
import logging
import uvicorn
import inquirer  # type: ignore  # third-party package without type stubs
from typing import NoReturn
from fastapi import FastAPI
from linkedin_mcp_server.arguments import parse_arguments
from linkedin_mcp_server.cli import print_claude_config
from linkedin_mcp_server.drivers.chrome import initialize_driver
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler


# Initialize FastAPI app
app = FastAPI()


def choose_transport_interactive() -> str:
    """Prompt user for transport mode using inquirer."""
    questions = [
        inquirer.List(
            "transport",
            message="Choose transport mode",
            choices=[
                ("stdio (Default CLI mode)", "stdio"),
                ("sse (Server-Sent Events HTTP mode)", "sse"),
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

    # Initialize the driver - with lazy initialization if specified
    initialize_driver(headless=args.headless, lazy_init=args.lazy_init)

    # Print configuration for Claude if in setup mode
    if args.setup:
        print_claude_config()

    # Create and run the MCP server
    mcp = create_mcp_server()

    # Decide transport
    if args.setup:
        transport = choose_transport_interactive()
    else:
        transport = "stdio"  # Default to stdio without prompt

    # Start server
    if transport == "sse":
        print("\n🚀 Running LinkedIn MCP server (SSE mode)...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        # Run using stdio
        print("\n🚀 Running LinkedIn MCP server (STDIO mode)...")
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
