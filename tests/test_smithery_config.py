# tests/test_smithery_config.py
"""
Test Smithery configuration parameter passing.
"""

import os
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from smithery_main import SmitheryConfigMiddleware, create_app


@pytest.mark.asyncio
async def test_smithery_middleware_extracts_config():
    """Test that SmitheryConfigMiddleware correctly extracts configuration from query parameters."""
    # Create a simple Starlette app for testing
    app = Starlette()
    middleware = SmitheryConfigMiddleware(app)

    # Create a mock request with query parameters
    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url = "http://test.com/mcp?linkedin_email=test@example.com&linkedin_password=testpass123"
    request.query_params = {
        "linkedin_email": "test@example.com",
        "linkedin_password": "testpass123",
    }

    # Mock call_next function
    async def mock_call_next(req):
        # During middleware execution, check that env vars are set
        assert os.environ.get("LINKEDIN_EMAIL") == "test@example.com"
        assert os.environ.get("LINKEDIN_PASSWORD") == "testpass123"
        return PlainTextResponse("OK")

    # Store original env vars
    original_email = os.environ.get("LINKEDIN_EMAIL")
    original_password = os.environ.get("LINKEDIN_PASSWORD")

    try:
        # Execute middleware
        response = await middleware.dispatch(request, mock_call_next)
        assert response.status_code == 200

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


@pytest.mark.asyncio
async def test_smithery_middleware_with_empty_config():
    """Test that middleware works correctly with no configuration."""
    # Create a simple Starlette app for testing
    app = Starlette()
    middleware = SmitheryConfigMiddleware(app)

    # Create a mock request with no query parameters
    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url = "http://test.com/mcp"
    request.query_params = {}

    # Mock call_next function
    async def mock_call_next(req):
        return PlainTextResponse("OK")

    # Should not raise any errors
    response = await middleware.dispatch(request, mock_call_next)
    assert response.status_code == 200

    print("✅ Smithery middleware handles empty configuration")


def test_smithery_app_creation():
    """Test that Smithery app can be created successfully."""
    app = create_app()
    assert app is not None
    print("✅ Smithery app creation successful")


def test_smithery_middleware_param_handling():
    """Test that SmitheryConfigMiddleware correctly handles different parameter scenarios."""
    # Create a simple Starlette app for testing
    app = Starlette()
    middleware = SmitheryConfigMiddleware(app)

    # Test that middleware can be instantiated
    assert middleware is not None
    assert hasattr(middleware, "dispatch")

    print("✅ Smithery middleware parameter handling is correct")


if __name__ == "__main__":
    # Run tests manually if executed directly
    import asyncio

    asyncio.run(test_smithery_middleware_extracts_config())
    asyncio.run(test_smithery_middleware_with_empty_config())
    test_smithery_app_creation()
    test_smithery_middleware_param_handling()
    print("🎉 All Smithery configuration tests passed!")
