"""A page in a window that never opens.

Headless mode is what makes a browser announce itself. Chromium prepends the
bare string ``Headless`` to the product name at runtime, so both the user agent
and the ``sec-ch-ua`` brand list read ``HeadlessChrome`` -- which is why no
change to *which* binary runs can remove it, only a change to the mode. But this
server must not put a window on somebody's desktop either.

Chromium can create a target with no UI window at all. The page behaves like an
ordinary one; it simply has nowhere to be drawn. Every value below was measured
before this was written, and the notes say which measurement decided what,
because three of these details are the difference between working and silently
not.

Not measured, and worth stating plainly: a window *is* on screen for about half
a second on every browser start, between Chromium opening its startup window and
this closing it. It cannot be shortened from here. Roughly 250 ms passes before
``launch_persistent_context()`` even returns, about 340 ms is macOS tearing the
window down after ``close()``, and the part this code controls is around 90 ms.
The only way to zero is for the browser never to open a window, which needs
``--no-startup-window``; that hangs Playwright for its full timeout
(https://github.com/microsoft/playwright/issues/42093).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

#: Playwright only surfaces a target Chromium reports as ``type: "other"`` when
#: this is set in the *driver process* environment. It is undocumented upstream,
#: and byte-identical across patchright 1.60.0, 1.60.1, 1.61.1 and 1.61.2; a
#: question about whether it is supported is open at
#: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/discussions/232
ATTACH_TO_OTHER = "PW_CHROMIUM_ATTACH_TO_OTHER"

#: How long to wait for the created target to surface as a Page. Generous: this
#: is a local round trip, and the failure it guards is a hang rather than a slow
#: machine.
_ATTACH_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.02


def hidden_target_is_supported() -> bool:
    """Whether this platform can work in a window that never opens.

    Two things have to be true, and only one of them is about displays.

    A headed browser needs a display server. Measured in the published
    container image: with no ``DISPLAY``, ``launch_persistent_context(
    headless=False)`` fails with a ``TargetClosedError`` before any of this code
    runs.

    And the browser has to survive losing its last visible window, because that
    is exactly what this does. Measured, same image, under Xvfb: closing the
    startup page kills Chromium and the hidden page dies with it, while keeping
    that page open leaves everything working. macOS does not quit an application
    when its last window closes, which is why the mechanism works there.

    So this is deliberately narrow. macOS is measured and supported. Windows is
    *not* measured -- it plausibly behaves like Linux -- and claiming it without
    looking would be the kind of guess this file exists to avoid. Everything
    else falls back to Chromium's headless mode and says so.

    Linux is not a gap so much as a different answer: under a virtual display
    nobody is looking at the screen, so an ordinary window is already invisible
    and there is nothing to hide. That is the container's path, and it belongs
    with the change that gives the container a display.
    """
    return sys.platform == "darwin"


class HiddenTargetError(RuntimeError):
    """The windowless page could not be created.

    Raised rather than recovered from. Falling back to real headless would
    restore the token the user believes is gone, and falling back to a visible
    window would put one on their screen unannounced. Both are worse than
    stopping.
    """


#: Guards the flag against overlapping launches. ``os.environ`` is
#: process-global while the block it scopes contains an ``await``, so two
#: launches can interleave: measured, two concurrent driver starts left the flag
#: set afterwards in two runs out of five, and the opposite ordering would let
#: the second launch miss it entirely. Today's production paths are serialised
#: by the bootstrap and browser-creation locks, but ``BrowserManager`` is public
#: and nothing about it promises that.
_flag_lock = threading.Lock()
#: How many launches are inside the block. The first sets, the last restores.
#: Counting rather than locking around the whole block, because that would
#: serialise driver startup for no reason -- concurrent launches all want the
#: flag set, so they can share it.
_flag_depth = 0
_flag_previous: str | None = None


@contextlib.contextmanager
def attaching_to_other_targets():
    """Set the attach flag for the duration of a driver launch, then restore it.

    Scoped rather than set once at import, because the flag makes Playwright
    promote *every* ``other`` target it sees. A headed login launched in the
    same process has no need of that and should not inherit it.

    The value has to be in the environment before the driver subprocess starts,
    which is why this wraps ``async_playwright().start()`` rather than the
    context launch.

    Safe to nest and to overlap: see ``_flag_depth``.
    """
    global _flag_depth, _flag_previous
    with _flag_lock:
        if _flag_depth == 0:
            _flag_previous = os.environ.get(ATTACH_TO_OTHER)
        _flag_depth += 1
        os.environ[ATTACH_TO_OTHER] = "1"
    try:
        yield
    finally:
        with _flag_lock:
            _flag_depth -= 1
            if _flag_depth == 0:
                if _flag_previous is None:
                    os.environ.pop(ATTACH_TO_OTHER, None)
                else:
                    os.environ[ATTACH_TO_OTHER] = _flag_previous
                _flag_previous = None


async def open_hidden_page(context: "BrowserContext", startup: "Page") -> "Page":
    """Return a page with no window, and close the visible startup page.

    *startup* is the page Chromium opened by itself. It is closed once the
    hidden one exists, not before: closing it first would leave the context
    with nothing and Chromium may exit.
    """
    browser = context.browser
    if browser is None:
        raise HiddenTargetError(
            "No browser object on the context, so no browser-level CDP session. "
            "A hidden target created from a page's session dies with that page."
        )

    # Browser-level, and this is not a stylistic choice. Measured: a target
    # created through `context.new_cdp_session(page)` is destroyed when that
    # page closes -- the browser stays up, `new_page()` still works, and the
    # hidden page is simply gone. `context.new_cdp_session` is the obvious call
    # and it is the wrong one.
    session = await browser.new_browser_cdp_session()

    # Identified by a nonce, never by position. The attach flag promotes every
    # `other` target, and `context.pages` was measured containing a component
    # extension's page beside the real one, so index-based selection picks the
    # wrong target without failing.
    nonce = secrets.token_hex(8)
    url = f"about:blank#{nonce}"

    # `about:blank` rather than a loopback URL, and that matters with a proxy.
    # Measured: with Playwright's `proxy` option set and no bypass, a loopback
    # nonce origin is fetched *through* the proxy and the page lands on
    # `chrome-error://chromewebdata/`. `about:blank` has no origin to route, so
    # it needs no bypass entry and cannot be broken by someone's proxy.
    await session.send(
        "Target.createTarget", {"url": url, "hidden": True, "background": True}
    )

    deadline = time.monotonic() + _ATTACH_TIMEOUT_SECONDS
    hidden: Page | None = None
    while time.monotonic() < deadline:
        hidden = next((page for page in context.pages if nonce in page.url), None)
        if hidden is not None:
            break
        await asyncio.sleep(_POLL_SECONDS)

    if hidden is None:
        raise HiddenTargetError(
            f"Chromium accepted a hidden target but it never surfaced as a page "
            f"within {_ATTACH_TIMEOUT_SECONDS:.0f}s. This needs "
            f"{ATTACH_TO_OTHER} set in the driver process environment."
        )

    await startup.close()
    logger.debug("Working in a hidden target; the startup window is closed")
    return hidden
