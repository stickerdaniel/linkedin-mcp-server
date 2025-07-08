# src/linkedin_mcp_server/cli.py
"""
CLI utilities for LinkedIn MCP server configuration generation.

Automatically generates Claude Desktop configuration with proper tool registration,
environment variables, and clipboard integration for seamless setup workflow.
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, List

import pyperclip  # type: ignore

from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)


def print_claude_config() -> None:
    """
    Print Claude configuration and copy to clipboard.

    This function generates the configuration needed for Claude Desktop
    and copies it to the clipboard for easy pasting.
    """
    config = get_config()
    current_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

    # Find the full path to uv executable
    try:
        uv_path = subprocess.check_output(["which", "uv"], text=True).strip()
        print(f"🔍 Found uv at: {uv_path}")
    except subprocess.CalledProcessError:
        # Fallback if which uv fails
        uv_path = "uv"
        print(
            "⚠️ Could not find full path to uv, using 'uv' directly. "
            "This may not work in Claude Desktop."
        )

    # Include useful command-line arguments in the default args
    args: List[str] = [
        "--directory",
        current_dir,
        "run",
        "main.py",
        "--no-setup",
    ]

    # Add environment variables to the configuration
    env_vars: Dict[str, str] = {}
    if config.linkedin.email:
        env_vars["LINKEDIN_EMAIL"] = config.linkedin.email
    if config.linkedin.password:
        env_vars["LINKEDIN_PASSWORD"] = config.linkedin.password
    if config.chrome.chromedriver_path:
        env_vars["CHROMEDRIVER"] = config.chrome.chromedriver_path

    config_json: Dict[str, Any] = {
        "mcpServers": {
            "linkedin-scraper": {
                "command": uv_path,
                "args": args,
                "disabled": False,
                "requiredTools": [
                    "get_person_profile",
                    "get_company_profile",
                    "get_job_details",
                    "search_jobs",
                ],
            }
        }
    }

    # Add environment variables if available
    if env_vars:
        config_json["mcpServers"]["linkedin-scraper"]["env"] = env_vars

    # Convert to string for clipboard
    config_str = json.dumps(config_json, indent=2)

    # Print the final configuration
    print("\n📋 Your Claude configuration should look like:")
    print(config_str)
    print(
        "\n🔧 Add this to your Claude Desktop configuration in Settings > Developer > Edit Config"
    )

    # Copy to clipboard
    try:
        pyperclip.copy(config_str)
        print("✅ Claude configuration copied to clipboard!")
    except ImportError:
        print(
            "⚠️ pyperclip not installed. To copy configuration automatically, run: uv add pyperclip"
        )
    except Exception as e:
        print(f"❌ Could not copy to clipboard: {e}")
