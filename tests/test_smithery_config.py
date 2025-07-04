# tests/test_smithery_config.py
"""
Test Smithery configuration parameter passing.
"""

import pytest
import os
from unittest.mock import patch, MagicMock
from fastmcp.client import Client
from fastmcp.server.middleware import MiddlewareContext
from linkedin_mcp_server.server import create_mcp_server
from smithery_main import SmitheryConfigMiddleware


@pytest.mark.asyncio
async def test_smithery_middleware_extracts_config():
    """Test that SmitheryConfigMiddleware correctly extracts configuration from query parameters."""
    middleware = SmitheryConfigMiddleware()

    # Mock MiddlewareContext with query parameters via environment
    context = MagicMock(spec=MiddlewareContext)
    context.fastmcp_context = None

    # Set query string in environment to simulate HTTP request
    os.environ["QUERY_STRING"] = (
        "linkedin_email=test@example.com&linkedin_password=testpass123"
    )

    # Mock call_next
    async def mock_call_next(ctx):
        # During tool execution, check that env vars are set
        assert os.environ.get("LINKEDIN_EMAIL") == "test@example.com"
        assert os.environ.get("LINKEDIN_PASSWORD") == "testpass123"
        return MagicMock()

    # Store original env vars
    original_email = os.environ.get("LINKEDIN_EMAIL")
    original_password = os.environ.get("LINKEDIN_PASSWORD")
    original_query_string = os.environ.get("QUERY_STRING")

    try:
        # Execute middleware
        await middleware.on_call_tool(context, mock_call_next)

        # After execution, env vars should be restored
        assert os.environ.get("LINKEDIN_EMAIL") == original_email
        assert os.environ.get("LINKEDIN_PASSWORD") == original_password

        print("✅ Smithery middleware correctly handles configuration")

    finally:
        # Cleanup
        if original_email is not None:
            os.environ["LINKEDIN_EMAIL"] = original_email
        elif "LINKEDIN_EMAIL" in os.environ:
            del os.environ["LINKEDIN_EMAIL"]

        if original_password is not None:
            os.environ["LINKEDIN_PASSWORD"] = original_password
        elif "LINKEDIN_PASSWORD" in os.environ:
            del os.environ["LINKEDIN_PASSWORD"]

        if original_query_string is not None:
            os.environ["QUERY_STRING"] = original_query_string
        elif "QUERY_STRING" in os.environ:
            del os.environ["QUERY_STRING"]


@pytest.mark.asyncio
async def test_smithery_middleware_with_empty_config():
    """Test that middleware works correctly with no configuration."""
    middleware = SmitheryConfigMiddleware()

    # Mock context with no query parameters
    context = MagicMock(spec=MiddlewareContext)
    context.fastmcp_context = None

    # Mock call_next
    async def mock_call_next(ctx):
        return MagicMock()

    # Should not raise any errors
    result = await middleware.on_call_tool(context, mock_call_next)
    assert result is not None

    print("✅ Smithery middleware handles empty configuration")


@pytest.mark.asyncio
async def test_smithery_server_with_middleware():
    """Test that MCP server with Smithery middleware can be created and tools discovered."""
    with patch("sys.argv", ["smithery_main.py"]):
        # Create server (simulate smithery_main.py)
        mcp = create_mcp_server()

        # Add middleware
        mcp.add_middleware(SmitheryConfigMiddleware())

        # Test that tools are discoverable
        async with Client(mcp) as client:
            tools = await client.list_tools()

            tool_names = [tool.name for tool in tools]
            expected_tools = [
                "get_person_profile",
                "get_company_profile",
                "get_job_details",
                "close_session",
            ]

            for expected_tool in expected_tools:
                assert expected_tool in tool_names, f"Tool '{expected_tool}' not found"

            print(f"✅ Smithery server with middleware: {len(tools)} tools discovered")


def test_smithery_middleware_param_mapping():
    """Test that SmitheryConfigMiddleware has correct parameter mapping."""
    middleware = SmitheryConfigMiddleware()

    expected_mapping = {
        "linkedin_email": "LINKEDIN_EMAIL",
        "linkedin_password": "LINKEDIN_PASSWORD",
    }

    assert middleware.param_mapping == expected_mapping
    print("✅ Smithery middleware parameter mapping is correct")


if __name__ == "__main__":
    # Run tests manually if executed directly
    import asyncio

    asyncio.run(test_smithery_middleware_extracts_config())
    asyncio.run(test_smithery_middleware_with_empty_config())
    asyncio.run(test_smithery_server_with_middleware())
    test_smithery_middleware_param_mapping()
    print("🎉 All Smithery configuration tests passed!")
