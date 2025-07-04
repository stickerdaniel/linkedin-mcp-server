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
from fastmcp.server.middleware import Middleware, MiddlewareContext

from linkedin_mcp_server.config import get_config, reset_config
from linkedin_mcp_server.drivers.chrome import initialize_driver
from linkedin_mcp_server.server import create_mcp_server, shutdown_handler


class SmitheryConfigMiddleware(Middleware):
    """
    FastMCP middleware to handle Smithery query parameter configuration.

    Intercepts HTTP requests and extracts configuration from query parameters,
    then temporarily sets environment variables for the duration of the request.
    """

    def __init__(self):
        super().__init__()
        self.param_mapping = {
            "linkedin_email": "LINKEDIN_EMAIL",
            "linkedin_password": "LINKEDIN_PASSWORD",
        }

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        """
        Called before each tool execution.
        Extract configuration from HTTP request query parameters.
        """
        # Store original environment variables
        original_env = {}
        for env_var in self.param_mapping.values():
            original_env[env_var] = os.environ.get(env_var)

        # Extract query parameters from the request context
        query_params = self._extract_query_params(context)

        if query_params:
            # Apply configuration from query parameters
            self._apply_config(query_params)

            # Reset configuration to pick up new environment variables
            reset_config()

        try:
            # Execute the tool with the new configuration
            result = await call_next(context)
            return result
        finally:
            # Restore original environment variables
            self._restore_env(original_env)

    def _extract_query_params(self, context: MiddlewareContext) -> dict:
        """Extract query parameters from the request context."""
        # Check if we can access FastMCP context for HTTP transport
        if hasattr(context, "fastmcp_context") and context.fastmcp_context:
            # Check if there's transport-specific information
            if hasattr(context.fastmcp_context, "transport_info"):
                transport_info = context.fastmcp_context.transport_info
                if hasattr(transport_info, "query_params"):
                    return dict(transport_info.query_params)

        # Try to get from environment if set by HTTP server
        query_string = os.environ.get("QUERY_STRING", "")
        if query_string:
            return {k: v[0] for k, v in parse_qs(query_string).items()}

        return {}

    def _apply_config(self, query_params: dict):
        """Apply configuration from query parameters to environment variables."""
        for param, env_var in self.param_mapping.items():
            if param in query_params and query_params[param]:
                os.environ[env_var] = query_params[param]
                print(f"🔧 Applied config: {param} -> {env_var}")

    def _restore_env(self, original_env: dict):
        """Restore original environment variables."""
        for env_var, original_value in original_env.items():
            if original_value is not None:
                os.environ[env_var] = original_value
            elif env_var in os.environ:
                del os.environ[env_var]


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

    # Add Smithery configuration middleware
    mcp.add_middleware(SmitheryConfigMiddleware())

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
