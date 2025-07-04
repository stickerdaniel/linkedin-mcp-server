#!/usr/bin/env python3
"""Test minimal imports and server creation."""

import os

# Set environment variables first
os.environ["LINKEDIN_EMAIL"] = ""
os.environ["LINKEDIN_PASSWORD"] = ""
os.environ["LAZY_INIT"] = "true"
os.environ["NON_INTERACTIVE"] = "true"
os.environ["HEADLESS"] = "true"

print("1. Environment variables set")

try:
    from linkedin_mcp_server.config import get_config

    print("2. Config imported successfully")

    config = get_config()
    print(
        f"3. Config loaded: lazy_init={config.server.lazy_init}, non_interactive={config.chrome.non_interactive}"
    )

    from linkedin_mcp_server.drivers.chrome import initialize_driver

    print("4. Chrome driver module imported")

    initialize_driver()
    print("5. Driver initialized (should be lazy)")

    from linkedin_mcp_server.server import create_mcp_server

    print("6. Server module imported")

    mcp = create_mcp_server()
    print("7. MCP server created")

    print("\n✅ All imports and initialization successful!")

except Exception as e:
    print(f"\n❌ Error at step: {e}")
    import traceback

    traceback.print_exc()
