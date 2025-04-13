# src/linkedin_mcp_server/arguments.py
"""
Command-line argument parsing for LinkedIn MCP server.

This module handles parsing and validating command-line arguments.
"""

import argparse
from dataclasses import dataclass


@dataclass
class ServerArguments:
    """Command-line arguments for the LinkedIn MCP server."""

    headless: bool
    setup: bool
    debug: bool


def parse_arguments() -> ServerArguments:
    """
    Parse command-line arguments for the LinkedIn MCP server.

    Returns:
        ServerArguments: Parsed command-line arguments
    """
    parser = argparse.ArgumentParser(
        description="LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration"
    )

    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run Chrome with a visible browser window (useful for debugging)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode with additional logging",
    )

    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="Enable debug mode with additional logging",
    )

    args = parser.parse_args()

    return ServerArguments(
        headless=not args.no_headless,
        setup=not args.no_setup,
        debug=args.debug,
    )
