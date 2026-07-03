"""Tests for the browser-import orchestrator: ranking, ordered validation, write."""

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.browser_import import orchestrate
from linkedin_mcp_server.browser_import.discovery import BrowserProfile
from linkedin_mcp_server.browser_import.extract import LiAtMeta, LinkedInCookie
from linkedin_mcp_server.browser_import.orchestrate import (
    import_session_from_browser,
    rank_live_profiles,
)
from linkedin_mcp_server.exceptions import (
    CookieDecryptionError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
)


_PLACEHOLDER = Path("/nonexistent")


def _profile(browser="chrome", display="Personal"):
    return BrowserProfile(
        browser=browser,
        browser_label={
            "chrome": "Google Chrome",
            "brave": "Brave",
            "helium": "Helium",
        }.get(browser, browser),
        safe_storage_label="Chrome",
        profile_dir_name="Default",
        display_name=display,
        user_data_root=_PLACEHOLDER,  # unused: extraction/metadata are mocked
        profile_path=_PLACEHOLDER,
        cookies_db=_PLACEHOLDER,
        local_state_path=_PLACEHOLDER,
    )


def _meta(*, expires=-1.0, last_access=0.0, app_bound=False):
    return LiAtMeta(expires=expires, last_access=last_access, app_bound=app_bound)


def _cookie(name, value="v"):
    return LinkedInCookie(
        name=name,
        value=value,
        domain=".linkedin.com",
        path="/",
        expires=-1.0,
        secure=True,
        http_only=True,
        same_site="Lax",
    )


def _patch_meta(monkeypatch, mapping):
    """Patch read_li_at_meta to return mapping[profile] (None when absent)."""
    monkeypatch.setattr(
        orchestrate, "read_li_at_meta", lambda profile: mapping.get(profile)
    )


def test_rank_drops_profiles_without_li_at(monkeypatch):
    with_li = _profile("chrome")
    without = _profile("brave")
    _patch_meta(monkeypatch, {with_li: _meta(last_access=10.0)})  # `without` -> None

    live, skipped = rank_live_profiles([with_li, without])

    assert [p.browser for p, _ in live] == ["chrome"]
    assert skipped == []


def test_rank_drops_expired_li_at(monkeypatch):
    profile = _profile("chrome")
    _patch_meta(monkeypatch, {profile: _meta(expires=1.0)})  # 1970 -> expired

    live, skipped = rank_live_profiles([profile])

    assert live == []
    assert skipped == [(profile, "li_at expired")]


def test_rank_records_app_bound(monkeypatch):
    profile = _profile("chrome")
    _patch_meta(monkeypatch, {profile: _meta(app_bound=True)})

    live, skipped = rank_live_profiles([profile])

    assert live == []
    assert skipped == [(profile, "app-bound encryption")]


def test_rank_orders_by_last_access_desc(monkeypatch):
    older = _profile("chrome", "Old")
    newer = _profile("brave", "New")
    _patch_meta(
        monkeypatch,
        {older: _meta(last_access=100.0), newer: _meta(last_access=999.0)},
    )

    live, _ = rank_live_profiles([older, newer])

    assert [p.browser for p, _ in live] == ["brave", "chrome"]


def test_rank_session_cookie_counts_as_live(monkeypatch):
    profile = _profile("chrome")
    _patch_meta(monkeypatch, {profile: _meta(expires=-1.0, last_access=5.0)})

    live, skipped = rank_live_profiles([profile])

    assert [p.browser for p, _ in live] == ["chrome"]
    assert skipped == []


@pytest.mark.asyncio
async def test_import_writes_full_set_then_persists_source_state(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    cookies = [_cookie("li_at"), _cookie("li_rm"), _cookie("custom_extra")]

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(orchestrate, "extract_linkedin_cookies", lambda p: cookies)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(return_value=True),
    )

    ok = await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert ok is True
    cookie_path = portable_cookie_path(user_data_dir)
    assert cookie_path.exists()
    written = json.loads(cookie_path.read_text())
    assert {c["name"] for c in written} == {"li_at", "li_rm", "custom_extra"}
    assert all("httpOnly" in c and "sameSite" in c for c in written)
    if os.name != "nt":
        assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600
    assert source_state_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_import_persists_synthesized_user_agent(isolate_profile_dir, monkeypatch):
    """The source browser's UA reaches validation AND source-state.json."""
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    ua = "Mozilla/5.0 (test) Chrome/148.0.0.0"

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )
    monkeypatch.setattr(orchestrate, "synthesize_user_agent", lambda p: ua)
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies", validate
    )

    ok = await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert ok is True
    assert validate.await_args is not None
    assert validate.await_args.kwargs.get("user_agent") == ua
    state = json.loads(source_state_path(user_data_dir).read_text())
    assert state["user_agent"] == ua


