"""Tests for linkedin_mcp_server.drivers.browser runtime-aware auth startup."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.drivers.browser import (
    _feed_auth_succeeds,
    _launch_options,
    close_browser,
    get_or_create_browser,
    reset_browser_for_testing,
    validate_imported_cookies,
)
import linkedin_mcp_server.drivers.browser as browser_module
from linkedin_mcp_server.session_state import (
    get_runtime_instance_id,
    portable_cookie_path,
    runtime_profile_dir,
    runtime_state_path,
    runtime_storage_state_path,
    source_state_path,
)


@pytest.fixture(autouse=True)
def _reset_browser():
    reset_browser_for_testing()
    yield
    reset_browser_for_testing()


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch, tmp_path):
    config = AppConfig()
    config.browser.user_data_dir = str(tmp_path / "profile")
    monkeypatch.setattr(
        "linkedin_mcp_server.drivers.browser.get_config", lambda: config
    )
    return config


def test_launch_options_includes_proxy_when_configured(_mock_config):
    _mock_config.browser.proxy_server = "http://proxy.example:8080"
    _mock_config.browser.proxy_username = "user"
    _mock_config.browser.proxy_password = "secret"

    launch_options, _viewport = _launch_options()

    assert launch_options["proxy"] == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "secret",
    }


def test_launch_options_omits_proxy_when_unconfigured(_mock_config):
    launch_options, _viewport = _launch_options()

    assert "proxy" not in launch_options


def test_launch_options_never_logs_proxy_password(_mock_config, caplog):
    _mock_config.browser.proxy_server = "http://proxy.example:8080"
    _mock_config.browser.proxy_password = "super-secret-password"

    with caplog.at_level("INFO"):
        _launch_options()

    assert "super-secret-password" not in caplog.text
    assert "proxy.example:8080" in caplog.text


def test_launch_options_disables_camoufox_humanize_for_no_stealth(_mock_config):
    _mock_config.browser.browser_engine = "camoufox"
    _mock_config.browser.stealth_profile = "NO_STEALTH"

    launch_options, _viewport = _launch_options()

    assert launch_options["humanize"] is False


def test_launch_options_omits_humanize_key_for_stealthy_camoufox_profiles(
    _mock_config,
):
    """CamoufoxAdapter.launch() already defaults humanize=True before
    spreading launch_options -- omitting the key (not setting it True) lets
    that default keep working unchanged for every non-NO_STEALTH profile."""
    _mock_config.browser.browser_engine = "camoufox"
    _mock_config.browser.stealth_profile = "MAXIMUM_STEALTH"

    launch_options, _viewport = _launch_options()

    assert "humanize" not in launch_options


def test_launch_options_never_sets_humanize_key_on_patchright(_mock_config):
    """ "humanize" isn't a recognized launch_persistent_context() kwarg --
    setting it for Patchright would break browser launch outright, not just
    be a no-op (see PatchrightAdapter.launch(), which spreads
    **launch_options directly into that call)."""
    _mock_config.browser.browser_engine = "patchright"
    _mock_config.browser.stealth_profile = "NO_STEALTH"

    launch_options, _viewport = _launch_options()

    assert "humanize" not in launch_options


class TestMakeBrowserUserAgent:
    """_make_browser() preserves the UA that owns the source session."""

    @pytest.fixture(autouse=True)
    def _mock_browser_manager(self, monkeypatch):
        captured = {}

        def fake_browser_manager(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            fake_browser_manager,
        )
        return captured

    def test_explicit_config_user_agent_wins_over_everything(
        self, _mock_config, _mock_browser_manager, tmp_path
    ):
        _mock_config.browser.user_agent = "explicit-ua"
        from linkedin_mcp_server.drivers.browser import _make_browser

        _make_browser(tmp_path, launch_options={}, viewport={"width": 1, "height": 1})

        assert _mock_browser_manager["user_agent"] == "explicit-ua"

    def test_imported_session_user_agent_is_preserved(
        self, _mock_config, _mock_browser_manager, tmp_path
    ):
        from linkedin_mcp_server.drivers.browser import _make_browser

        _make_browser(
            tmp_path,
            launch_options={},
            viewport={"width": 1, "height": 1},
            user_agent="source-browser-ua",
        )

        assert _mock_browser_manager["user_agent"] == "source-browser-ua"

    @pytest.mark.parametrize("configured", [False, True])
    def test_camoufox_uses_native_identity(
        self, _mock_config, _mock_browser_manager, tmp_path, configured
    ):
        _mock_config.browser.browser_engine = "camoufox"
        if configured:
            _mock_config.browser.user_agent = "configured-chrome-ua"
        from linkedin_mcp_server.drivers.browser import _make_browser

        _make_browser(
            tmp_path,
            launch_options={},
            viewport={"width": 1, "height": 1},
            user_agent="source-chrome-ua",
        )

        assert _mock_browser_manager["user_agent"] is None

    @pytest.mark.parametrize("stealth_profile", ["NO_STEALTH", "MINIMAL_STEALTH"])
    def test_unset_ua_uses_engine_native_identity(
        self,
        _mock_config,
        _mock_browser_manager,
        tmp_path,
        stealth_profile,
    ):
        _mock_config.browser.stealth_profile = stealth_profile
        from linkedin_mcp_server.drivers.browser import _make_browser

        _make_browser(tmp_path, launch_options={}, viewport={"width": 1, "height": 1})

        assert _mock_browser_manager["user_agent"] is None


def _make_mock_browser() -> MagicMock:
    browser = MagicMock()
    browser.start = AsyncMock()
    browser.close = AsyncMock(return_value=True)
    browser.page = MagicMock()
    browser.page.url = "https://www.linkedin.com/feed/"
    browser.page.goto = AsyncMock()
    browser.page.set_default_timeout = MagicMock()
    browser.page.title = AsyncMock(return_value="LinkedIn")
    browser.page.evaluate = AsyncMock(return_value="Feed")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    browser.page.locator = MagicMock(return_value=locator)
    browser.import_cookies = AsyncMock(return_value=False)
    browser.refresh_imported_cookie_snapshot = AsyncMock(return_value=True)
    browser.export_cookies = AsyncMock(return_value=False)
    browser.export_storage_state = AsyncMock(return_value=True)
    return browser


def _write_source_state(
    tmp_path,
    *,
    runtime_id: str,
    login_generation: str = "gen-1",
    camoufox_identity_sha256: str | None = None,
):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile_dir).write_text(
        json.dumps(
            [
                {
                    "name": "li_at",
                    "domain": ".linkedin.com",
                    "value": "session",
                }
            ]
        )
    )
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": runtime_id,
                "login_generation": login_generation,
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(portable_cookie_path(profile_dir)),
                "camoufox_identity_sha256": camoufox_identity_sha256,
            }
        )
    )
    return profile_dir


def _write_runtime_state(
    tmp_path,
    runtime_id: str,
    *,
    source_runtime_id: str = "macos-arm64-host",
    source_login_generation: str = "gen-1",
    with_storage_state: bool = True,
):
    profile_dir = runtime_profile_dir(runtime_id, tmp_path / "profile")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    storage_state_path = runtime_storage_state_path(runtime_id, tmp_path / "profile")
    if with_storage_state:
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        storage_state_path.write_text("{}")
    runtime_state_path(runtime_id, tmp_path / "profile").write_text(
        json.dumps(
            {
                "version": 1,
                "runtime_id": runtime_id,
                "source_runtime_id": source_runtime_id,
                "source_login_generation": source_login_generation,
                "created_at": "2026-03-12T17:10:00Z",
                "committed_at": "2026-03-12T17:10:05Z",
                "profile_path": str(profile_dir),
                "storage_state_path": str(storage_state_path),
                "commit_method": "checkpoint_restart",
            }
        )
    )
    return profile_dir


@pytest.mark.asyncio
async def test_get_or_create_browser_requires_source_state():
    from linkedin_mcp_server.core import AuthenticationError

    with pytest.raises(AuthenticationError):
        await get_or_create_browser()


@pytest.mark.asyncio
async def test_camoufox_rejects_legacy_source_without_bound_identity(
    tmp_path, _mock_config
):
    _mock_config.browser.browser_engine = "camoufox"
    _write_source_state(tmp_path, runtime_id="linux-amd64-host")

    with pytest.raises(AuthenticationError, match="not bound to a Camoufox identity"):
        await get_or_create_browser()


@pytest.mark.asyncio
async def test_camoufox_bridge_reuses_source_identity_digest(tmp_path, _mock_config):
    _mock_config.browser.browser_engine = "camoufox"
    digest = "d" * 64
    _write_source_state(
        tmp_path,
        runtime_id="linux-amd64-host",
        camoufox_identity_sha256=digest,
    )
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ) as constructor,
        patch(
            "linkedin_mcp_server.drivers.browser.load_camoufox_identity_sha256",
            return_value=digest,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await get_or_create_browser() is browser

    kwargs = constructor.call_args.kwargs
    assert kwargs["camoufox_identity_path"] == tmp_path / "camoufox-identity.json"
    assert kwargs["expected_camoufox_identity_sha256"] == digest


@pytest.mark.asyncio
async def test_same_runtime_uses_isolated_profile_not_source(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()
    source_browser.import_cookies = AsyncMock(return_value=True)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    ctor.assert_called_once()
    assert ctor.call_args.kwargs["user_data_dir"] == runtime_profile_dir(
        "macos-arm64-host", tmp_path / "profile"
    )
    assert ctor.call_args.kwargs["user_data_dir"] != tmp_path / "profile"
    source_browser.import_cookies.assert_awaited_once()
    source_browser.refresh_imported_cookie_snapshot.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )


@pytest.mark.asyncio
async def test_bridge_fails_closed_when_rotated_cookies_cannot_be_saved(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    browser.refresh_imported_cookie_snapshot = AsyncMock(return_value=False)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(NetworkError, match="post-feed cookie snapshot"),
    ):
        await get_or_create_browser()

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_runtime_clicks_remember_me_during_feed_validation(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()
    source_browser.import_cookies = AsyncMock(return_value=True)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.wait_for_session_resume_redirect",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    # One pre-import navigation plus the initial and post-interstitial checks.
    assert source_browser.page.goto.await_count == 3
    assert remember_me.await_count == 1


@pytest.mark.asyncio
async def test_feed_auth_retries_feed_after_remember_me_error_recovery():
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=[Exception("net::ERR_TOO_MANY_REDIRECTS"), None]
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await _feed_auth_succeeds(browser) is True

    assert browser.page.goto.await_count == 2
    remember_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_feed_auth_retries_after_cookie_backed_resume_navigation_error():
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=[Exception("net::ERR_TOO_MANY_REDIRECTS"), None]
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.wait_for_session_resume_redirect",
            new_callable=AsyncMock,
            return_value=True,
        ) as resume,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await _feed_auth_succeeds(browser) is True

    assert browser.page.goto.await_count == 2
    resume.assert_awaited_once()


@pytest.mark.asyncio
async def test_feed_auth_records_single_post_recovery_trace():
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=[Exception("net::ERR_TOO_MANY_REDIRECTS"), None]
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.record_page_trace",
            new_callable=AsyncMock,
        ) as record_page_trace,
    ):
        assert await _feed_auth_succeeds(browser) is True

    steps = [call.args[1] for call in record_page_trace.await_args_list]
    assert "feed-after-remember-me-error-recovery" in steps
    assert "feed-navigation-error-before-remember-me-retry" not in steps


@pytest.mark.asyncio
async def test_feed_auth_raises_network_error_on_transport_failure():
    """Regression test for the profile-wipe incident: a dead browser/driver
    connection (e.g. the playwright==1.60.0 Firefox driver crash under
    Camoufox) must be reported as NetworkError, never coerced to a plain
    `False` -- callers treat `False` as "LinkedIn rejected the cookie" and
    destructively reset the profile, which is wrong for an inconclusive
    infra failure."""
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(
        side_effect=Exception("Connection closed while reading from the driver")
    )

    with patch(
        "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
        new_callable=AsyncMock,
        return_value=True,
    ) as remember_me:
        with pytest.raises(NetworkError):
            await _feed_auth_succeeds(browser)

    # A dead connection can't be recovered by clicking a remember-me prompt --
    # classification must short-circuit straight to raising, not waste a
    # round-trip attempting recovery on an already-dead page.
    remember_me.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        Exception("net::ERR_NAME_NOT_RESOLVED"),
        Exception("net::ERR_CERT_AUTHORITY_INVALID"),
        Exception("navigation timed out after 30000ms"),
    ],
)
async def test_feed_auth_never_treats_navigation_failure_as_cookie_rejection(
    failure,
):
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(side_effect=failure)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.wait_for_session_resume_redirect",
            new_callable=AsyncMock,
            return_value=False,
        ),
        pytest.raises(NetworkError, match="Feed validation could not complete"),
    ):
        await _feed_auth_succeeds(browser)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://captive.portal.example/login",
        "http://www.linkedin.com/feed/",
        "https://www.linkedin.com:444/feed/",
    ],
)
async def test_feed_auth_rejects_off_origin_redirect_as_inconclusive(redirect_url):
    browser = _make_mock_browser()
    browser.page.url = redirect_url

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ) as detector,
        pytest.raises(NetworkError, match="redirected outside LinkedIn"),
    ):
        await _feed_auth_succeeds(browser)

    remember_me.assert_not_awaited()
    detector.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_auth_never_recovers_navigation_error_off_origin():
    browser = _make_mock_browser()
    browser.page.url = "https://captive.portal.example/login"
    browser.page.goto = AsyncMock(side_effect=Exception("navigation timed out"))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
        ) as remember_me,
        patch(
            "linkedin_mcp_server.drivers.browser.wait_for_session_resume_redirect",
            new_callable=AsyncMock,
        ) as resume,
        pytest.raises(NetworkError, match="redirected outside LinkedIn"),
    ):
        await _feed_auth_succeeds(browser)

    remember_me.assert_not_awaited()
    resume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect_url", "message", "detector_expected"),
    [
        ("https://www.linkedin.com/jobs/", "expected LinkedIn feed", True),
        ("https://help.linkedin.com/feed/", "redirected outside LinkedIn", False),
    ],
)
async def test_feed_auth_requires_expected_main_linkedin_feed(
    redirect_url, message, detector_expected
):
    browser = _make_mock_browser()
    browser.page.url = redirect_url

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.resolve_remember_me_prompt",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.wait_for_session_resume_redirect",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ) as detector,
        pytest.raises(NetworkError, match=message),
    ):
        await _feed_auth_succeeds(browser)

    if detector_expected:
        detector.assert_awaited_once()
    else:
        detector.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_auth_still_returns_false_on_real_auth_barrier():
    """A genuine auth barrier (LinkedIn showing a login page) is a real,
    actionable "not logged in" signal and must keep returning False --
    only transport failures (above) change behavior."""
    browser = _make_mock_browser()

    with patch(
        "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
        new_callable=AsyncMock,
        return_value="login title: sign in | linkedin",
    ):
        assert await _feed_auth_succeeds(browser) is False


@pytest.mark.asyncio
async def test_default_foreign_runtime_bridges_fresh_each_startup(tmp_path):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    _write_runtime_state(
        tmp_path,
        "linux-amd64-container",
        source_login_generation="gen-2",
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=first_browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    expected_profile = runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    )
    assert result is first_browser
    assert ctor.call_count == 1
    assert ctor.call_args.kwargs["user_data_dir"] == expected_profile
    first_browser.import_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )
    first_browser.export_storage_state.assert_not_awaited()
    first_browser.close.assert_not_awaited()
    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()


@pytest.mark.asyncio
async def test_camoufox_bridge_rejects_legacy_unbound_source(tmp_path, _mock_config):
    _mock_config.browser.browser_engine = "camoufox"
    _write_source_state(tmp_path, runtime_id="linux-amd64-host")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-host",
        ),
        patch("linkedin_mcp_server.drivers.browser.BrowserManager") as ctor,
        pytest.raises(AuthenticationError, match="not bound to a Camoufox identity"),
    ):
        await get_or_create_browser()

    ctor.assert_not_called()


@pytest.mark.asyncio
async def test_patchright_rejects_camoufox_bound_source(tmp_path):
    profile_dir = _write_source_state(tmp_path, runtime_id="linux-amd64-host")
    state_path = source_state_path(profile_dir)
    source_state = json.loads(state_path.read_text())
    source_state["camoufox_identity_sha256"] = "a" * 64
    state_path.write_text(json.dumps(source_state))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-host",
        ),
        patch("linkedin_mcp_server.drivers.browser.BrowserManager") as ctor,
        pytest.raises(AuthenticationError, match="minted under Camoufox"),
    ):
        await get_or_create_browser()

    ctor.assert_not_called()


@pytest.mark.asyncio
async def test_camoufox_bridge_reuses_source_bound_identity(tmp_path, _mock_config):
    _mock_config.browser.browser_engine = "camoufox"
    digest = "b" * 64
    profile_dir = _write_source_state(tmp_path, runtime_id="linux-amd64-host")
    state_path = source_state_path(profile_dir)
    source_state = json.loads(state_path.read_text())
    source_state["camoufox_identity_sha256"] = digest
    state_path.write_text(json.dumps(source_state))
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.load_camoufox_identity_sha256",
            return_value=digest,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        assert await get_or_create_browser() is browser

    assert ctor.call_args.kwargs["camoufox_identity_path"] == (
        tmp_path / "camoufox-identity.json"
    )
    assert ctor.call_args.kwargs["expected_camoufox_identity_sha256"] == digest


@pytest.mark.asyncio
async def test_debug_bridge_cookie_set_flows_through_foreign_runtime_bridge(
    tmp_path, monkeypatch
):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=first_browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await get_or_create_browser()

    first_browser.import_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )


@pytest.mark.asyncio
async def test_same_runtime_start_failure_closes_browser(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()
    source_browser.start = AsyncMock(side_effect=RuntimeError("start failed"))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=source_browser,
        ),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        await get_or_create_browser()

    source_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_foreign_runtime_start_failure_closes_browser(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    first_browser = _make_mock_browser()
    first_browser.start = AsyncMock(side_effect=RuntimeError("start failed"))

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=first_browser,
        ),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        await get_or_create_browser()

    first_browser.close.assert_awaited_once()
    assert not runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()


@pytest.mark.asyncio
async def test_close_preserves_uncertain_profile_and_rotates_instance(tmp_path):
    runtime_id = "linux-amd64-host"
    old_profile = runtime_profile_dir(runtime_id, tmp_path / "profile")
    old_profile.mkdir(parents=True)
    (old_profile / "lock-marker").write_text("owned")
    browser = _make_mock_browser()
    browser.close = AsyncMock(return_value=False)
    browser_module._browser = browser
    browser_module._browser_runtime_id = runtime_id
    browser_module._browser_runtime_instance_id = get_runtime_instance_id()

    assert await close_browser() is False

    new_profile = runtime_profile_dir(runtime_id, tmp_path / "profile")
    assert new_profile != old_profile
    assert (old_profile / "lock-marker").read_text() == "owned"


@pytest.mark.asyncio
async def test_close_rotates_namespace_before_cancellable_teardown(tmp_path):
    runtime_id = "linux-amd64-host"
    old_instance_id = get_runtime_instance_id()
    old_profile = runtime_profile_dir(runtime_id, tmp_path / "profile")
    old_profile.mkdir(parents=True)
    (old_profile / "lock-marker").write_text("owned")
    close_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocked_close() -> bool:
        close_started.set()
        await never_finishes.wait()
        return False

    browser = _make_mock_browser()
    browser.close = AsyncMock(side_effect=blocked_close)
    browser_module._browser = browser
    browser_module._browser_runtime_id = runtime_id
    browser_module._browser_runtime_instance_id = old_instance_id

    close_task = asyncio.create_task(close_browser())
    await close_started.wait()
    assert get_runtime_instance_id() != old_instance_id
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert (old_profile / "lock-marker").read_text() == "owned"
    assert runtime_profile_dir(runtime_id, tmp_path / "profile") != old_profile


@pytest.mark.asyncio
async def test_close_reports_runtime_cleanup_failure():
    runtime_id = "linux-amd64-host"
    browser = _make_mock_browser()
    browser_module._browser = browser
    browser_module._browser_runtime_id = runtime_id
    browser_module._browser_runtime_instance_id = get_runtime_instance_id()

    with patch(
        "linkedin_mcp_server.drivers.browser.clear_runtime_instance",
        return_value=False,
    ) as clear:
        assert await close_browser() is False

    browser.close.assert_awaited_once()
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_get_waits_for_inflight_close_before_publishing_new_singleton(tmp_path):
    """close/get cannot overlap and clear a browser created by the other task."""
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    old_browser = _make_mock_browser()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocked_close() -> bool:
        close_started.set()
        await release_close.wait()
        return True

    old_browser.close = AsyncMock(side_effect=blocked_close)
    browser_module._browser = old_browser
    new_browser = _make_mock_browser()
    bridge = AsyncMock(return_value=new_browser)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._bridge_runtime_profile",
            bridge,
        ),
    ):
        close_task = asyncio.create_task(close_browser())
        await asyncio.wait_for(close_started.wait(), timeout=1)
        get_task = asyncio.create_task(get_or_create_browser())
        await asyncio.sleep(0)

        assert not get_task.done()
        bridge.assert_not_awaited()

        release_close.set()
        assert await close_task is True
        assert await get_task is new_browser

    bridge.assert_awaited_once()
    assert browser_module._browser is new_browser


@pytest.mark.asyncio
async def test_fork_discards_inherited_browser_and_locked_lifecycle(
    tmp_path, monkeypatch
):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    parent_browser = _make_mock_browser()
    child_browser = _make_mock_browser()
    inherited_lock = asyncio.Lock()
    await inherited_lock.acquire()
    browser_module._browser = parent_browser
    browser_module._browser_runtime_id = "macos-arm64-host"
    browser_module._browser_runtime_instance_id = get_runtime_instance_id()
    browser_module._browser_lifecycle_lock = inherited_lock
    browser_module._browser_owner_pid = 111
    monkeypatch.setattr(browser_module.os, "getpid", lambda: 222)

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._bridge_runtime_profile",
            new_callable=AsyncMock,
            return_value=child_browser,
        ) as bridge,
    ):
        result = await asyncio.wait_for(get_or_create_browser(), timeout=1)

    assert result is child_browser
    bridge.assert_awaited_once()
    parent_browser.close.assert_not_awaited()
    assert browser_module._browser_lifecycle_lock is not inherited_lock


@pytest.mark.asyncio
@pytest.mark.parametrize("teardown_complete", [True, False])
async def test_bridge_network_error_cleans_only_after_confirmed_teardown(
    tmp_path, teardown_complete
):
    """A transport failure (dead connection) during bridge validation must
    surface as NetworkError, not a generic Exception -- so a caller one
    layer up (e.g. bootstrap.py's auto-import whitelist) can tell an
    inconclusive infra failure apart from a real AuthenticationError.

    The bridge always starts from a fresh process-isolated runtime directory;
    this test pins the exception type rather than cookie validity."""
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.close = AsyncMock(return_value=teardown_complete)
    first_browser.import_cookies = AsyncMock(return_value=True)
    isolated_profile = runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    )

    async def start_with_profile() -> None:
        isolated_profile.mkdir(parents=True)
        (isolated_profile / "private-cookie-marker").write_text("possibly live")

    first_browser.start = AsyncMock(side_effect=start_with_profile)
    # First goto (pre-import feed nav) succeeds; the second (inside
    # _feed_auth_succeeds, post cookie-injection) hits a dead connection.
    first_browser.page.goto = AsyncMock(
        side_effect=[
            None,
            Exception("Connection closed while reading from the driver"),
        ]
    )
    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=first_browser,
        ),
        pytest.raises(NetworkError),
    ):
        await get_or_create_browser()

    assert isolated_profile.exists() is (not teardown_complete)


@pytest.mark.asyncio
async def test_bridge_cookie_injection_disconnect_is_not_auth_rejection(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(
        side_effect=NetworkError(
            "Browser driver failed while injecting cookies: Connection closed"
        )
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="macos-arm64-host",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        pytest.raises(NetworkError, match="Connection closed") as exc_info,
    ):
        await get_or_create_browser()

    assert not isinstance(exc_info.value, AuthenticationError)
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_imported_cookies_returns_feed_result(tmp_path, monkeypatch):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is True
    feed_ok.assert_awaited_once()
    browser.import_cookies.assert_awaited_once_with(
        cookie_path, preset_name="bridge_core"
    )
    browser.refresh_imported_cookie_snapshot.assert_awaited_once_with(
        cookie_path, preset_name="bridge_core"
    )
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_imported_cookies_returns_false_when_feed_auth_fails(
    tmp_path,
):
    # Import succeeds but the session is expired -> feed auth fails. The common
    # real-world case: importable-but-expired cookies.
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=False,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is False
    feed_ok.assert_awaited_once()
    browser.refresh_imported_cookie_snapshot.assert_not_awaited()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_imported_cookies_short_circuits_on_import_failure(
    tmp_path,
):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=False)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ) as feed_ok,
    ):
        result = await validate_imported_cookies(cookie_path, tmp_path / "profile")

    assert result is False
    feed_ok.assert_not_awaited()  # short-circuits before the feed check
    browser.refresh_imported_cookie_snapshot.assert_not_awaited()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_import_fails_closed_when_snapshot_refresh_fails(tmp_path):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    browser.refresh_imported_cookie_snapshot = AsyncMock(return_value=False)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "old"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ),
        pytest.raises(NetworkError, match="post-feed cookie snapshot"),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_import_never_reports_success_when_teardown_is_uncertain(
    tmp_path,
):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    browser.close = AsyncMock(return_value=False)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "old"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ),
        pytest.raises(NetworkError, match="confirm browser teardown"),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    browser.refresh_imported_cookie_snapshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_imported_cookies_closes_browser_on_error(tmp_path):
    browser = _make_mock_browser()
    browser.page.goto = AsyncMock(side_effect=RuntimeError("nav boom"))
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        pytest.raises(RuntimeError, match="nav boom"),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_uses_local_manager_not_singleton(tmp_path):
    browser = _make_mock_browser()
    browser.import_cookies = AsyncMock(return_value=True)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "session"}])
    )

    reset_browser_for_testing()
    with (
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser._feed_auth_succeeds",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await validate_imported_cookies(cookie_path, tmp_path / "profile")

    # The singleton globals must remain untouched by the import validator.
    assert browser_module._browser is None
