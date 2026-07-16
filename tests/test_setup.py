import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.core.camoufox_identity import (
    load_camoufox_identity_sha256,
)
import linkedin_mcp_server.core.camoufox_identity as identity_module
from linkedin_mcp_server.core.exceptions import BrowserTeardownError, NetworkError
from linkedin_mcp_server.session_state import (
    camoufox_identity_path,
    clear_auth_state,
    load_source_state,
    portable_cookie_path,
    write_source_state,
)
from linkedin_mcp_server.setup import interactive_login
import linkedin_mcp_server.session_state as session_state_module
import linkedin_mcp_server.setup as setup_impl


class _BrowserContextManager:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _UncertainTeardownContextManager(_BrowserContextManager):
    async def __aexit__(self, exc_type, exc, tb):
        raise BrowserTeardownError("teardown uncertain")


def _make_browser(*, export_cookies: bool) -> MagicMock:
    browser = MagicMock()
    browser.page = MagicMock()
    browser.page.goto = AsyncMock()
    browser.context = MagicMock()
    browser.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "domain": ".linkedin.com", "value": "session"}]
    )

    async def export(path):
        if export_cookies:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
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
        return export_cookies

    browser.export_cookies = AsyncMock(side_effect=export)
    return browser


def _patch_login_deps(
    monkeypatch,
    *,
    browser_factory,
    config: AppConfig | None = None,
    commit_source_session: Callable[..., Any] | None = None,
) -> None:
    """Patch all interactive_login dependencies in one place."""
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.get_config", lambda: config or AppConfig()
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.BrowserManager", browser_factory)
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.resolve_remember_me_prompt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.wait_for_manual_login", AsyncMock())
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.commit_source_session",
        commit_source_session
        or MagicMock(return_value=SimpleNamespace(login_generation="gen-1")),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.asyncio.sleep", AsyncMock())


def _commit_paths(commit_mock: MagicMock) -> tuple[Any, Any, Mapping[str, Any]]:
    call = commit_mock.call_args
    assert call is not None
    return call.args[0], call.args[1], call.kwargs


def _write_valid_identity(path, marker="pending") -> str:
    config = {
        "navigator.userAgent": "Mozilla/5.0 Firefox/135.0",
        "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64",
        "fonts:spacing_seed": marker,
    }
    options = {
        "env": {"CAMOU_CONFIG_1": json.dumps(config, separators=(",", ":"))},
        "firefox_user_prefs": {"webgl.enable-webgl2": True},
    }
    artifact = identity_module._new_artifact(options)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact))
    return artifact["identity_sha256"]


@pytest.mark.asyncio
async def test_interactive_login_writes_source_state_when_cookie_export_succeeds(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=True)
    commit_source_session = MagicMock(
        return_value=SimpleNamespace(login_generation="gen-123")
    )

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        commit_source_session=commit_source_session,
    )

    assert await interactive_login(tmp_path / "profile") is True

    export_call = browser.export_cookies.await_args
    assert export_call is not None
    staged_cookie = export_call.args[0]
    assert staged_cookie.name == "cookies.json"
    assert staged_cookie.parent.name.startswith(".login-pending-")
    # No UA override configured -> record None (runtime default is stable).
    committed_cookie, committed_profile, kwargs = _commit_paths(commit_source_session)
    assert committed_cookie == staged_cookie
    assert committed_profile == (tmp_path / "profile").resolve()
    assert kwargs == {"user_agent": None}
    assert not staged_cookie.parent.exists()
    captured = capsys.readouterr()
    assert "cookies exported for docker portability" in captured.out.lower()
    assert "source session generation: gen-123" in captured.out.lower()


