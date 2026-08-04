import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.session_state import portable_cookie_path
from linkedin_mcp_server.setup import interactive_login

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _BrowserContextManager:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _make_browser(*, export_cookies: bool) -> MagicMock:
    browser = MagicMock()
    browser.page = MagicMock()
    browser.page.goto = AsyncMock()
    browser.context = MagicMock()
    browser.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "domain": ".linkedin.com"}]
    )
    browser.export_cookies = AsyncMock(return_value=export_cookies)
    return browser


def _patch_login_deps(
    monkeypatch,
    *,
    browser_factory,
    config: AppConfig | None = None,
    write_source_state: MagicMock | None = None,
    rotate_source_profile: MagicMock | None = None,
    close_browser: AsyncMock | None = None,
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
        "linkedin_mcp_server.setup.write_source_state",
        write_source_state
        or MagicMock(return_value=SimpleNamespace(login_generation="gen-1")),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.asyncio.sleep", AsyncMock())
    rotate = rotate_source_profile or MagicMock(return_value=None)
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.rotate_shielded",
        AsyncMock(side_effect=lambda *a: rotate(*a)),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.close_browser", close_browser or AsyncMock()
    )


@pytest.mark.asyncio
async def test_interactive_login_writes_source_state_when_cookie_export_succeeds(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=True)
    write_source_state = MagicMock(
        return_value=SimpleNamespace(login_generation="gen-123")
    )

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        write_source_state=write_source_state,
    )

    assert await interactive_login(tmp_path / "profile") is True

    browser.export_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )
    write_source_state.assert_called_once_with(tmp_path / "profile")
    captured = capsys.readouterr()
    assert "cookies exported for docker portability" in captured.out.lower()
    assert "source session generation: gen-123" in captured.out.lower()


@pytest.mark.asyncio
async def test_interactive_login_returns_false_when_cookie_export_fails(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=False)
    write_source_state = MagicMock()

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(browser),
        write_source_state=write_source_state,
    )

    assert await interactive_login(tmp_path / "profile") is False

    browser.export_cookies.assert_awaited_once_with(
        portable_cookie_path(tmp_path / "profile")
    )
    write_source_state.assert_not_called()
    captured = capsys.readouterr()
    assert "warning: cookie export failed" in captured.out.lower()
    assert "profile saved to" not in captured.out.lower()


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
    config.browser.viewport_width = 1920
    config.browser.viewport_height = 1080

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    profile = tmp_path / "profile"
    await interactive_login(profile)

    assert captured_kwargs["user_data_dir"] == profile
    assert captured_kwargs["headless"] is False
    assert captured_kwargs["slow_mo"] == 250
    assert captured_kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert captured_kwargs["executable_path"] == "/custom/chrome"


@pytest.mark.asyncio
async def test_login_keeps_webrtc_on_the_proxy(monkeypatch, tmp_path):
    """The login browser needs the WebRTC restriction as much as scraping does.

    This is the path that matters most. The login is where the session is
    created, so a leak here has been attached to that session from its first
    moment — and this path used to build its own launch options, which is
    exactly how a setting ends up applying to scraping but not to login.
    """
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs["proxy"]["server"] == "http://gate.example:7000"
    args = captured_kwargs["args"]
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args


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
async def test_login_never_overrides_the_user_agent(monkeypatch, tmp_path):
    """No user agent reaches BrowserManager, even from a config carrying one.

    ``BrowserConfig.user_agent`` still exists, because the daemon's
    configuration fingerprint hashes it and removing a field there breaks owner
    turnover. Nothing may read it: the login is where the session is minted, so
    a UA applied here would be the one every later contradiction is measured
    against.
    """
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.user_agent = "CustomAgent/1.0"

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert "user_agent" not in captured_kwargs


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


@pytest.mark.asyncio
async def test_interactive_login_retires_the_previous_profile(monkeypatch, tmp_path):
    """A manual login may be for a different account, so the previous Chromium
    profile must not be reused: it would carry the same machine_id into the new
    session and present both accounts to LinkedIn as one device."""
    profile_dir = tmp_path / "profile"
    rotate = MagicMock(return_value=None)
    order: list[str] = []
    rotate.side_effect = lambda *_args: order.append("rotate")

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: (
            order.append("launch"),
            _BrowserContextManager(_make_browser(export_cookies=True)),
        )[1],
        rotate_source_profile=rotate,
        close_browser=AsyncMock(side_effect=lambda: order.append("close")),
    )

    await interactive_login(profile_dir)

    rotate.assert_called_once_with(profile_dir)
    # close must come first: the singleton exports cookies on teardown, so a
    # later close would write the retired session's cookies over the new ones.
    assert order == ["close", "rotate", "launch"]


@pytest.mark.asyncio
async def test_interactive_login_restores_the_session_when_login_fails(
    monkeypatch, tmp_path
):
    """The old session is retired before the new one exists, so a failed login
    must not leave the user logged out of a session that was working."""
    retired = tmp_path / "invalid-state-x"
    restore = MagicMock(return_value=True)

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(
            _make_browser(export_cookies=False)
        ),
        rotate_source_profile=MagicMock(return_value=retired),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.restore_source_profile", restore)

    assert await interactive_login(tmp_path / "profile") is False

    restore.assert_called_once_with(retired, tmp_path / "profile")