@pytest.mark.asyncio
async def test_import_tries_next_browser_when_first_rejected(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    fresh = _profile("chrome", "Fresh")  # most recently used, but rejected
    older = _profile("brave", "Older")  # accepted

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [older, fresh]
    )
    _patch_meta(
        monkeypatch,
        {fresh: _meta(last_access=999.0), older: _meta(last_access=1.0)},
    )

    def fake_extract(profile):
        return [_cookie("li_at", profile.browser)]

    monkeypatch.setattr(orchestrate, "extract_linkedin_cookies", fake_extract)
    # Fresh (chrome) tried first and rejected, then older (brave) accepted.
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(side_effect=[False, True]),
    )

    ok = await import_session_from_browser(None, user_data_dir=user_data_dir)

    assert ok is True
    written = json.loads(portable_cookie_path(user_data_dir).read_text())
    # The accepted (brave) session is what ends up on disk.
    assert [c["value"] for c in written] == ["brave"]
    assert source_state_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_import_falls_through_on_unexpected_extract_error(
    isolate_profile_dir, monkeypatch
):
    # An unexpected error (e.g. a locked/corrupt Cookies DB raising sqlite3.Error
    # or an OSError mid-copy) for one ranked profile must not abort the run; the
    # next-freshest browser is still tried.
    user_data_dir = isolate_profile_dir
    broken = _profile("chrome", "Broken")  # most recently used, but extract blows up
    good = _profile("brave", "Good")  # accepted

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [good, broken]
    )
    _patch_meta(
        monkeypatch,
        {broken: _meta(last_access=999.0), good: _meta(last_access=1.0)},
    )

    def fake_extract(profile):
        if profile is broken:
            raise OSError("source Cookies DB unreadable")
        return [_cookie("li_at", profile.browser)]

    monkeypatch.setattr(orchestrate, "extract_linkedin_cookies", fake_extract)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(return_value=True),
    )

    ok = await import_session_from_browser(None, user_data_dir=user_data_dir)

    assert ok is True
    written = json.loads(portable_cookie_path(user_data_dir).read_text())
    assert [c["value"] for c in written] == ["brave"]
    assert source_state_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_import_validation_failure_removes_cookies(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(return_value=False),
    )

    ok = await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert ok is False
    assert not portable_cookie_path(user_data_dir).exists()
    assert not source_state_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_import_no_live_session_raises(isolate_profile_dir, monkeypatch):
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(expires=1.0)})  # expired

    with pytest.raises(NoLinkedInSessionFoundError):
        await import_session_from_browser("chrome", user_data_dir=user_data_dir)


@pytest.mark.asyncio
async def test_import_does_not_block_event_loop(isolate_profile_dir, monkeypatch):
    """The blocking extract runs off the loop so a concurrent coroutine progresses.

    extract_linkedin_cookies stands in for the keychain subprocess + SQLite reads
    and blocks synchronously for a window. If that ran on the event loop thread,
    the ticker below could not advance during the window. Offloading via
    asyncio.to_thread keeps the loop responsive.
    """
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    block_window = 0.3

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})

    def blocking_extract(_profile):
        time.sleep(block_window)  # synchronous, like the real keychain/SQLite work
        return [_cookie("li_at")]

    monkeypatch.setattr(orchestrate, "extract_linkedin_cookies", blocking_extract)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(return_value=True),
    )

    ticks = {"value": 0}

    async def ticker():
        while True:
            ticks["value"] += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    try:
        ok = await import_session_from_browser("chrome", user_data_dir=user_data_dir)
    finally:
        ticker_task.cancel()

    assert ok is True
    # With the offload, the ticker ran many times during the blocking window. If
    # the sync extract executed on the loop, ticks would be ~0 for that window.
    assert ticks["value"] > 5


@pytest.mark.asyncio
async def test_import_live_but_undecryptable_raises_decryption_error(
    isolate_profile_dir, monkeypatch
):
    # A live li_at exists on disk (keychain-free metadata sees it) but no
    # candidate decrypts (e.g. the keychain key is unavailable, as with a
    # mislabeled fork). Must raise CookieDecryptionError -- not return False --
    # so the caller says "couldn't decrypt" instead of "session may be expired".
    user_data_dir = isolate_profile_dir
    profile = _profile("helium")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(orchestrate, "_extract_and_stage", lambda p, path: False)
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies", validate
    )

    with pytest.raises(CookieDecryptionError):
        await import_session_from_browser(None, user_data_dir=user_data_dir)
    validate.assert_not_called()
    assert not portable_cookie_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_import_app_bound_only_raises_decryption_error(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    profile = _profile("brave")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(app_bound=True)})

    with pytest.raises(CookieDecryptionError) as exc:
        await import_session_from_browser(None, user_data_dir=user_data_dir)
    assert "Brave" in str(exc.value)
