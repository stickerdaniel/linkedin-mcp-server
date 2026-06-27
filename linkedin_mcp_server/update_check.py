"""Background check for a newer release on PyPI, surfaced as a tool-result notice.

Most users install via ``uvx mcp-server-linkedin@latest``, which re-resolves PyPI
on every client launch, so they are already current. This is defense-in-depth for
the minority that pin a fixed version, run a stale Docker tag, or are offline: it
polls the PyPI JSON API at most once a day, caches the answer under
``~/.linkedin-mcp``, and appends a single gentle notice to a tool result when the
installed version is meaningfully behind. Set ``LINKEDIN_MCP_CHECK_FOR_UPDATES=off``
to disable; it is skipped automatically in CI and for source/dev installs.

The notice is added as an extra text content block so the model relays it. It never
touches the structured tool output, so the documented ``{url, sections}`` shape is
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from packaging.version import InvalidVersion, Version

from linkedin_mcp_server import __version__

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/mcp-server-linkedin/json"
_CACHE_PATH = Path.home() / ".linkedin-mcp" / "update-check.json"
_CACHE_TTL_SECONDS = 24 * 60 * 60
_REQUEST_TIMEOUT_SECONDS = 2.0
_DEV_VERSION = "0.0.0.dev"
_DISABLED_VALUES = {"0", "false", "off", "no"}

# Latest version learned this process. None until the background check resolves.
_latest_known: str | None = None


def _check_disabled() -> bool:
    """Whether the network check should be skipped entirely."""
    value = os.environ.get("LINKEDIN_MCP_CHECK_FOR_UPDATES", "").strip().lower()
    if value in _DISABLED_VALUES:
        return True
    if os.environ.get("CI"):
        return True
    if __version__ == _DEV_VERSION:
        return True
    return False


def _read_cache() -> tuple[float, str] | None:
    try:
        data = json.loads(_CACHE_PATH.read_text())
        return float(data["checked_at"]), str(data["latest"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(latest: str) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({"checked_at": time.time(), "latest": latest})
        )
    except OSError:
        logger.debug("Could not write update-check cache", exc_info=True)


def _fetch_latest_from_pypi() -> str | None:
    request = Request(
        _PYPI_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": f"mcp-server-linkedin/{__version__}",
        },
    )
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data["info"]["version"])
    except Exception:  # noqa: BLE001 - never let a version check break a tool call
        logger.debug("PyPI update check failed", exc_info=True)
        return None


async def refresh_latest_version() -> None:
    """Populate ``_latest_known`` from cache or a throttled PyPI request.

    Fire-and-forget: any failure falls back to the stale cache or leaves the
    notice silent. Never raises.
    """
    global _latest_known
    if _check_disabled():
        return

    cached = _read_cache()
    if cached is not None and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        _latest_known = cached[1]
        return

    latest = await asyncio.to_thread(_fetch_latest_from_pypi)
    if latest is None:
        if cached is not None:  # fail open to whatever we last knew
            _latest_known = cached[1]
        return

    _write_cache(latest)
    _latest_known = latest


def _is_meaningfully_behind(current: Version, latest: Version) -> bool:
    """True for a minor/major bump, or two-plus patch releases on the same line.

    A single fresh patch is ignored so ``@latest`` users mid-refresh are not nagged.
    """
    if latest <= current:
        return False
    if (latest.major, latest.minor) > (current.major, current.minor):
        return True
    return (latest.micro - current.micro) >= 2


def pending_update_notice() -> str | None:
    """A one-line notice when the installed version is meaningfully behind, else None."""
    if _latest_known is None or __version__ == _DEV_VERSION:
        return None
    try:
        current = Version(__version__)
        latest = Version(_latest_known)
    except InvalidVersion:
        return None
    if current.is_devrelease or latest.is_prerelease:
        return None
    if not _is_meaningfully_behind(current, latest):
        return None
    return (
        f"Update available: mcp-server-linkedin {latest} is out (you are on "
        f"{current}). Restart your MCP client to pick it up; if a fixed version is "
        'pinned, switch to "mcp-server-linkedin@latest". Set '
        "LINKEDIN_MCP_CHECK_FOR_UPDATES=off to silence this."
    )


class UpdateNoticeMiddleware(Middleware):
    """Kick off the update check once and append the notice to a tool result once."""

    def __init__(self) -> None:
        self._kicked = False
        self._notified = False
        self._task: asyncio.Task[None] | None = None

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not self._kicked:
            self._kicked = True
            # Retain a reference so the task is not garbage-collected mid-flight.
            self._task = asyncio.create_task(refresh_latest_version())

        result = await call_next(context)

        if not self._notified:
            notice = pending_update_notice()
            if notice is not None:
                self._notified = True
                result.content = [
                    *result.content,
                    mt.TextContent(type="text", text=notice),
                ]
        return result
