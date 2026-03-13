"""Tests for linkedin_mcp_server.drivers.browser runtime-aware auth startup."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.drivers.browser import (
    _feed_auth_succeeds,
    get_or_create_browser,
    reset_browser_for_testing,
)
from linkedin_mcp_server.session_state import (
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


def _make_mock_browser() -> MagicMock:
    browser = MagicMock()
    browser.start = AsyncMock()
    browser.close = AsyncMock()
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
    browser.export_cookies = AsyncMock(return_value=False)
    browser.export_storage_state = AsyncMock(return_value=True)
    return browser


def _write_source_state(tmp_path, *, runtime_id: str, login_generation: str = "gen-1"):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile_dir).write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com"}])
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
async def test_same_runtime_uses_source_profile(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()

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
    assert ctor.call_args.kwargs["user_data_dir"] == tmp_path / "profile"
    source_browser.import_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_runtime_clicks_remember_me_during_feed_validation(tmp_path):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    source_browser = _make_mock_browser()

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
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is source_browser
    assert source_browser.page.goto.await_count == 2
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
async def test_experimental_derived_runtime_reuses_matching_committed_profile(
    tmp_path, monkeypatch
):
    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    derived_profile = _write_runtime_state(tmp_path, "linux-amd64-container")
    derived_browser = _make_mock_browser()
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=derived_browser,
        ) as ctor,
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        result = await get_or_create_browser()

    assert result is derived_browser
    assert ctor.call_args.kwargs["user_data_dir"] == derived_profile
    derived_browser.import_cookies.assert_not_awaited()
    derived_browser.export_storage_state.assert_not_awaited()


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
async def test_experimental_missing_derived_runtime_bridges_and_checkpoint_commits(
    tmp_path, monkeypatch
):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    reopened_browser = _make_mock_browser()
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            side_effect=[first_browser, reopened_browser],
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
    expected_storage = runtime_storage_state_path(
        "linux-amd64-container", tmp_path / "profile"
    )
    assert result is reopened_browser
    assert ctor.call_count == 2
    assert ctor.call_args_list[0].kwargs["user_data_dir"] == expected_profile
    assert ctor.call_args_list[1].kwargs["user_data_dir"] == expected_profile
    first_browser.import_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )
    first_browser.export_storage_state.assert_awaited_once_with(
        expected_storage,
        indexed_db=True,
    )
    first_browser.close.assert_awaited_once()
    runtime_state = json.loads(
        runtime_state_path("linux-amd64-container", tmp_path / "profile").read_text()
    )
    assert runtime_state["source_login_generation"] == "gen-2"
    assert runtime_state["storage_state_path"] == str(expected_storage.resolve())


@pytest.mark.asyncio
async def test_debug_skip_checkpoint_restart_keeps_fresh_bridged_browser(
    tmp_path, monkeypatch
):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")
    monkeypatch.setenv("LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART", "1")

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

    assert result is first_browser
    assert ctor.call_count == 1
    first_browser.import_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )
    first_browser.export_storage_state.assert_not_awaited()
    first_browser.close.assert_not_awaited()
    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()


@pytest.mark.asyncio
async def test_debug_bridge_every_startup_skips_matching_committed_profile(
    tmp_path, monkeypatch
):
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
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_EVERY_STARTUP", "1")
    monkeypatch.setenv("LINKEDIN_DEBUG_SKIP_CHECKPOINT_RESTART", "1")

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
async def test_experimental_stale_derived_runtime_rebuilds_from_new_generation(
    tmp_path, monkeypatch
):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-3"
    )
    stale_profile = _write_runtime_state(
        tmp_path,
        "linux-amd64-container",
        source_login_generation="old-gen",
    )
    old_marker = stale_profile / "stale.txt"
    old_marker.write_text("stale")
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    reopened_browser = _make_mock_browser()
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            side_effect=[first_browser, reopened_browser],
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await get_or_create_browser()

    assert not old_marker.exists()
    runtime_state = json.loads(
        runtime_state_path("linux-amd64-container", tmp_path / "profile").read_text()
    )
    assert runtime_state["source_login_generation"] == "gen-3"


@pytest.mark.asyncio
async def test_experimental_matching_derived_runtime_failure_does_not_fallback_to_bridge(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.core import AuthenticationError

    _write_source_state(tmp_path, runtime_id="macos-arm64-host")
    _write_runtime_state(tmp_path, "linux-amd64-container")
    invalid_browser = _make_mock_browser()
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            return_value=invalid_browser,
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value="login title: linkedin login",
        ),
        pytest.raises(AuthenticationError),
    ):
        await get_or_create_browser()

    invalid_browser.import_cookies.assert_not_awaited()


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
async def test_experimental_checkpoint_reopen_failure_clears_runtime_dir(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.core import AuthenticationError

    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    reopened_browser = _make_mock_browser()
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    barrier_mock = AsyncMock(side_effect=[None, "checkpoint"])
    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            side_effect=[first_browser, reopened_browser],
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            barrier_mock,
        ),
        pytest.raises(AuthenticationError),
    ):
        await get_or_create_browser()

    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
    assert not runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
    reopened_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_experimental_reopen_start_failure_closes_reopened_browser(
    tmp_path, monkeypatch
):
    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    reopened_browser = _make_mock_browser()
    reopened_browser.start = AsyncMock(side_effect=RuntimeError("reopen failed"))
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    with (
        patch(
            "linkedin_mcp_server.drivers.browser.get_runtime_id",
            return_value="linux-amd64-container",
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.BrowserManager",
            side_effect=[first_browser, reopened_browser],
        ),
        patch(
            "linkedin_mcp_server.drivers.browser.detect_auth_barrier_quick",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(RuntimeError, match="reopen failed"),
    ):
        await get_or_create_browser()

    reopened_browser.close.assert_awaited_once()
    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
    assert not runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()


@pytest.mark.asyncio
async def test_experimental_bridge_validation_failure_before_commit_clears_runtime_dir(
    tmp_path, monkeypatch
):
    from linkedin_mcp_server.core import AuthenticationError

    _write_source_state(
        tmp_path, runtime_id="macos-arm64-host", login_generation="gen-2"
    )
    first_browser = _make_mock_browser()
    first_browser.import_cookies = AsyncMock(return_value=True)
    monkeypatch.setenv("LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION", "1")

    barrier_mock = AsyncMock(return_value="login title: linkedin login")
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
            barrier_mock,
        ),
        pytest.raises(AuthenticationError),
    ):
        await get_or_create_browser()

    assert not runtime_state_path(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
    assert not runtime_profile_dir(
        "linux-amd64-container", tmp_path / "profile"
    ).exists()