@pytest.mark.asyncio
async def test_interactive_login_rolls_back_pair_when_source_state_write_fails(
    monkeypatch, tmp_path
):
    profile_dir = tmp_path / "profile"
    cookie_path = portable_cookie_path(profile_dir)
    state_path = cookie_path.with_name("source-state.json")
    old_cookie = b'[{"canonical-cookie":"exact bytes"}]\n'
    old_state = b'{"canonical-state":"exact bytes"}\n'
    cookie_path.write_bytes(old_cookie)
    state_path.write_bytes(old_state)
    browser = _make_browser(export_cookies=True)

    def fail_after_overwriting_state(*args, **kwargs):
        del args, kwargs
        state_path.write_bytes(b'{"partial-new-state":true}')
        raise OSError("fault-injected source-state write")

    monkeypatch.setattr(
        session_state_module,
        "write_source_state",
        fail_after_overwriting_state,
    )
    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        commit_source_session=setup_impl.commit_source_session,
    )

    with pytest.raises(OSError, match="fault-injected"):
        await interactive_login(profile_dir)

    assert cookie_path.read_bytes() == old_cookie
    assert state_path.read_bytes() == old_state
    assert not list(tmp_path.glob(".login-pending-*"))


@pytest.mark.asyncio
async def test_interactive_login_records_override_user_agent(monkeypatch, tmp_path):
    """A configured UA override is the fingerprint the manual-login cookie was
    minted under, so it must be recorded in source-state (else a later replay
    without the override falls back to a different UA)."""
    browser = _make_browser(export_cookies=True)
    commit_source_session = MagicMock(
        return_value=SimpleNamespace(login_generation="gen-1")
    )
    config = AppConfig()
    config.browser.user_agent = "CustomAgent/1.0"

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        config=config,
        commit_source_session=commit_source_session,
    )

    assert await interactive_login(tmp_path / "profile") is True
    _cookie, committed_profile, kwargs = _commit_paths(commit_source_session)
    assert committed_profile == (tmp_path / "profile").resolve()
    assert kwargs == {"user_agent": "CustomAgent/1.0"}


