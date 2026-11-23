"""Test-only launcher: the real stdio server with a stand-in login.

Runs ``linkedin_mcp_server.__main__.main()`` exactly as ``python -m
linkedin_mcp_server`` does, after replacing the login browser with a recorded
page. Nothing in the package can switch to this; only a test starts it.

Environment:

``LOGIN_RELAY_TEST_EVENTS``
    File the stand-in page appends every input event to, one JSON line each.
``LOGIN_RELAY_TEST_FINISH``
    The stand-in login completes once this file exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast


def _jpeg(width: int, height: int) -> bytes:
    """The smallest byte string the view reads as a JPEG of that size."""
    sof = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


class _RecordedPage:
    """A page whose screenshots are fixed and whose input is written down."""

    def __init__(self, events: Path) -> None:
        self._events = events
        page = self

        class Mouse:
            async def click(self, x: float, y: float, *, button: str) -> None:
                page.record("click", [x, y, button])

            async def move(self, x: float, y: float) -> None:
                page.record("move", [x, y])

            async def wheel(self, dx: float, dy: float) -> None:
                page.record("wheel", [dx, dy])

        class Keyboard:
            async def press(self, key: str) -> None:
                page.record("press", key)

            async def type(self, text: str) -> None:
                page.record("type", text)

            async def insert_text(self, text: str) -> None:
                page.record("insert_text", text)

        self.mouse = Mouse()
        self.keyboard = Keyboard()

    def record(self, kind: str, value: Any) -> None:
        with self._events.open("a", encoding="utf-8") as events:
            events.write(json.dumps([kind, value]) + "\n")

    def is_closed(self) -> bool:
        return False

    async def evaluate(self, _expression: str) -> list[int]:
        return [800, 600]

    async def screenshot(self, **_options: object) -> bytes:
        return _jpeg(800, 600)


class _Context:
    def on(self, _event: str, _listener: object) -> None:
        pass

    def remove_listener(self, _event: str, _listener: object) -> None:
        pass


def _install_the_stand_in() -> None:
    from linkedin_mcp_server import bootstrap, login_view
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        source_state_path,
    )

    events = Path(os.environ["LOGIN_RELAY_TEST_EVENTS"])
    finish = Path(os.environ["LOGIN_RELAY_TEST_FINISH"])
    page = _RecordedPage(events)

    async def login(profile: Path, *, superseded_by: object) -> bool:
        del superseded_by
        browser = SimpleNamespace(page=page, context=_Context())
        async with login_view.published(cast(Any, browser)):
            while not finish.exists():
                await asyncio.sleep(0.05)
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Cookies").write_text("placeholder")
        cookies = portable_cookie_path(profile)
        cookies.write_text(json.dumps([{"name": "li_at", "domain": ".linkedin.com"}]))
        source_state_path(profile).write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_runtime_id": "test-host",
                    "login_generation": "gen-relay",
                    "created_at": "2026-11-20T00:00:00Z",
                    "profile_path": str(profile),
                    "cookies_path": str(cookies),
                }
            )
        )
        return True

    async def installed(**_kwargs: object) -> None:
        return None

    setattr(bootstrap, "interactive_login", login)
    setattr(bootstrap, "_ensure_browser_installed", installed)


if __name__ == "__main__":
    _install_the_stand_in()
    from linkedin_mcp_server.__main__ import main

    sys.argv = ["mcp-server-linkedin", "--transport", "stdio"]
    main()
