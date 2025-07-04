# tests/conftest.py
"""
Simple pytest configuration for LinkedIn MCP server tests.
"""

import os
import pytest
from linkedin_mcp_server.config import reset_config


@pytest.fixture(autouse=True)
def clean_environment():
    """Clean environment before each test."""
    # Reset configuration singleton
    reset_config()

    # Clear environment variables that might affect tests
    env_vars_to_clear = [
        "LINKEDIN_EMAIL",
        "LINKEDIN_PASSWORD",
        "DEBUG",
        "CHROMEDRIVER",
        "HEADLESS",
        "TRANSPORT",
    ]
    original_env = {}
    for var in env_vars_to_clear:
        original_env[var] = os.environ.get(var)
        if var in os.environ:
            del os.environ[var]

    yield

    # Restore environment variables
    for var, value in original_env.items():
        if value is not None:
            os.environ[var] = value
        elif var in os.environ:
            del os.environ[var]
