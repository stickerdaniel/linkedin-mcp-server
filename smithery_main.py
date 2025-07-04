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
    Uses existing lazy initialization system.
    """
    print("🔗 LinkedIn MCP Server (Smithery) 🔗")
    print("=" * 40)

    # Get PORT from environment (Smithery requirement)
    port = int(os.environ.get("PORT", 8000))

    # Set up environment for Smithery (can be called with query params later)
    # For now, just ensure we're in the right mode
    os.environ["DEBUG"] = os.environ.get("DEBUG", "false")

    # Force HTTP transport and container-friendly settings
    os.environ.setdefault("TRANSPORT", "streamable-http")

    # Get configuration (will use lazy_init=True by default)
    config = get_config()

    # Configure logging
    log_level = logging.DEBUG if config.server.debug else logging.ERROR
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger = logging.getLogger("linkedin_mcp_server")
    logger.info(f"Starting Smithery MCP server on port {port}")

    # Initialize driver (will use lazy init by default - perfect for Smithery!)
    initialize_driver()

    # Create MCP server (tools will be available for discovery)
    mcp = create_mcp_server()

    # Start HTTP server
    print("\n🚀 Running LinkedIn MCP server (Smithery HTTP mode)...")
    print(f"📡 HTTP server listening on http://0.0.0.0:{port}/mcp")
    print("🔧 Tools available for discovery - credentials validated on use")

    try:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
    except KeyboardInterrupt:
        print("\n👋 Shutting down LinkedIn MCP server...")
        shutdown_handler()
    except Exception as e:
        print(f"❌ Error running MCP server: {e}")
        shutdown_handler()
        raise


if __name__ == "__main__":
    main()
