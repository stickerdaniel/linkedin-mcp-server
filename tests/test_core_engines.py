"""Tests for the engine adapter registry (core.engines).

Both BrowserManager's launch path and any cleanup/reset code resolve an
engine's on-disk profile directory through ENGINES[engine].profile_dir() --
these tests pin that single source of truth so it can't silently drift
between call sites again (see the profile-wipe incident these adapters
replaced the scattered `if engine == "camoufox"` branches for)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import linkedin_mcp_server.core.engines as engines_module
from linkedin_mcp_server.core.engines import ENGINES, CamoufoxAdapter, PatchrightAdapter


def _launch_kwargs(tmp_path):
    return {
        "user_data_dir": tmp_path,
        "headless": True,
        "slow_mo": 0,
        "viewport": {"width": 1280, "height": 720},
        "user_agent": None,
        "launch_options": {},
        "camoufox_identity_path": tmp_path / "camoufox-identity.json",
        "expected_camoufox_identity_sha256": None,
    }


def _mock_failed_persistent_launch(monkeypatch, engine_name, failure):
    driver = MagicMock()
    driver.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=driver)

    if engine_name == "patchright":
        import patchright.async_api

        driver.chromium = MagicMock()
        driver.chromium.launch_persistent_context = AsyncMock(side_effect=failure)
        monkeypatch.setattr(
            patchright.async_api,
            "async_playwright",
            MagicMock(return_value=starter),
        )
    else:
        import camoufox.async_api
        import camoufox.utils
        import playwright.async_api as playwright_async_api

        monkeypatch.setattr(
            playwright_async_api,
            "async_playwright",
            MagicMock(return_value=starter),
        )
        monkeypatch.setattr(
            camoufox.async_api,
            "AsyncNewBrowser",
            AsyncMock(side_effect=failure),
        )
        monkeypatch.setattr(
            camoufox.utils,
            "launch_options",
            MagicMock(
                return_value={
                    "env": {
                        "CAMOU_CONFIG_1": json.dumps(
                            {
                                "navigator.userAgent": "Mozilla/5.0 Firefox/135.0",
                                "navigator.platform": "Linux x86_64",
                                "navigator.oscpu": "Linux x86_64",
                            }
                        )
                    },
                    "firefox_user_prefs": {},
                }
            ),
        )

    return driver


def test_engines_registry_has_both_adapters():
    assert set(ENGINES) == {"patchright", "camoufox"}
    assert isinstance(ENGINES["patchright"], PatchrightAdapter)
    assert isinstance(ENGINES["camoufox"], CamoufoxAdapter)


def test_patchright_profile_dir_is_user_data_dir_root(tmp_path):
    assert ENGINES["patchright"].profile_dir(tmp_path) == tmp_path


def test_camoufox_profile_dir_is_namespaced_subdirectory(tmp_path):
    resolved = ENGINES["camoufox"].profile_dir(tmp_path)
    assert resolved == tmp_path / "camoufox"
    # Never the shared root itself -- that's the exact incident this
    # namespacing prevents (a Camoufox reset must never touch it).
    assert resolved != tmp_path


def test_adapters_never_resolve_to_the_same_directory(tmp_path):
    assert ENGINES["patchright"].profile_dir(tmp_path) != ENGINES[
        "camoufox"
    ].profile_dir(tmp_path)


def test_patchright_needs_managed_install_unless_chrome_path_set():
    assert ENGINES["patchright"].needs_managed_install(None) is True
    assert ENGINES["patchright"].needs_managed_install("/usr/bin/chrome") is False


def test_camoufox_never_needs_managed_install():
    assert ENGINES["camoufox"].needs_managed_install(None) is False
    assert ENGINES["camoufox"].needs_managed_install("/usr/bin/chrome") is False


def test_supports_indexed_db_flags():
    assert ENGINES["patchright"].supports_indexed_db is True
    assert ENGINES["camoufox"].supports_indexed_db is False


def test_timeout_error_classes_are_distinct_between_engines():
    patchright_classes = set(ENGINES["patchright"].timeout_error_classes)
    camoufox_classes = set(ENGINES["camoufox"].timeout_error_classes)
    assert patchright_classes
    assert camoufox_classes
    assert patchright_classes.isdisjoint(camoufox_classes)


@pytest.mark.asyncio
async def test_camoufox_launch_uses_sanitized_stable_from_options(
    tmp_path, monkeypatch
):
    import camoufox.async_api
    import camoufox.utils
    import playwright.async_api as playwright_async_api

    driver = MagicMock()
    driver.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=driver)
    monkeypatch.setattr(
        playwright_async_api,
        "async_playwright",
        MagicMock(return_value=starter),
    )
    current_config = {
        "navigator.userAgent": "Mozilla/5.0 Firefox/135.0",
        "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64",
        "canvas:seed": 123,
        "geolocation:latitude": 20.0,
    }
    generated_options = {
        "headless": True,
        "user_data_dir": str(tmp_path / "camoufox"),
        "env": {
            "CURRENT_SECRET": "not-on-disk",
            "CAMOU_CONFIG_1": json.dumps(current_config),
        },
        "firefox_user_prefs": {"webgl.enable-webgl2": True},
    }
    generate = MagicMock(return_value=generated_options)
    launch = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(camoufox.utils, "launch_options", generate)
    monkeypatch.setattr(camoufox.async_api, "AsyncNewBrowser", launch)
    identity_path = tmp_path / "camoufox-identity.json"
    kwargs = _launch_kwargs(tmp_path)
    kwargs["launch_options"] = {
        "env": {
            "CURRENT_SECRET": "not-on-disk",
            "CAMOU_CONFIG_1": "stale-parent-value",
        }
    }

    returned_driver, context = await ENGINES["camoufox"].launch(**kwargs)

    assert returned_driver is driver
    assert context is launch.return_value
    generated_env = generate.call_args.kwargs["env"]
    assert generated_env == {"CURRENT_SECRET": "not-on-disk"}
    launch.assert_awaited_once()
    assert launch.await_args is not None
    launch_kwargs = launch.await_args.kwargs
    assert set(launch_kwargs) == {"from_options", "persistent_context"}
    assert launch_kwargs["persistent_context"] is True
    assert launch_kwargs["from_options"]["user_data_dir"] == str(tmp_path / "camoufox")
    assert launch_kwargs["from_options"]["env"]["CURRENT_SECRET"] == ("not-on-disk")
    assert "not-on-disk" not in identity_path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_name", ["patchright", "camoufox"])
async def test_failed_persistent_launch_stops_playwright_and_preserves_error(
    tmp_path, monkeypatch, engine_name
):
    failure = RuntimeError("persistent launch failed")
    playwright = _mock_failed_persistent_launch(monkeypatch, engine_name, failure)

    with pytest.raises(RuntimeError) as exc_info:
        await ENGINES[engine_name].launch(**_launch_kwargs(tmp_path))

    assert exc_info.value is failure
    playwright.stop.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_name", ["patchright", "camoufox"])
async def test_cancelled_persistent_launch_stops_playwright_and_preserves_cancellation(
    tmp_path, monkeypatch, engine_name
):
    launch_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def blocked_launch(*_args, **_kwargs):
        launch_started.set()
        await never_finishes.wait()

    playwright = _mock_failed_persistent_launch(
        monkeypatch, engine_name, blocked_launch
    )
    launch_task = asyncio.create_task(
        ENGINES[engine_name].launch(**_launch_kwargs(tmp_path))
    )
    await launch_started.wait()
    launch_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await launch_task

    assert launch_task.cancelled()
    playwright.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_launch_bounds_teardown_and_preserves_original_error(
    tmp_path, monkeypatch
):
    failure = RuntimeError("persistent launch failed")
    playwright = _mock_failed_persistent_launch(monkeypatch, "patchright", failure)
    never_finishes = asyncio.Event()
    playwright.stop = AsyncMock(side_effect=never_finishes.wait)
    monkeypatch.setattr(engines_module, "_PLAYWRIGHT_STOP_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(RuntimeError) as exc_info:
        await ENGINES["patchright"].launch(**_launch_kwargs(tmp_path))

    assert exc_info.value is failure
    assert engines_module.launch_teardown_was_confirmed(exc_info.value) is False
    playwright.stop.assert_awaited_once_with()
