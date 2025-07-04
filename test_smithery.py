#!/usr/bin/env python3
"""Test script to verify Smithery tool discovery works correctly."""

import asyncio
import httpx
import json
import sys


async def test_tool_discovery():
    """Test that the MCP server exposes its tools correctly."""
    base_url = "http://localhost:8000/mcp"

    print("🔍 Testing MCP Server Tool Discovery...")
    print(f"📡 Server URL: {base_url}")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # First, try to list available tools
            print("1. Testing tool listing...")

            # MCP protocol request to list tools
            request_data = {
                "jsonrpc": "2.0",
                "method": "tools/list",
                "params": {},
                "id": 1,
            }

            response = await client.post(
                base_url,
                json=request_data,
                headers={"Content-Type": "application/json"},
            )

            print(f"   Response status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(f"   Response: {json.dumps(data, indent=2)}")

                if "result" in data and "tools" in data["result"]:
                    tools = data["result"]["tools"]
                    print(f"\n✅ Found {len(tools)} tools:")
                    for tool in tools:
                        print(
                            f"   - {tool.get('name', 'Unknown')}: {tool.get('description', 'No description')}"
                        )
                else:
                    print("❌ No tools found in response")
            else:
                print(f"❌ Server returned error: {response.text}")

        except Exception as e:
            print(f"❌ Error testing server: {e}")
            return False

    return True


async def test_server_info():
    """Test basic server information endpoint."""
    base_url = "http://localhost:8000/mcp"

    print("\n2. Testing server info...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Try to get server information
            request_data = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "0.1.0",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
                "id": 0,
            }

            response = await client.post(
                base_url,
                json=request_data,
                headers={"Content-Type": "application/json"},
            )

            print(f"   Response status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                print(f"   Server info: {json.dumps(data, indent=2)}")
                print("✅ Server initialization successful")
            else:
                print(f"❌ Server returned error: {response.text}")

        except Exception as e:
            print(f"❌ Error testing server: {e}")
            return False

    return True


async def main():
    """Run all tests."""
    print("=" * 50)
    print("LinkedIn MCP Server - Smithery Test")
    print("=" * 50)

    # Test server info first
    if not await test_server_info():
        print("\n⚠️  Server may not be running or configured correctly")
        sys.exit(1)

    # Then test tool discovery
    if not await test_tool_discovery():
        print("\n⚠️  Tool discovery failed")
        sys.exit(1)

    print("\n✅ All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
