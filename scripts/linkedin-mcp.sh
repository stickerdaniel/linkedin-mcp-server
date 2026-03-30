#!/usr/bin/env zsh
# MCP stdio entry point for Claude. Uses Patchright's persistent profile.
set -euo pipefail

cd /Users/davis/code/linkedin-mcp-server
export PLAYWRIGHT_BROWSERS_PATH="$HOME/.linkedin-mcp/patchright-browsers"
exec uv run -m linkedin_mcp_server
