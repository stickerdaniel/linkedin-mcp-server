# main.py
"""
LinkedIn MCP Server - A Model Context Protocol server for LinkedIn integration.

This is the main entry point that runs the LinkedIn MCP server.
"""
import sys

from src.linkedin_mcp_server.cli import print_claude_config
from src.linkedin_mcp_server.drivers.chrome import initialize_driver
from src.linkedin_mcp_server.server import create_mcp_server, shutdown_handler


def main() -> None:
    """Initialize and run the LinkedIn MCP server."""
    print("🔗 LinkedIn MCP Server 🔗")
    print("=" * 40)

    # Initialize the driver before starting the server
    initialize_driver()

    # Print configuration for Claude
    print_claude_config()

    # Create and run the MCP server
    mcp = create_mcp_server()
    print("\n🚀 Starting LinkedIn MCP server...")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Shutting down LinkedIn MCP server...")
        shutdown_handler()
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error running MCP server: {e}")
        shutdown_handler()
        sys.exit(1)