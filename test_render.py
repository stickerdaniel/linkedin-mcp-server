#!/usr/bin/env python3
"""
Test script for verifying Render deployment configuration.
This script tests the HTTP transport mode locally before deploying to Render.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict

import aiohttp

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_mcp_server(base_url: str = "http://localhost:8000") -> None:
    """Test the MCP server HTTP endpoints."""

    # Test endpoints
    endpoints = [
        ("GET", "/", "Root endpoint"),
        ("POST", "/mcp", "MCP tools list"),
    ]

    async with aiohttp.ClientSession() as session:
        for method, path, description in endpoints:
            url = f"{base_url}{path}"
            logger.info(f"Testing {description}: {method} {url}")

            try:
                if method == "GET":
                    async with session.get(url) as response:
                        logger.info(f"Status: {response.status}")
                        if response.status == 200:
                            text = await response.text()
                            logger.info(f"Response: {text[:100]}...")

                elif method == "POST" and path == "/mcp":
                    # Test MCP tools list request
                    mcp_request = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {}
                    }

                    async with session.post(
                        url,
                        json=mcp_request,
                        headers={"Content-Type": "application/json"}
                    ) as response:
                        logger.info(f"Status: {response.status}")
                        if response.status == 200:
                            data = await response.json()
                            logger.info(f"Available tools: {len(data.get('result', {}).get('tools', []))}")
                            for tool in data.get('result', {}).get('tools', [])[:3]:
                                logger.info(f"  - {tool.get('name')}: {tool.get('description', 'No description')[:60]}...")
                        else:
                            text = await response.text()
                            logger.error(f"Error response: {text}")

            except Exception as e:
                logger.error(f"Error testing {description}: {e}")

            logger.info("-" * 50)


def start_local_server() -> subprocess.Popen:
    """Start the MCP server locally in HTTP mode."""
    logger.info("Starting local MCP server in HTTP mode...")

    # Set environment variables for testing
    env = os.environ.copy()
    env.update({
        "LINKEDIN_COOKIE": "test_cookie_value",  # Not a real cookie for testing
        "PYTHONUNBUFFERED": "1",
        "LOG_LEVEL": "INFO"
    })

    # Start server process
    cmd = [
        "uv", "run", "-m", "linkedin_mcp_server",
        "--transport", "streamable-http",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--path", "/mcp",
        "--log-level", "INFO"
    ]

    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )

    # Wait a moment for server to start
    logger.info("Waiting for server to start...")
    time.sleep(5)

    return process


async def main():
    """Main test function."""
    logger.info("=== LinkedIn MCP Server Render Configuration Test ===")

    # Check if we should test against a live URL
    test_url = os.getenv("TEST_URL")
    if test_url:
        logger.info(f"Testing live deployment at: {test_url}")
        await test_mcp_server(test_url)
        return

    # Start local server for testing
    server_process = None
    try:
        server_process = start_local_server()

        # Test the server
        await test_mcp_server()

        logger.info("✅ Local HTTP server test completed successfully!")
        logger.info("Your Render configuration should work correctly.")

    except KeyboardInterrupt:
        logger.info("Test interrupted by user")
    except Exception as e:
        logger.error(f"Test failed: {e}")
        sys.exit(1)
    finally:
        if server_process:
            logger.info("Stopping local server...")
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()


if __name__ == "__main__":
    # Handle Ctrl+C gracefully
    def signal_handler(signum, frame):
        logger.info("Received interrupt signal, exiting...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    asyncio.run(main())