"""Redirect-loop recovery in extractor navigation.

A ``net::ERR_TOO_MANY_REDIRECTS`` failure means LinkedIn is bouncing the
session between login/checkpoint — a stale or corrupt cookie state. The
extractor must recover autonomously: clear the live context's cookies,
drop persisted auth artifacts, and retry the navigation once so the
existing auth-barrier flow can raise AuthenticationError and hand off to
the interactive re-login. Without this, users had to quit the client and
delete ~/.linkedin-mcp/profile manually.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor


class _FakeFrame:
    url = "https://www.linkedin.com/"


class _FakePage:
    """Page whose goto redirect-loops once, then succeeds."""

    def __init__(self, failures: int = 1) -> None:
        self._failures = failures
        self.goto_calls: list[str] = []
        self.main_frame = _FakeFrame()
        self.context = SimpleNamespace(clear_cookies=AsyncMock())

    def on(self, *_args) -> None:
        """Accept framenavigated listener registration."""

    def remove_listener(self, *_args) -> None:
        pass

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("Page.goto: net::ERR_TOO_MANY_REDIRECTS at " + url)


class _ScriptedPage(_FakePage):
    """Page whose goto follows a scripted list of outcomes."""

    def __init__(self, outcomes: list[str]) -> None:
        super().__init__(failures=0)
        self._outcomes = outcomes

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        outcome = self._outcomes.pop(0) if self._outcomes else "ok"
        if outcome == "loop":
            raise RuntimeError("Page.goto: net::ERR_TOO_MANY_REDIRECTS at " + url)


def _make_extractor(page: _FakePage) -> LinkedInExtractor:
    extractor = LinkedInExtractor.__new__(LinkedInExtractor)
    extractor._page = page
    return extractor


@pytest.fixture()
def nav_env():
    """Neutralize tracing/stabilization and observe auth-state clearing."""
    with (
        patch(
            "linkedin_mcp_server.scraping.extractor.record_page_trace",
            new=AsyncMock(),
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor.stabilize_navigation",
            new=AsyncMock(),
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "linkedin_mcp_server.session_state.clear_auth_state",
            return_value=True,
        ) as clear_auth,
    ):
        yield clear_auth


class TestRedirectLoopRecovery:
    async def test_clears_state_and_retries_once(self, nav_env):
        """One redirect loop clears auth state and retries exactly once:
        two goto calls — the original attempt plus one retry."""
        page = _FakePage(failures=1)
        extractor = _make_extractor(page)

        await extractor._goto_with_auth_checks("https://www.linkedin.com/feed/")

        assert len(page.goto_calls) == 2
        page.context.clear_cookies.assert_awaited_once()
        nav_env.assert_called_once()

    async def test_persistent_loop_propagates_after_single_recovery(self, nav_env):
        """Recovery must not retry endlessly: a second consecutive loop is
        a real failure (e.g. LinkedIn soft-flag) and propagates to the
        caller."""
        page = _FakePage(failures=2)
        extractor = _make_extractor(page)

        with (
            patch.object(
                LinkedInExtractor,
                "_log_navigation_failure",
                new=AsyncMock(),
            ),
            patch.object(
                LinkedInExtractor,
                "_raise_if_auth_barrier",
                new=AsyncMock(),
            ),
            pytest.raises(RuntimeError, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await extractor._goto_with_auth_checks("https://www.linkedin.com/feed/")

        assert len(page.goto_calls) == 2
        page.context.clear_cookies.assert_awaited_once()
        nav_env.assert_called_once()

    async def test_remember_me_retry_does_not_rearm_recovery(self, nav_env):
        """A remember-me retry on the error path must not re-arm redirect
        loop recovery: with the prompt still resolvable from the stale DOM,
        two consecutive loops clear auth state exactly once and the third
        goto failure propagates instead of triggering a second cleanup."""
        page = _FakePage(failures=3)
        extractor = _make_extractor(page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                LinkedInExtractor,
                "_log_navigation_failure",
                new=AsyncMock(),
            ),
            patch.object(
                LinkedInExtractor,
                "_raise_if_auth_barrier",
                new=AsyncMock(),
            ),
            pytest.raises(RuntimeError, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await extractor._goto_with_auth_checks("https://www.linkedin.com/feed/")

        assert len(page.goto_calls) == 3
        page.context.clear_cookies.assert_awaited_once()
        nav_env.assert_called_once()

    async def test_barrier_retry_after_recovery_does_not_rearm_recovery(self, nav_env):
        """When the recovery retry lands on an auth barrier and remember-me
        resolves it, the follow-up navigation must not re-arm recovery: a
        redirect loop there propagates instead of wiping the freshly
        selected session with a second cleanup."""
        page = _ScriptedPage(["loop", "ok", "loop"])
        extractor = _make_extractor(page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
                new=AsyncMock(return_value="login page"),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                LinkedInExtractor,
                "_log_navigation_failure",
                new=AsyncMock(),
            ),
            patch.object(
                LinkedInExtractor,
                "_raise_if_auth_barrier",
                new=AsyncMock(),
            ),
            pytest.raises(RuntimeError, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await extractor._goto_with_auth_checks("https://www.linkedin.com/feed/")

        assert len(page.goto_calls) == 3
        page.context.clear_cookies.assert_awaited_once()
        nav_env.assert_called_once()

    async def test_other_navigation_errors_do_not_touch_auth_state(self, nav_env):
        page = _FakePage(failures=1)

        async def goto(url, **_kwargs):
            page.goto_calls.append(url)
            raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")

        page.goto = goto  # ty: ignore[invalid-assignment]
        extractor = _make_extractor(page)

        with (
            patch.object(
                LinkedInExtractor,
                "_log_navigation_failure",
                new=AsyncMock(),
            ),
            patch.object(
                LinkedInExtractor,
                "_raise_if_auth_barrier",
                new=AsyncMock(),
            ),
            pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"),
        ):
            await extractor._goto_with_auth_checks("https://www.linkedin.com/feed/")

        assert len(page.goto_calls) == 1
        page.context.clear_cookies.assert_not_awaited()
        nav_env.assert_not_called()