@pytest.mark.asyncio
async def test_interactive_login_keeps_the_retired_session_on_success(
    monkeypatch, tmp_path
):
    restore = MagicMock()

    _patch_login_deps(
        monkeypatch,
        browser_factory=lambda **kwargs: _BrowserContextManager(
            _make_browser(export_cookies=True)
        ),
        rotate_source_profile=MagicMock(return_value=tmp_path / "invalid-state-x"),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.restore_source_profile", restore)

    assert await interactive_login(tmp_path / "profile") is True

    restore.assert_not_called()


@pytest.mark.asyncio
async def test_rotate_shielded_restores_when_cancelled(tmp_path, monkeypatch):
    """A bare await on the rotation thread is cancellable, and the cancel lands
    after the move: the session is gone and its backup path with it."""
    import threading

    from linkedin_mcp_server import session_state

    retired = tmp_path / "invalid-state-x"
    released = threading.Event()
    restore = MagicMock(return_value=True)

    def slow_rotate(*_args):
        released.wait(5)
        return retired

    monkeypatch.setattr(session_state, "rotate_source_profile", slow_rotate)
    monkeypatch.setattr(session_state, "restore_source_profile", restore)

    task = asyncio.ensure_future(session_state.rotate_shielded(tmp_path / "profile"))
    await asyncio.sleep(0.05)  # let the worker thread enter slow_rotate
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    restore.assert_called_once_with(retired, tmp_path / "profile")


@pytest.mark.asyncio
async def test_rotate_shielded_survives_overlapping_cancels(tmp_path, monkeypatch):
    """A tool timeout racing a server shutdown cancels twice. The second must
    not abandon the worker thread mid-rotation, or the session is stranded."""
    import threading

    from linkedin_mcp_server import session_state

    retired = tmp_path / "invalid-state-x"
    released = threading.Event()
    restore = MagicMock(return_value=True)

    def slow_rotate(*_args):
        released.wait(5)
        return retired

    monkeypatch.setattr(session_state, "rotate_source_profile", slow_rotate)
    monkeypatch.setattr(session_state, "restore_source_profile", restore)

    task = asyncio.ensure_future(session_state.rotate_shielded(tmp_path / "profile"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # second source of cancellation
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    restore.assert_called_once_with(retired, tmp_path / "profile")


def test_rotate_shielded_does_not_wedge_event_loop_shutdown(tmp_path):
    """asyncio.run cancels every pending task at teardown, and shielding an
    already-cancelled task re-raises forever. Running the move on a bare
    executor future keeps it out of that sweep, so the process can exit."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(f"""
        import asyncio, pathlib, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from linkedin_mcp_server import session_state

        session_state.rotate_source_profile = lambda *a: (
            time.sleep(0.2), pathlib.Path("/tmp/backup-x")
        )[1]
        session_state.restore_source_profile = lambda *a: True

        async def main():
            # Detached, still running when main() returns: exactly the shape a
            # server shutdown hits mid-login.
            asyncio.ensure_future(session_state.rotate_shielded(pathlib.Path("/tmp/p")))
            await asyncio.sleep(0.05)

        asyncio.run(main())
        print("SHUTDOWN COMPLETED")
    """)

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20
    )

    assert "SHUTDOWN COMPLETED" in result.stdout


@pytest.mark.asyncio
async def test_interactive_login_forwards_the_proxy(monkeypatch, tmp_path):
    """The login browser must use the same proxy as later scrapes.

    A session created from one address and then used from another is exactly
    what triggers LinkedIn's security checkpoint, so --login cannot be allowed
    to bypass the configured proxy.
    """
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = "user"
    config.browser.proxy_password = "pw"

    _patch_login_deps(monkeypatch, browser_factory=fake_browser_manager, config=config)

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs["proxy"] == {
        "server": "http://gate.example:7000",
        "username": "user",
        "password": "pw",
    }


@pytest.mark.asyncio
async def test_interactive_login_without_proxy_omits_the_key(monkeypatch, tmp_path):
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    def fake_browser_manager(**kwargs):
        captured_kwargs.update(kwargs)
        return _BrowserContextManager(browser)

    _patch_login_deps(
        monkeypatch, browser_factory=fake_browser_manager, config=AppConfig()
    )

    await interactive_login(tmp_path / "profile")

    assert "proxy" not in captured_kwargs


class TestTheExplicitCommandsStayUnguarded:
    """`--login` and `--import-from-browser` are insistence, not a request.

    Somebody typing them wants a new session whatever is on disk, so they must
    not stand down for a peer. Asserted at the call sites rather than on the
    helper's signature default: an earlier version checked only the default, and
    a mutation passing `superseded_by=None` at the call site left it green while
    the real command would have skipped the login it was asked for.
    """

    def test_login_passes_no_generation(self, monkeypatch):
        import linkedin_mcp_server.setup as setup

        seen: dict[str, object] = {}

        async def capture(profile_dir=None, **kwargs):
            seen.update(kwargs)
            return True

        monkeypatch.setattr(setup, "interactive_login", capture)

        assert setup.run_profile_creation("/tmp/whatever") is True

        assert "superseded_by" not in seen, seen

    def test_import_passes_no_generation(self, monkeypatch):
        import linkedin_mcp_server.browser_import.orchestrate as orchestrate

        seen: dict[str, object] = {}

        async def capture(_browser, *, user_data_dir, **kwargs):
            seen.update(kwargs)
            return True

        from linkedin_mcp_server.config import set_config
        from linkedin_mcp_server.config.schema import AppConfig

        # Installed rather than loaded: the handler calls get_config(), which
        # parses sys.argv, and under pytest that is pytest's own command line.
        config = AppConfig()
        config.server.import_from_browser = "auto"
        set_config(config)

        monkeypatch.setattr(orchestrate, "import_session_from_browser", capture)
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.configure_browser_environment", lambda: None
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.cli_main.set_headless", lambda _v: None
        )

        from linkedin_mcp_server.cli_main import import_from_browser_and_exit

        with pytest.raises(SystemExit) as caught:
            import_from_browser_and_exit()

        assert caught.value.code == 0
        assert "superseded_by" not in seen, seen
