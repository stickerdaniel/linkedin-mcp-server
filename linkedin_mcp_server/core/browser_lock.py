"""Shared browser lock for serializing browser access across tool calls and background tasks."""

import asyncio

browser_lock = asyncio.Lock()
