"""Shared debug-only helpers for slower, traceable navigation flows."""

from __future__ import annotations

import asyncio
import logging
import os

_NAV_STABILIZE_DELAY_SECONDS = 5.0


def debug_stabilize_navigation_enabled() -> bool:
    """Return whether debug-only navigation stabilization sleeps are enabled."""
    return os.getenv("LINKEDIN_DEBUG_STABILIZE_NAVIGATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def stabilize_navigation(label: str, logger: logging.Logger) -> None:
    """Pause between navigation steps to help debug timing-sensitive flows."""
    if os.environ.get("PYTEST_CURRENT_TEST") or not debug_stabilize_navigation_enabled():
        return

    logger.debug(
        "Stabilizing navigation for %.1fs after %s",
        _NAV_STABILIZE_DELAY_SECONDS,
        label,
    )
    await asyncio.sleep(_NAV_STABILIZE_DELAY_SECONDS)
