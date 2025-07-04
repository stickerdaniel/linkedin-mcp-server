#!/bin/bash
# Simple test to see if server starts

export PORT=8000
export LAZY_INIT=true
export NON_INTERACTIVE=true
export HEADLESS=true
export DEBUG=false

echo "Starting server..."
uv run python smithery_main.py
