"""Import-boundary contracts for the public core package."""

from __future__ import annotations

from pathlib import Path

import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(source: str) -> None:
    subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def test_navigation_leaf_import_does_not_load_browser_lifecycle_modules():
    _run_isolated(
        """
import sys
import linkedin_mcp_server.scraping.navigation

loaded = set(sys.modules)
forbidden = {
    name
    for name in loaded
    if name == "linkedin_mcp_server.core.browser"
    or name == "linkedin_mcp_server.browser_downgrade"
    or name == "linkedin_mcp_server.hidden_target"
    or name.startswith("linkedin_mcp_server.process_")
}
assert forbidden == set(), sorted(forbidden)
"""
    )


def test_public_core_browser_exports_resolve_lazily_with_module_identity():
    _run_isolated(
        """
import sys
import linkedin_mcp_server.core as core

assert "BrowserManager" in core.__all__
assert "await_deferring_cancels" in core.__all__
assert "linkedin_mcp_server.core.browser" not in sys.modules

from linkedin_mcp_server.core import BrowserManager, await_deferring_cancels
from linkedin_mcp_server.core import browser

assert BrowserManager is browser.BrowserManager
assert await_deferring_cancels is browser.await_deferring_cancels
assert core.BrowserManager is browser.BrowserManager
assert core.await_deferring_cancels is browser.await_deferring_cancels
"""
    )
