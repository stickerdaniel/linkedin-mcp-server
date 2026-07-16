"""Tests for the browser-import orchestrator: ranking, ordered validation, write."""

import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.browser_import import orchestrate
from linkedin_mcp_server.browser_import.discovery import BrowserProfile
from linkedin_mcp_server.browser_import.extract import LiAtMeta, LinkedInCookie
from linkedin_mcp_server.browser_import.orchestrate import (
    import_session_from_browser,
    rank_live_profiles,
)
import linkedin_mcp_server.core.camoufox_identity as identity_module
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    BrowserTeardownError,
    NetworkError,
)
from linkedin_mcp_server.exceptions import (
    CookieDecryptionError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
    camoufox_identity_path,
    clear_auth_state,
    portable_cookie_path,
    source_state_path,
    write_source_state,
)
import linkedin_mcp_server.session_state as session_state_module


_PLACEHOLDER = Path("/nonexistent")


def _write_valid_identity(path: Path, marker: str) -> tuple[bytes, str]:
    config = {
        "navigator.userAgent": "Mozilla/5.0 Firefox/135.0",
        "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64",
        "fonts:spacing_seed": marker,
    }
    artifact = identity_module._new_artifact(
        {
            "env": {"CAMOU_CONFIG_1": json.dumps(config, separators=(",", ":"))},
            "firefox_user_prefs": {"webgl.enable-webgl2": True},
        }
    )
    payload = json.dumps(artifact, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload, artifact["identity_sha256"]


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


@pytest.fixture(autouse=True)
def _default_browser_engine(monkeypatch):
    """Keep orchestrator tests independent from pytest's own CLI arguments."""
    monkeypatch.setattr(
        orchestrate.config_module,
        "get_config",
        lambda: SimpleNamespace(browser=SimpleNamespace(browser_engine="patchright")),
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
async def test_import_commits_the_post_feed_snapshot_written_by_validator(
    isolate_profile_dir, monkeypatch
):
    profile = _profile("chrome")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate,
        "extract_linkedin_cookies",
        lambda p: [
            _cookie("li_at", "before-feed"),
            _cookie("JSESSIONID", "before-feed"),
            _cookie("custom_extra", "preserved"),
        ],
    )

    async def validate_and_rotate(cookie_path, _profile_dir, **_kwargs):
        cookie_path.write_text(
            json.dumps(
                [
                    _cookie("li_at", "after-feed").to_playwright(),
                    _cookie("custom_extra", "preserved").to_playwright(),
                    _cookie("new_after_feed", "new").to_playwright(),
                ]
            )
        )
        return True

    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(side_effect=validate_and_rotate),
    )

    assert (
        await import_session_from_browser("chrome", user_data_dir=isolate_profile_dir)
        is True
    )

    committed = json.loads(portable_cookie_path(isolate_profile_dir).read_text())
    by_name = {cookie["name"]: cookie["value"] for cookie in committed}
    assert by_name == {
        "li_at": "after-feed",
        "custom_extra": "preserved",
        "new_after_feed": "new",
    }


@pytest.mark.asyncio
async def test_import_rolls_back_canonical_pair_when_source_state_write_fails(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    canonical_cookie = portable_cookie_path(user_data_dir)
    canonical_state = source_state_path(user_data_dir)
    canonical_identity = camoufox_identity_path(user_data_dir)
    old_cookie = b'[{"canonical-cookie":"exact bytes"}]\n'
    old_state = b'{"canonical-state":"exact bytes"}\n'
    old_identity, _ = _write_valid_identity(canonical_identity, "old")
    canonical_cookie.write_bytes(old_cookie)
    canonical_state.write_bytes(old_state)

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(return_value=True),
    )

    def fail_after_overwriting_state(*args, **kwargs):
        del args, kwargs
        canonical_state.write_bytes(b'{"partial-new-state":true}')
        raise OSError("fault-injected source-state write")

    monkeypatch.setattr(
        session_state_module,
        "write_source_state",
        fail_after_overwriting_state,
    )

    with pytest.raises(OSError, match="fault-injected"):
        await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert canonical_cookie.read_bytes() == old_cookie
    assert canonical_state.read_bytes() == old_state
    assert canonical_identity.read_bytes() == old_identity
    assert not list(user_data_dir.parent.glob(".import-pending-*"))


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
async def test_import_removes_decrypted_pending_cookies_when_ua_synthesis_fails(
    isolate_profile_dir, monkeypatch
):
    profile = _profile("chrome")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )

    def fail_ua(_profile):
        raise OSError("UA read failed")

    monkeypatch.setattr(orchestrate, "synthesize_user_agent", fail_ua)

    with pytest.raises(OSError, match="UA read failed"):
        await import_session_from_browser("chrome", user_data_dir=isolate_profile_dir)

    assert not list(isolate_profile_dir.parent.glob(".import-pending-*"))


