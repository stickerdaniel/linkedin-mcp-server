"""Staging outlasts one browser that never sends its first request, and no more.

No browser: the import validation is replaced by one that drives the product's
own ``goto_reporting_proxy_errors`` against a page whose request never leaves,
so the failure staging sees is the one the product raises, word for word.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from differential import session
from differential.session import STAGING_LAUNCHES, StagingError
from linkedin_mcp_server.core.proxy_errors import goto_reporting_proxy_errors
from linkedin_mcp_server.session_state import source_state_path

_FEED = "https://www.linkedin.com/feed/"


class _SilentPage:
    """A page whose navigation never sends its request, like a stalled browser."""

    main_frame = None

    def on(self, event: str, handler: Any) -> None:
        pass

    def remove_listener(self, event: str, handler: Any) -> None:
        pass

    async def goto(self, url: str, **kwargs: Any) -> None:
        await asyncio.Event().wait()


def _browsers(monkeypatch, *outcomes: str) -> list[str]:
    """Replace the validation with one browser per entry of *outcomes*.

    ``stall`` never sends its request, ``late`` sends it and gets no answer,
    ``accept`` and ``reject`` finish the check. Returns the launches made.
    """
    monkeypatch.setattr(session, "stage_installed_browser", lambda: None)
    monkeypatch.setattr("linkedin_mcp_server.core.proxy_errors.STARTUP_BUDGET_MS", 50)
    launched: list[str] = []

    async def validate(cookie_path: Path, profile: Path) -> bool:
        outcome = outcomes[len(launched)]
        launched.append(outcome)
        if outcome == "stall":
            await goto_reporting_proxy_errors(
                _SilentPage(), _FEED, wait_until="domcontentloaded"
            )
        if outcome == "late":
            raise PlaywrightTimeoutError(
                "Page.goto: Timeout 30000ms exceeded after the request was sent."
            )
        return outcome == "accept"

    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies", validate
    )
    return launched


async def test_a_browser_that_never_sends_is_replaced(isolate_profile_dir, monkeypatch):
    _browsers(monkeypatch, "stall", "accept")
    staged = await session.stage_signed_in_session(isolate_profile_dir)
    assert staged.li_at
    assert source_state_path(isolate_profile_dir).exists()


async def test_every_browser_stalling_fails_after_the_bound(
    isolate_profile_dir, monkeypatch
):
    launched = _browsers(monkeypatch, *["stall"] * (STAGING_LAUNCHES + 1))
    with pytest.raises(StagingError, match="sent its /feed/ request"):
        await session.stage_signed_in_session(isolate_profile_dir)
    assert len(launched) == STAGING_LAUNCHES
    assert not source_state_path(isolate_profile_dir).exists()


@pytest.mark.parametrize(
    ("first", "error"),
    [
        ("late", PlaywrightTimeoutError),
        ("reject", StagingError),
    ],
)
async def test_any_other_failure_ends_staging_at_once(
    isolate_profile_dir, monkeypatch, first, error
):
    launched = _browsers(monkeypatch, first, "accept")
    with pytest.raises(error):
        await session.stage_signed_in_session(isolate_profile_dir)
    assert launched == [first]
    assert not source_state_path(isolate_profile_dir).exists()
