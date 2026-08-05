"""The windowless page: how it is found, and how it refuses.

Three details here are the difference between working and silently not, and each
was measured rather than reasoned about. They get a test apiece, because a wrong
one of them does not raise -- it hands back the wrong page, or a page that
vanishes later.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast

import pytest

from linkedin_mcp_server.hidden_target import (
    ATTACH_TO_OTHER,
    HiddenTargetError,
    attaching_to_other_targets,
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
            self._pages.append(SimpleNamespace(url=params["url"], close=_noop))
            if self._trailing is not None:
                self._pages.append(self._trailing)
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

        Asserting that the close eventually happened proves nothing about the
        order, so the startup page records what the session had been asked for
        at the moment it was closed.
        """
        pages: list = []
        session = _Session(pages)
        context = _Context(pages, _Browser(session))

        class _RecordingStartup(_Startup):
            methods_at_first_close: list[str] | None = None

            async def close(self):
                # First close only. A close-before-create mutation calls this
                # twice, and letting the second overwrite the record is exactly
                # how this test would stop catching anything.
                if self.methods_at_first_close is None:
                    self.methods_at_first_close = [m for m, _ in session.sent]
                await super().close()

        startup = _RecordingStartup()
        pages.append(startup)

        await open_hidden_page(cast(Any, context), cast(Any, startup))

        assert startup.closed is True
        assert startup.methods_at_first_close == ["Target.createTarget"]


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
