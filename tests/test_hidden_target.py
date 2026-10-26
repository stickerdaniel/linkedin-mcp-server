"""The windowless page: how it is found, and how it refuses.

Three details here are the difference between working and silently not, and each
was measured rather than reasoned about. They get a test apiece, because a wrong
one of them does not raise -- it hands back the wrong page, or a page that
vanishes later.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from linkedin_mcp_server.hidden_target import (
    ATTACH_TO_OTHER,
    HiddenTargetError,
    attaching_to_other_targets,
    hidden_target_is_supported,
    open_hidden_page,
)


class _Session:
    """A browser-level CDP session that records what it was asked to create.

    ``trailing`` is appended *after* the created target, which is what makes the
    ordering test meaningful: the attach flag promotes every ``other`` target,
    so the page we asked for is not reliably the last one.
    """

    def __init__(self, pages: list, *, create: bool = True, trailing=None):
        self.sent: list[tuple[str, dict]] = []
        self._pages = pages
        self._create = create
        self._trailing = trailing

    async def send(self, method: str, params: dict):
        self.sent.append((method, params))
        if method == "Target.createTarget" and self._create:
            # Surfaced on a later tick, not synchronously. Chromium answers
            # `createTarget` before Playwright has attached and exposed a Page,
            # and a fake that appends immediately hides every ordering mistake:
            # the page is already there by the time anything checks.
            async def _surface() -> None:
                await asyncio.sleep(0.03)
                self._pages.append(SimpleNamespace(url=params["url"], close=_noop))
                if self._trailing is not None:
                    self._pages.append(self._trailing)

            asyncio.get_running_loop().create_task(_surface())
        return {"targetId": "T1"}


async def _noop():
    return None


class _Browser:
    def __init__(self, session):
        self._session = session

    async def new_browser_cdp_session(self):
        return self._session


class _Context:
    def __init__(self, pages, browser):
        self.pages = pages
        self.browser = browser


class _Startup:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False

    async def close(self):
        self.closed = True


class TestTheAttachFlag:
    """It has to be in the driver process environment, and only for that launch."""

    def test_sets_and_restores_when_unset(self, monkeypatch):
        monkeypatch.delenv(ATTACH_TO_OTHER, raising=False)

        with attaching_to_other_targets():
            assert os.environ[ATTACH_TO_OTHER] == "1"

        assert ATTACH_TO_OTHER not in os.environ

    def test_restores_a_previous_value(self, monkeypatch):
        monkeypatch.setenv(ATTACH_TO_OTHER, "operator-set")

        with attaching_to_other_targets():
            assert os.environ[ATTACH_TO_OTHER] == "1"

        assert os.environ[ATTACH_TO_OTHER] == "operator-set"

    async def test_overlapping_launches_do_not_leave_it_set(self, monkeypatch):
        """Two launches in flight at once must still restore exactly once.

        `os.environ` is process-global and the block it scopes contains an
        `await`, so the two interleave. Measured before the counter existed: the
        flag was left set afterwards in two runs out of five, and the opposite
        ordering lets the second launch miss it entirely. Today's production
        paths are serialised by other locks, but nothing about the public
        `BrowserManager` promises that.
        """
        monkeypatch.delenv(ATTACH_TO_OTHER, raising=False)
        seen: list[str | None] = []

        async def launch(delay: float) -> None:
            with attaching_to_other_targets():
                await asyncio.sleep(delay)
                seen.append(os.environ.get(ATTACH_TO_OTHER))

        await asyncio.gather(launch(0.02), launch(0.001))

        # Both saw it set while inside, and nothing is left behind after.
        assert seen == ["1", "1"]
        assert ATTACH_TO_OTHER not in os.environ

    async def test_an_overlapping_launch_restores_a_previous_value(self, monkeypatch):
        monkeypatch.setenv(ATTACH_TO_OTHER, "operator-set")

        async def launch(delay: float) -> None:
            with attaching_to_other_targets():
                await asyncio.sleep(delay)

        await asyncio.gather(launch(0.02), launch(0.001))

        assert os.environ[ATTACH_TO_OTHER] == "operator-set"

    def test_restores_after_a_failure(self, monkeypatch):
        """A launch that raises must not leave the flag behind.

        It makes Playwright promote every `other` target it sees, so a headed
        login later in the same process would inherit behaviour it never asked
        for.
        """
        monkeypatch.delenv(ATTACH_TO_OTHER, raising=False)

        with pytest.raises(RuntimeError):
            with attaching_to_other_targets():
                raise RuntimeError("launch failed")

        assert ATTACH_TO_OTHER not in os.environ


class TestFindingTheRightPage:
    async def test_creates_a_hidden_about_blank_target(self):
        pages: list = []
        context = _Context(pages, _Browser(_Session(pages)))
        startup = _Startup()
        pages.append(startup)

        page = await open_hidden_page(cast(Any, context), cast(Any, startup))

        method, params = context.browser._session.sent[0]
        assert method == "Target.createTarget"
        assert params["hidden"] is True
        assert params["background"] is True
        # `about:blank` has no origin, so a configured proxy cannot route it.
        # Measured: a loopback nonce URL went through the proxy and the page
        # landed on chrome-error://chromewebdata/.
        assert params["url"].startswith("about:blank#")
        assert page.url == params["url"]

    async def test_finds_by_nonce_and_not_by_position(self):
        """Ordering picks the wrong page without failing.

        The attach flag promotes every `other` target, and a real run was
        measured with a component extension's page sitting in `context.pages`
        beside the one we asked for.
        """
        pages: list = []
        decoy = SimpleNamespace(url="chrome-extension://abc/offscreen.html")
        session = _Session(pages, trailing=decoy)
        context = _Context(pages, _Browser(session))
        startup = _Startup()
        pages.append(startup)

        page = await open_hidden_page(cast(Any, context), cast(Any, startup))

        assert pages[-1] is decoy, "the decoy must sit after the target we want"
        assert page is not decoy
        assert page.url.startswith("about:blank#")

    async def test_closes_the_startup_page_only_after_the_hidden_one_exists(self):
        """Closing first would leave the context empty and Chromium may exit.

        The contract is not "createTarget was sent before close" -- Chromium
        answers that call before Playwright has attached anything, so it holds
        even when the close is far too early. What has to be true is that the
        hidden page was already *there*.
        """
        pages: list = []
        session = _Session(pages)
        context = _Context(pages, _Browser(session))

        class _RecordingStartup(_Startup):
            hidden_present_at_close: bool | None = None

            async def close(self):
                # First close only: a close-before-wait mutation calls this
                # twice, and letting the second overwrite the record is how
                # this test would stop catching anything.
                if self.hidden_present_at_close is None:
                    self.hidden_present_at_close = any(
                        str(page.url).startswith("about:blank#") for page in pages
                    )
                await super().close()

        startup = _RecordingStartup()
        pages.append(startup)

        await open_hidden_page(cast(Any, context), cast(Any, startup))

        assert startup.closed is True
        assert startup.hidden_present_at_close is True


class TestItFailsClosed:
    async def test_refuses_without_a_browser_object(self):
        """No browser means no browser-level session.

        A target created from a page's session was measured dying with that
        page: the browser stays up, `new_page()` still works, and the hidden
        page is gone.
        """
        pages: list = []
        context = _Context(pages, None)
        startup = _Startup()

        with pytest.raises(HiddenTargetError, match="browser-level"):
            await open_hidden_page(cast(Any, context), cast(Any, startup))

        assert startup.closed is False

    async def test_raises_when_the_target_never_surfaces(self, monkeypatch):
        """Chromium accepting the target does not mean Playwright exposed it.

        Without the attach flag it detaches the session instead, and the wait
        would otherwise hang rather than say so.
        """
        monkeypatch.setattr(
            "linkedin_mcp_server.hidden_target._ATTACH_TIMEOUT_SECONDS", 0.05
        )
        pages: list = []
        session = _Session(pages, create=False)
        context = _Context(pages, _Browser(session))
        startup = _Startup()
        pages.append(startup)

        with pytest.raises(HiddenTargetError, match=ATTACH_TO_OTHER):
            await open_hidden_page(cast(Any, context), cast(Any, startup))

        # The visible page is left alone: a caller that recovers still has one.
        assert startup.closed is False


class TestPlatformSupport:
    """Where the mechanism is claimed to work, and where it is not."""

    def test_macos_is_supported(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")

        assert hidden_target_is_supported() is True

    def test_linux_is_not(self, monkeypatch):
        """Measured in the container image: with a display, closing the startup
        page kills Chromium and the hidden page with it; without one, a headed
        launch never starts."""
        monkeypatch.setattr("sys.platform", "linux")

        assert hidden_target_is_supported() is False

    def test_windows_is_not_claimed(self, monkeypatch):
        """Unmeasured, and it plausibly behaves like Linux. Claiming support
        without looking is the guess this module exists to avoid."""
        monkeypatch.setattr("sys.platform", "win32")

        assert hidden_target_is_supported() is False