@pytest.mark.asyncio
async def test_camoufox_import_rejected_before_browser_discovery(
    isolate_profile_dir, monkeypatch
):
    monkeypatch.setattr(
        orchestrate.config_module,
        "get_config",
        lambda: SimpleNamespace(browser=SimpleNamespace(browser_engine="camoufox")),
    )
    discover = AsyncMock()
    monkeypatch.setattr(orchestrate, "_discover_and_rank", discover)

    with pytest.raises(AuthenticationError, match="--browser camoufox --login"):
        await import_session_from_browser("chrome", user_data_dir=isolate_profile_dir)

    discover.assert_not_awaited()


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
async def test_import_validation_rejection_preserves_canonical_triplet_byte_for_byte(
    isolate_profile_dir, monkeypatch
):
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    canonical_cookie = portable_cookie_path(user_data_dir)
    canonical_state = source_state_path(user_data_dir)
    canonical_identity = camoufox_identity_path(user_data_dir)
    old_cookie = b'[{"canonical-cookie":"exact bytes"}]\n'
    old_identity, digest = _write_valid_identity(canonical_identity, "canonical")
    write_source_state(user_data_dir, camoufox_identity_sha256=digest)
    old_state = canonical_state.read_bytes()
    canonical_cookie.write_bytes(old_cookie)

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )

    async def reject_pending(cookie_path, validation_profile, **kwargs):
        assert cookie_path.parent.name.startswith(".import-pending-")
        assert validation_profile.parent == cookie_path.parent
        assert "camoufox_identity_path" not in kwargs
        return False

    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(side_effect=reject_pending),
    )

    ok = await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert ok is False
    assert canonical_cookie.read_bytes() == old_cookie
    assert canonical_state.read_bytes() == old_state
    assert canonical_identity.read_bytes() == old_identity
    assert not list(user_data_dir.parent.glob(".import-pending-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "pending_retained"),
    (
        (NetworkError("temporary DNS failure"), False),
        (BrowserTeardownError("teardown uncertain"), True),
    ),
    ids=("confirmed-teardown", "uncertain-teardown"),
)
async def test_import_validation_error_preserves_canonical_and_handles_pending_safely(
    isolate_profile_dir, monkeypatch, failure, pending_retained
):
    """A NetworkError (dead browser/driver connection) during validation says
    nothing about whether the cookie is actually good -- it must NOT be
    treated like a real rejection: no unlink, no profile reset."""
    user_data_dir = isolate_profile_dir
    profile = _profile("chrome")
    canonical_cookie = portable_cookie_path(user_data_dir)
    canonical_state = source_state_path(user_data_dir)
    canonical_identity = camoufox_identity_path(user_data_dir)
    old_cookie = b'[{"canonical-cookie":"exact bytes"}]\n'
    old_identity, digest = _write_valid_identity(canonical_identity, "canonical")
    write_source_state(user_data_dir, camoufox_identity_sha256=digest)
    old_state = canonical_state.read_bytes()
    canonical_cookie.write_bytes(old_cookie)

    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    monkeypatch.setattr(
        orchestrate, "extract_linkedin_cookies", lambda p: [_cookie("li_at")]
    )

    async def fail_pending(cookie_path, validation_profile, **kwargs):
        assert cookie_path.parent.name.startswith(".import-pending-")
        assert validation_profile.parent == cookie_path.parent
        assert "camoufox_identity_path" not in kwargs
        raise failure

    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies",
        AsyncMock(side_effect=fail_pending),
    )

    with pytest.raises(type(failure), match=str(failure)):
        await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    assert canonical_cookie.read_bytes() == old_cookie
    assert canonical_state.read_bytes() == old_state
    assert canonical_identity.read_bytes() == old_identity
    pending = list(user_data_dir.parent.glob(".import-pending-*"))
    if pending_retained:
        assert len(pending) == 1
        assert (pending[0] / "cookies.json").exists()
        assert not (pending[0] / "camoufox-identity.json").exists()
        assert await clear_auth_state(user_data_dir) is False
        assert pending[0].exists()
    else:
        assert pending == []


@pytest.mark.asyncio
async def test_import_failure_never_touches_sibling_engine_profile(
    isolate_profile_dir, monkeypatch
):
    """Isolated validation never opens or resets either canonical engine profile."""
    user_data_dir = isolate_profile_dir
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # Patchright's profile lives directly under user_data_dir (e.g. its
    # Default/ subdir); Camoufox's lives namespaced in user_data_dir/camoufox.
    patchright_marker = user_data_dir / "Default" / "Cookies"
    patchright_marker.parent.mkdir(parents=True, exist_ok=True)
    patchright_marker.write_text("patchright profile data")

    camoufox_marker = user_data_dir / "camoufox" / "cookies.sqlite"
    camoufox_marker.parent.mkdir(parents=True, exist_ok=True)
    camoufox_marker.write_text("camoufox profile data")

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
        AsyncMock(return_value=False),  # a real, confirmed rejection
    )

    await import_session_from_browser("chrome", user_data_dir=user_data_dir)

    # The sibling engine's profile is completely untouched.
    assert patchright_marker.exists()
    assert patchright_marker.read_text() == "patchright profile data"
    assert camoufox_marker.read_text() == "camoufox profile data"
    assert not list(user_data_dir.parent.glob("invalid-state-*"))


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
    monkeypatch.setattr(orchestrate, "_extract_cookies", lambda p: None)
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.validate_imported_cookies", validate
    )

    with pytest.raises(CookieDecryptionError):
        await import_session_from_browser(None, user_data_dir=user_data_dir)
    validate.assert_not_called()
    assert not portable_cookie_path(user_data_dir).exists()


@pytest.mark.asyncio
async def test_cancelled_worker_cannot_publish_cookies_after_source_lock_release(
    isolate_profile_dir, monkeypatch
):
    profile = _profile("chrome")
    monkeypatch.setattr(
        orchestrate, "discover_profiles", lambda browser=None: [profile]
    )
    _patch_meta(monkeypatch, {profile: _meta(last_access=10.0)})
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_extract(_profile):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return [_cookie("li_at")]

    monkeypatch.setattr(orchestrate, "extract_linkedin_cookies", blocked_extract)
    task = asyncio.create_task(
        import_session_from_browser("chrome", user_data_dir=isolate_profile_dir)
    )
    assert await asyncio.to_thread(started.wait, 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)

    assert not portable_cookie_path(isolate_profile_dir).exists()


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
