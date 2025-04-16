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

    setup: bool
    debug: bool
    cache: bool


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
        "--debug",
        action="store_true",
        help="Enable debug mode with additional logging",
    )

    parser.add_argument(
        "--no-setup",
        action="store_true",
        help="Skip printing configuration information",
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Don't use cached credentials or cookies",
    )

    args = parser.parse_args()

    return ServerArguments(
        setup=not args.no_setup,
        debug=args.debug,
        cache=not args.no_cache,
    )