@pytest.mark.asyncio
async def test_interactive_login_does_not_record_ignored_camoufox_user_agent(
    monkeypatch, tmp_path
):
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def browser_factory(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    commit_source_session = MagicMock(
        return_value=SimpleNamespace(login_generation="gen-1")
    )
    config = AppConfig()
    config.browser.browser_engine = "camoufox"
    config.browser.user_agent = "CustomChromeAgent/1.0"

    _patch_login_deps(
        monkeypatch,
        browser_factory=browser_factory,
        config=config,
        commit_source_session=commit_source_session,
    )

    assert await interactive_login(tmp_path / "profile") is True
    staged_cookie, committed_profile, kwargs = _commit_paths(commit_source_session)
    staged_identity = kwargs["staged_camoufox_identity_path"]
    assert committed_profile == (tmp_path / "profile").resolve()
    assert kwargs["user_agent"] is None
    assert staged_identity.parent == staged_cookie.parent
    assert staged_identity.name == "camoufox-identity.json"
    assert captured_kwargs["camoufox_identity_path"] == staged_identity
    assert captured_kwargs["user_data_dir"] == staged_cookie.parent / "profile"
    assert captured_kwargs["user_data_dir"] != tmp_path / "profile"


@pytest.mark.asyncio
async def test_camoufox_login_recovers_from_corrupt_canonical_identity(
    monkeypatch, tmp_path
):
    profile_dir = tmp_path / "profile"
    canonical_identity = camoufox_identity_path(profile_dir)
    corrupt_bytes = b'{"identity_sha256":"corrupt and incomplete"}'
    canonical_identity.write_bytes(corrupt_bytes)
    write_source_state(profile_dir, camoufox_identity_sha256="a" * 64)
    canonical_marker = profile_dir / "canonical-marker"
    canonical_marker.parent.mkdir(parents=True)
    canonical_marker.write_text("untouched")

    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict[str, Any] = {}
    generated_digest: str | None = None

    def browser_factory(**kwargs):
        nonlocal generated_digest
        captured_kwargs.update(kwargs)
        # The real Camoufox adapter creates a pending identity at launch. This
        # fake mirrors only that local side effect; no LinkedIn/browser runs.
        assert not kwargs["camoufox_identity_path"].exists()
        kwargs["user_data_dir"].mkdir(parents=True)
        (kwargs["user_data_dir"] / "browser-marker").write_text("isolated")
        generated_digest = _write_valid_identity(
            kwargs["camoufox_identity_path"], "recovered"
        )
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.browser_engine = "camoufox"
    _patch_login_deps(
        monkeypatch,
        browser_factory=browser_factory,
        config=config,
        commit_source_session=setup_impl.commit_source_session,
    )

    assert await interactive_login(profile_dir) is True

    assert generated_digest is not None
    assert canonical_identity.read_bytes() != corrupt_bytes
    assert load_camoufox_identity_sha256(canonical_identity) == generated_digest
    state = load_source_state(profile_dir)
    assert state is not None
    assert state.camoufox_identity_sha256 == generated_digest
    assert canonical_marker.read_text() == "untouched"
    assert not captured_kwargs["user_data_dir"].parent.exists()


@pytest.mark.asyncio
async def test_camoufox_login_reuses_only_valid_bound_identity(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profile"
    canonical_identity = camoufox_identity_path(profile_dir)
    digest = _write_valid_identity(canonical_identity, "bound")
    canonical_bytes = canonical_identity.read_bytes()
    write_source_state(profile_dir, camoufox_identity_sha256=digest)
    browser = _make_browser(export_cookies=True)
    captured_pending_bytes: bytes | None = None

    def browser_factory(**kwargs):
        nonlocal captured_pending_bytes
        pending_identity = kwargs["camoufox_identity_path"]
        captured_pending_bytes = pending_identity.read_bytes()
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.browser_engine = "camoufox"
    _patch_login_deps(
        monkeypatch,
        browser_factory=browser_factory,
        config=config,
        commit_source_session=setup_impl.commit_source_session,
    )

    assert await interactive_login(profile_dir) is True

    assert captured_pending_bytes == canonical_bytes
    assert canonical_identity.read_bytes() == canonical_bytes
    state = load_source_state(profile_dir)
    assert state is not None
    assert state.camoufox_identity_sha256 == digest


@pytest.mark.asyncio
async def test_interactive_login_returns_false_when_cookie_export_fails(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=False)
    commit_source_session = MagicMock()

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        commit_source_session=commit_source_session,
    )

    assert await interactive_login(tmp_path / "profile") is False

    export_call = browser.export_cookies.await_args
    assert export_call is not None
    assert export_call.args[0].name == "cookies.json"
    assert not list(tmp_path.glob(".login-pending-*"))
    commit_source_session.assert_not_called()
    captured = capsys.readouterr()
    assert "warning: cookie export failed" in captured.out.lower()
    assert "profile saved to" not in captured.out.lower()


@pytest.mark.asyncio
async def test_interactive_login_publishes_nothing_when_teardown_is_uncertain(
    monkeypatch, tmp_path
):
    profile_dir = tmp_path / "profile"
    cookie_path = portable_cookie_path(profile_dir)
    cookie_path.write_text("original")
    browser = _make_browser(export_cookies=True)
    commit_source_session = MagicMock()
    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _UncertainTeardownContextManager(browser),
        commit_source_session=commit_source_session,
    )

    with pytest.raises(NetworkError, match="teardown uncertain"):
        await interactive_login(profile_dir)

    assert cookie_path.read_text() == "original"
    commit_source_session.assert_not_called()
    # Teardown ownership is uncertain, so the unique isolated profile is
    # intentionally abandoned rather than deleted or reopened.
    pending = list(tmp_path.glob(".login-pending-*"))
    assert len(pending) == 1
    assert await clear_auth_state(profile_dir) is False
    assert pending[0].exists()


@pytest.mark.asyncio
async def test_interactive_login_removes_pending_after_confirmed_error(
    monkeypatch, tmp_path
):
    browser = _make_browser(export_cookies=True)
    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.wait_for_manual_login",
        AsyncMock(side_effect=NetworkError("temporary DNS failure")),
    )

    with pytest.raises(NetworkError, match="temporary DNS"):
        await interactive_login(tmp_path / "profile")

    assert not list(tmp_path.glob(".login-pending-*"))


@pytest.mark.asyncio
async def test_interactive_login_cleans_pending_when_identity_staging_fails(
    monkeypatch, tmp_path
):
    config = AppConfig()
    config.browser.browser_engine = "camoufox"
    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **_kwargs: pytest.fail("browser must not launch"),
        config=config,
    )

    def fail_staging(path, _profile):
        path.write_text("partial private identity")
        raise OSError("identity copy failed")

    monkeypatch.setattr(setup_impl, "stage_bound_camoufox_identity", fail_staging)

    with pytest.raises(OSError, match="identity copy failed"):
        await interactive_login(tmp_path / "profile")

    assert not list(tmp_path.glob(".login-pending-*"))


