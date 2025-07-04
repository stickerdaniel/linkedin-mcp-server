#!/usr/bin/env python3
# smithery_main.py
"""
LinkedIn MCP Server - Smithery HTTP Transport Entry Point

Handles Smithery's query parameter configuration approach.
Smithery passes config as query params: /mcp?linkedin_email=user@example.com&linkedin_password=pass
"""

import logging
import os
import sys

import uvicorn

# Set up environment for lazy initialization
os.environ.setdefault("LINKEDIN_EMAIL", "")
os.environ.setdefault("LINKEDIN_PASSWORD", "")
os.environ.setdefault("LAZY_INIT", "true")
os.environ.setdefault("NON_INTERACTIVE", "true")
os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TRANSPORT", "streamable-http")
os.environ.setdefault("DEBUG", "false")
os.environ.setdefault("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

# Configure logging
logging.basicConfig(
    level=logging.INFO if os.environ.get("DEBUG") == "true" else logging.ERROR,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Suppress noisy libraries
for logger_name in ["selenium", "urllib3", "httpx", "httpcore"]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)

logger = logging.getLogger("smithery_main")

# Import after environment setup
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from linkedin_mcp_server.config import reset_config  # noqa: E402
from linkedin_mcp_server.server import create_mcp_server  # noqa: E402


class SmitheryConfigMiddleware(BaseHTTPMiddleware):
    """Extract Smithery query parameters and update environment."""

    async def dispatch(self, request: Request, call_next) -> Response:
        """Process query parameters before handling the request."""
        # Extract query parameters
        query_params = dict(request.query_params)

        # Log incoming request for debugging
        logger.info(f"Incoming request: {request.method} {request.url}")
        logger.info(f"Query params: {query_params}")

        # Update environment if credentials are provided
        if query_params:
            # Check for linkedin credentials in query params
            if "linkedin_email" in query_params:
                os.environ["LINKEDIN_EMAIL"] = query_params["linkedin_email"]
                logger.info("Updated LINKEDIN_EMAIL from query params")

            if "linkedin_password" in query_params:
                os.environ["LINKEDIN_PASSWORD"] = query_params["linkedin_password"]
                logger.info("Updated LINKEDIN_PASSWORD from query params")

            # Reset config to pick up new values
            reset_config()

        # Process the request
        response = await call_next(request)
        return response


def create_app():
    """Create the FastMCP ASGI application with Smithery middleware."""
    # Create MCP server
    mcp = create_mcp_server()

    # Create middleware list
    middleware = [Middleware(SmitheryConfigMiddleware)]

    # Create HTTP app with middleware
    app = mcp.http_app(path="/mcp", middleware=middleware, transport="streamable-http")

    return app


def main() -> None:
    """Main entry point for Smithery deployment."""
    print("🔗 LinkedIn MCP Server (Smithery Edition) 🔗")
    print("=" * 50)

    # Get PORT from environment (Smithery requirement)
    port = int(os.environ.get("PORT", 8000))

    # Create the app
    app = create_app()

    print(f"\n🚀 Starting server on port {port}...")
    print(f"📡 Server endpoint: http://0.0.0.0:{port}/mcp")
    print("🔧 Tools available for discovery")
    print("⚙️  Config via query params: ?linkedin_email=...&linkedin_password=...")
    print("\n✨ Server is starting...\n")

    # Run with uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="error" if os.environ.get("DEBUG") != "true" else "info",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
