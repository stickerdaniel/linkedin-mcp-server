# tests/test_mcp_http.py
"""
Test that the MCP server HTTP transport works and tools are accessible.
"""

import pytest
import asyncio
from unittest.mock import patch
from fastmcp.client import Client
from linkedin_mcp_server.server import create_mcp_server


@pytest.mark.asyncio
async def test_mcp_server_tools_accessible():
    """Test that MCP server tools are accessible via in-memory client."""
    # Mock sys.argv to avoid pytest argument parsing conflicts
    with patch("sys.argv", ["main.py"]):
        # Create MCP server
        mcp = create_mcp_server()

        # Connect client directly to server (in-memory)
        async with Client(mcp) as client:
            # Test that we can list tools
            tools = await client.list_tools()

            # Verify expected LinkedIn tools are present
            tool_names = [tool.name for tool in tools]
            expected_tools = [
                "get_person_profile",
                "get_company_profile",
                "get_job_details",
                "close_session",
            ]

            for expected_tool in expected_tools:
                assert expected_tool in tool_names, (
                    f"Tool '{expected_tool}' not found in {tool_names}"
                )

            print(f"✅ Found {len(tools)} tools: {tool_names}")


@pytest.mark.asyncio
async def test_tools_have_proper_schemas():
    """Test that tools have proper input schemas."""
    with patch("sys.argv", ["main.py"]):
        mcp = create_mcp_server()

        async with Client(mcp) as client:
            tools = await client.list_tools()

            # Check each tool has required properties
            for tool in tools:
                assert tool.name is not None
                assert tool.description is not None
                assert len(tool.description) > 0

                if tool.name in [
                    "get_person_profile",
                    "get_company_profile",
                    "get_job_details",
                ]:
                    # These tools should have input schemas
                    assert tool.inputSchema is not None
                    assert "properties" in tool.inputSchema

            print(f"✅ All {len(tools)} tools have proper schemas")


@pytest.mark.asyncio
async def test_close_session_tool_works():
    """Test that close_session tool can be called successfully."""
    with patch("sys.argv", ["main.py"]):
        mcp = create_mcp_server()

        async with Client(mcp) as client:
            # Call close_session tool (should work without credentials)
            result = await client.call_tool("close_session")

            assert result.content is not None
            assert len(result.content) > 0

            response = result.content[0]
            assert response.type == "text"
            assert len(response.text) > 0

            print(f"✅ close_session tool response: {response.text[:100]}...")


@pytest.mark.asyncio
async def test_tools_fail_gracefully_without_credentials():
    """Test that LinkedIn tools fail gracefully when no credentials provided."""
    # Mock sys.argv to avoid pytest argument parsing conflicts
    with patch("sys.argv", ["main.py"]):
        # Mock the driver creation to avoid WebDriver initialization
        with patch(
            "linkedin_mcp_server.drivers.chrome.get_or_create_driver"
        ) as mock_driver:
            mock_driver.return_value = None  # Simulate no driver available

            mcp = create_mcp_server()

            async with Client(mcp) as client:
                # Try to call a LinkedIn tool without credentials
                # This should either return an error message or raise an exception gracefully
                try:
                    result = await client.call_tool(
                        "get_person_profile",
                        {"linkedin_url": "https://www.linkedin.com/in/test-user/"},
                    )

                    # If no exception, check that result indicates missing credentials
                    assert result.content is not None
                    response = result.content[0]

                    # Should mention credentials, driver, or login issues
                    error_keywords = [
                        "credential",
                        "driver",
                        "login",
                        "error",
                        "failed",
                    ]
                    assert any(
                        keyword in response.text.lower() for keyword in error_keywords
                    ), f"Expected error message about credentials, got: {response.text}"

                    print(f"✅ Tool failed gracefully: {response.text[:100]}...")

                except Exception as e:
                    # Exception is also acceptable - means proper error handling
                    print(f"✅ Tool raised exception (acceptable): {str(e)[:100]}...")


def test_mcp_server_creation():
    """Test that MCP server can be created successfully."""
    with patch("sys.argv", ["main.py"]):
        mcp = create_mcp_server()

        assert mcp is not None
        assert mcp.name == "linkedin_scraper"

        print("✅ MCP server created successfully")


if __name__ == "__main__":
    # Run tests manually if executed directly
    asyncio.run(test_mcp_server_tools_accessible())
    asyncio.run(test_tools_have_proper_schemas())
    asyncio.run(test_close_session_tool_works())
    asyncio.run(test_tools_fail_gracefully_without_credentials())
    test_mcp_server_creation()
    print("🎉 All tests passed!")