@pytest.mark.asyncio
async def test_interactive_login_rejects_missing_li_at_after_propagation_wait(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=True)
    browser.context.cookies = AsyncMock(return_value=[])
    commit_source_session = MagicMock()

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        commit_source_session=commit_source_session,
    )

    assert await interactive_login(tmp_path / "profile") is False
    assert browser.context.cookies.await_count == 2
    browser.export_cookies.assert_not_awaited()
    commit_source_session.assert_not_called()
    assert not list(tmp_path.glob(".login-pending-*"))
    assert "no usable li_at" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_interactive_login_passes_chrome_path_to_browser_manager(
    monkeypatch, tmp_path
):
    """When config.browser.chrome_path is set, executable_path must reach BrowserManager."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.chrome_path = "/custom/chrome"

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("executable_path") == "/custom/chrome"


@pytest.mark.asyncio
async def test_interactive_login_forwards_all_browser_params(monkeypatch, tmp_path):
    """All browser config params must reach BrowserManager during --login."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.chrome_path = "/custom/chrome"
    config.browser.slow_mo = 250
    config.browser.user_agent = "CustomAgent/1.0"
    config.browser.viewport_width = 1920
    config.browser.viewport_height = 1080

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    profile = tmp_path / "profile"
    marker = profile / "canonical-marker"
    marker.parent.mkdir(parents=True)
    marker.write_text("never opened")
    await interactive_login(profile)

    isolated_profile = captured_kwargs["user_data_dir"]
    assert isolated_profile != profile
    assert isolated_profile.name == "profile"
    assert isolated_profile.parent.name.startswith(".login-pending-")
    assert marker.read_text() == "never opened"
    assert not isolated_profile.parent.exists()
    assert captured_kwargs["headless"] is False
    assert captured_kwargs["slow_mo"] == 250
    assert captured_kwargs["user_agent"] == "CustomAgent/1.0"
    assert captured_kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert captured_kwargs["executable_path"] == "/custom/chrome"


@pytest.mark.asyncio
async def test_interactive_login_passes_slow_mo_to_browser_manager(
    monkeypatch, tmp_path
):
    """When config.browser.slow_mo is set, it must reach BrowserManager."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.slow_mo = 250

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("slow_mo") == 250


@pytest.mark.asyncio
async def test_interactive_login_passes_user_agent_to_browser_manager(
    monkeypatch, tmp_path
):
    """When config.browser.user_agent is set, it must reach BrowserManager."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.user_agent = "CustomAgent/1.0"

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("user_agent") == "CustomAgent/1.0"


@pytest.mark.asyncio
async def test_interactive_login_threads_login_timeout(monkeypatch, tmp_path):
    """config.browser.login_timeout_seconds is converted to ms and passed through."""
    browser = _make_browser(export_cookies=True)
    config = AppConfig()
    config.browser.login_timeout_seconds = 600

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        config=config,
    )
    wait_mock = AsyncMock()
    monkeypatch.setattr("linkedin_mcp_server.setup.wait_for_manual_login", wait_mock)

    await interactive_login(tmp_path / "profile")

    wait_mock.assert_awaited_once()
    await_args = wait_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["timeout"] == 600000


@pytest.mark.asyncio
async def test_interactive_login_threads_login_timeout_zero_unlimited(
    monkeypatch, tmp_path
):
    """login_timeout_seconds == 0 passes timeout=0 (unlimited) through."""
    browser = _make_browser(export_cookies=True)
    config = AppConfig()
    config.browser.login_timeout_seconds = 0

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        config=config,
    )
    wait_mock = AsyncMock()
    monkeypatch.setattr("linkedin_mcp_server.setup.wait_for_manual_login", wait_mock)

    await interactive_login(tmp_path / "profile")

    wait_mock.assert_awaited_once()
    await_args = wait_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["timeout"] == 0


@pytest.mark.asyncio
async def test_interactive_login_passes_viewport_to_browser_manager(
    monkeypatch, tmp_path
):
    """Non-default viewport_width/viewport_height must reach BrowserManager as viewport."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.viewport_width = 1920
    config.browser.viewport_height = 1080

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("viewport") == {"width": 1920, "height": 1080}
