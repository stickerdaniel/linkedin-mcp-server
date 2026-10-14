"""Staging outlasts one browser that stalls on ``/feed/``, and no more.

No browser: the import validation is replaced by one that stalls the way the
staging code's own does. For the candidate that is the product's
``goto_reporting_proxy_errors`` against a page whose request never leaves, so
the failure is the one the product raises, word for word; for the frozen
baseline it is the wording its Patchright was measured to raise. A stalled
browser leaves debris in the profile and the cookie file, as a killed one can,
and each launch records what it found.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from differential import session
from differential.session import (
    STAGING_LAUNCHES,
    StagingError,
    write_synthetic_cookie_file,
)
from linkedin_mcp_server.core.proxy_errors import goto_reporting_proxy_errors
from linkedin_mcp_server.session_state import portable_cookie_path, source_state_path

_FEED = "https://www.linkedin.com/feed/"

#: What Patchright 1.61.2, the baseline's, raised for a ``goto`` that ran out
#: its default clock (measured against a loopback listener; the URL is ours).
_BASELINE_TIMEOUT = (
    "Page.goto: Timeout 30000ms exceeded.\nCall log:\n"
    f'  - navigating to "{_FEED}", waiting until "domcontentloaded"\n'
)


class _SilentPage:
    """A page whose navigation never sends its request, like a stalled browser."""

    main_frame = None

    def on(self, event: str, handler: Any) -> None:
        pass

    def remove_listener(self, event: str, handler: Any) -> None:
        pass

    async def goto(self, url: str, **kwargs: Any) -> None:
        await asyncio.Event().wait()


async def _candidate_stall() -> None:
    await goto_reporting_proxy_errors(
        _SilentPage(), _FEED, wait_until="domcontentloaded"
    )


async def _baseline_stall() -> None:
    raise PlaywrightTimeoutError(_BASELINE_TIMEOUT)


def _found(profile: Path, cookie_path: Path) -> tuple[list[str], str]:
    files = sorted(p.relative_to(profile).as_posix() for p in profile.rglob("*"))
    return files if profile.exists() else ["<absent>"], cookie_path.read_text()


def _validation(
    outcomes: tuple[str, ...],
    stall: Callable[[], Awaitable[None]],
    launched: list[tuple[str, Any]],
) -> Callable[[Path, Path], Awaitable[bool]]:
    """One browser per entry of *outcomes*; each records what it found.

    ``stall`` leaves debris and then stalls, ``late`` sends its request and gets
    no answer, ``accept`` and ``reject`` finish the check.
    """

    async def validate(cookie_path: Path, profile: Path) -> bool:
        outcome = outcomes[len(launched)]
        launched.append((outcome, _found(profile, cookie_path)))
        if outcome == "stall":
            (profile / "Default").mkdir(parents=True, exist_ok=True)
            (profile / "Default" / "Cookies").write_bytes(b"left by a killed browser")
            cookie_path.write_text("[]")
            await stall()
        if outcome == "late":
            raise PlaywrightTimeoutError(
                "Page.goto: Timeout 30000ms exceeded after the request was sent."
            )
        return outcome == "accept"

    return validate


def _candidate(monkeypatch, profile: Path, *outcomes: str) -> list[tuple[str, Any]]:
    monkeypatch.setattr(session, "stage_installed_browser", lambda: None)
    monkeypatch.setattr("linkedin_mcp_server.core.proxy_errors.STARTUP_BUDGET_MS", 50)
    # Something the profile already held, which a fresh one keeps.
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "kept").write_text("before staging")
    launched: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        _validation(outcomes, _candidate_stall, launched),
    )
    return launched


async def test_a_browser_that_never_sends_is_replaced_on_a_fresh_profile(
    isolate_profile_dir, monkeypatch
):
    launched = _candidate(monkeypatch, isolate_profile_dir, "stall", "accept")
    staged = await session.stage_signed_in_session(isolate_profile_dir)
    assert source_state_path(isolate_profile_dir).exists()
    [(_, first), (_, second)] = launched
    assert second == first, "the second browser opened what the first one left"
    assert first[0] == ["kept"]
    assert staged.li_at in second[1]


async def test_every_browser_stalling_fails_after_the_bound(
    isolate_profile_dir, monkeypatch
):
    launched = _candidate(
        monkeypatch, isolate_profile_dir, *["stall"] * (STAGING_LAUNCHES + 1)
    )
    with pytest.raises(StagingError, match="stalled on /feed/"):
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
    launched = _candidate(monkeypatch, isolate_profile_dir, first, "accept")
    with pytest.raises(error):
        await session.stage_signed_in_session(isolate_profile_dir)
    assert [outcome for outcome, _ in launched] == [first]
    assert not source_state_path(isolate_profile_dir).exists()


# --- The frozen baseline's staging script ----------------------------------------


@pytest.fixture
def baseline_stage(monkeypatch) -> ModuleType:
    # Its import points ``sys.path[0]`` at the tests directory, for the
    # baseline's interpreter; here that is undone after the test.
    monkeypatch.setattr(sys, "path", list(sys.path))
    return importlib.import_module("differential.baseline_stage")


def _baseline(
    monkeypatch, module: ModuleType, profile: Path, *outcomes: str
) -> list[tuple[str, Any]]:
    """The script's own steps, with the baseline's validation replaced."""
    write_synthetic_cookie_file(portable_cookie_path(profile))
    profile.mkdir(parents=True, exist_ok=True)
    launched: list[tuple[str, Any]] = []
    monkeypatch.setattr(module, "get_profile_dir", lambda: profile)
    monkeypatch.setattr(module, "stage_installed_browser", lambda: None)
    monkeypatch.setattr(
        module,
        "validate_imported_cookies",
        _validation(outcomes, _baseline_stall, launched),
    )
    return launched


def test_the_baseline_staging_replaces_a_stalled_browser_on_a_fresh_profile(
    isolate_profile_dir, monkeypatch, baseline_stage
):
    launched = _baseline(
        monkeypatch, baseline_stage, isolate_profile_dir, "stall", "accept"
    )
    assert baseline_stage.main(isolate_profile_dir) == 0
    assert source_state_path(isolate_profile_dir).exists()
    [(_, first), (_, second)] = launched
    assert second == first


def test_the_baseline_staging_stops_after_the_bound(
    isolate_profile_dir, monkeypatch, baseline_stage
):
    launched = _baseline(
        monkeypatch,
        baseline_stage,
        isolate_profile_dir,
        *["stall"] * (STAGING_LAUNCHES + 1),
    )
    with pytest.raises(StagingError, match="stalled on /feed/"):
        baseline_stage.main(isolate_profile_dir)
    assert len(launched) == STAGING_LAUNCHES
    assert not source_state_path(isolate_profile_dir).exists()
