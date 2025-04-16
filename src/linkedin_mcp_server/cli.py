# src/linkedin_mcp_server/cli.py
"""
CLI utilities for LinkedIn MCP server.

This module handles the command-line interface and configuration management.
"""

import sys
from typing import Dict, Any, List
import os
import json
import logging
import pyperclip  # type: ignore

logger = logging.getLogger(__name__)


def print_claude_config() -> None:
    """
    Print Claude configuration and copy to clipboard.

    This function generates the configuration needed for Claude Desktop
    and copies it to the clipboard for easy pasting.
    """
    current_dir = os.path.abspath(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    )

    # Find the full path to python executable
    try:
        python_path = sys.executable
        print(f"🔍 Found Python executable at: {python_path}")
    except NameError:
        # Fallback if sys is not imported
        python_path = "python"
        print(
            "⚠️ Could not find full path to Python, using 'python' directly. "
            "This may not work in Claude Desktop."
        )

    # Include useful command-line arguments in the default args
    args: List[str] = [
        os.path.join(current_dir, "main.py"),
        "--no-setup",
    ]

    config_json: Dict[str, Any] = {
        "mcpServers": {
            "linkedin-scraper": {
                "command": python_path,
                "args": args,
                "env": {
                    "LINKEDIN_EMAIL": "your.email@example.com",
                    "LINKEDIN_PASSWORD": "your_password_here",
                },
            }
        }
    }

    # Convert to string for clipboard
    config_str = json.dumps(config_json, indent=2)

    # Print the final configuration
    print("\n📋 Your Claude configuration should look like:")
    print(config_str)
    print(
        "\n🔧 Add this to your Claude Desktop configuration in Settings > Developer > Edit Config"
    )
    print(
        "\n⚠️ Be sure to update LINKEDIN_EMAIL and LINKEDIN_PASSWORD with your actual credentials"
    )

    # Copy to clipboard
    try:
        pyperclip.copy(config_str)  # Only copy the JSON, not the comments
        print("✅ Claude configuration copied to clipboard!")
    except ImportError:
        print(
            "⚠️ pyperclip not installed. To copy configuration automatically, run: uv add pyperclip"
        )
    except Exception as e:
        print(f"❌ Could not copy to clipboard: {e}")
