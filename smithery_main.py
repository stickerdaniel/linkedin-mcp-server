# smithery_main.py
"""
LinkedIn MCP Server - Smithery HTTP Transport Entry Point

This entry point is specifically designed for Smithery deployment with:
- HTTP transport (streamable-http)
- Query parameter configuration parsing
- PORT environment variable support
- Uses existing lazy authentication system
"""

import os
import logging
from urllib.parse import parse_qs

from linkedin_mcp_server.config import get_config, reset_config
from linkedin_mcp_server.drivers.chrome import initialize_driver
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler


def setup_smithery_environment(query_string: str | None = None) -> None:
    """
    Set up environment variables from Smithery query parameters.

    Args:
        query_string: Query parameters from Smithery configuration
    """
    if not query_string:
        return

    # Parse query parameters
    parsed = parse_qs(query_string)

    # Map Smithery parameters to environment variables
    param_mapping = {
        "linkedin_email": "LINKEDIN_EMAIL",
        "linkedin_password": "LINKEDIN_PASSWORD",
    }

    for param, env_var in param_mapping.items():
        if param in parsed and parsed[param]:
            value = parsed[param][0]  # Take first value
            os.environ[env_var] = value

    # Reset config to pick up new environment variables
    reset_config()


def main() -> None:
    """
    Main entry point for Smithery deployment.

    Starts HTTP server listening on PORT environment variable.
    Handles query parameter configuration as required by Smithery Custom Deploy.
    """
    print("🔗 LinkedIn MCP Server (Smithery) 🔗")
    print("=" * 40)

    # Get PORT from environment (Smithery requirement)
    port = int(os.environ.get("PORT", 8000))

    # Force settings for Smithery compatibility
    os.environ["DEBUG"] = "false"  # No debug logs in production
    os.environ.setdefault("TRANSPORT", "streamable-http")

    # Ensure we don't try to use keyring in containers
    os.environ.setdefault("LINKEDIN_EMAIL", "")
    os.environ.setdefault("LINKEDIN_PASSWORD", "")

    # Initialize configuration (will use lazy_init=True by default)
    get_config()

    # Configure minimal logging for containers
    logging.basicConfig(
        level=logging.ERROR,  # Only errors, no debug/info spam
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger("linkedin_mcp_server")
    logger.error(f"Starting Smithery MCP server on port {port}")

    # Initialize driver with lazy loading (no immediate credentials needed)
    initialize_driver()

    # Create MCP server (tools will be registered and available for discovery)
    mcp = create_mcp_server()

    # Start HTTP server
    print("\n🚀 Running LinkedIn MCP server (Smithery HTTP mode)...")
    print(f"📡 HTTP server listening on http://0.0.0.0:{port}/mcp")
    print("🔧 Tools available for discovery - no credentials required")
    print("⚙️  Configure linkedin_email and linkedin_password to use tools")

    try:
        # Add a startup delay to ensure everything is ready
        import time

        time.sleep(1)

        mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
    except KeyboardInterrupt:
        print("\n👋 Shutting down LinkedIn MCP server...")
        shutdown_handler()
    except Exception as e:
        print(f"❌ Error running MCP server: {e}")
        print(f"Stack trace: {e.__class__.__name__}: {str(e)}")
        shutdown_handler()
        raise


if __name__ == "__main__":
    main()
