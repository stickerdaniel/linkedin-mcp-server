import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.login_attempt import attempts_root
from linkedin_mcp_server.session_state import (
    load_source_state,
    portable_cookie_path,
    quarantine_dirs,
    write_source_state,
)
from linkedin_mcp_server.setup import interactive_login

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _BrowserContextManager:
    """A login browser that writes into the directory it was given, as Chromium does."""

    def __init__(self, browser, user_data_dir, on_enter=None):
        self.browser = browser
        self.user_data_dir = pathlib.Path(user_data_dir)
        self.on_enter = on_enter

    async def __aenter__(self):
        (self.user_data_dir / "Default").mkdir(parents=True, exist_ok=True)
        (self.user_data_dir / "Default" / "Cookies").write_text("new session")
        if self.on_enter is not None:
            self.on_enter(self.user_data_dir)
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

    async def export(path):
        if export_cookies:
            pathlib.Path(path).write_text("[]")
        return export_cookies

    browser.export_cookies = AsyncMock(side_effect=export)
    return browser


def _factory(browser, *, captured: dict | None = None, on_enter=None):
    def build(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return _BrowserContextManager(browser, kwargs["user_data_dir"], on_enter)

    return build


def _patch_login_deps(
    monkeypatch,
    *,
    browser_factory,
    config: AppConfig | None = None,
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
    monkeypatch.setattr("linkedin_mcp_server.setup.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.close_browser", close_browser or AsyncMock()
    )


def _write_session(profile_dir: pathlib.Path) -> str:
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default" / "Cookies").write_text("previous session")
    portable_cookie_path(profile_dir).write_text('["previous"]')
    return write_source_state(profile_dir).login_generation


def _attempts_left(profile_dir: pathlib.Path) -> list[str]:
    root = attempts_root(profile_dir)
    return sorted(entry.name for entry in root.iterdir()) if root.is_dir() else []


@pytest.mark.asyncio
async def test_interactive_login_writes_source_state_when_cookie_export_succeeds(
    isolate_profile_dir, monkeypatch, capsys
):
    profile = isolate_profile_dir
    browser = _make_browser(export_cookies=True)
    _patch_login_deps(monkeypatch, browser_factory=_factory(browser))

    assert await interactive_login(profile) is True

    state = load_source_state(profile)
    assert state is not None
    assert state.profile_path == str(profile)
    assert portable_cookie_path(profile).read_text() == "[]"
    assert (profile / "Default" / "Cookies").read_text() == "new session"
    captured = capsys.readouterr()
    assert "cookies exported for docker portability" in captured.out.lower()
    assert f"source session generation: {state.login_generation}" in (
        captured.out.lower()
    )
    assert _attempts_left(profile) == []


@pytest.mark.asyncio
async def test_interactive_login_returns_false_when_cookie_export_fails(
    isolate_profile_dir, monkeypatch, capsys
):
    profile = isolate_profile_dir
    browser = _make_browser(export_cookies=False)
    _patch_login_deps(monkeypatch, browser_factory=_factory(browser))

    assert await interactive_login(profile) is False

    assert load_source_state(profile) is None
    assert not portable_cookie_path(profile).exists()
    captured = capsys.readouterr()
    assert "warning: cookie export failed" in captured.out.lower()
    assert "profile saved to" not in captured.out.lower()
    assert _attempts_left(profile) == []


@pytest.mark.asyncio
async def test_interactive_login_passes_chrome_path_to_browser_manager(
    monkeypatch, tmp_path
):
    """When config.browser.chrome_path is set, executable_path must reach BrowserManager."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    config = AppConfig()
    config.browser.chrome_path = "/custom/chrome"

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(browser, captured=captured_kwargs),
        config=config,
    )

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("executable_path") == "/custom/chrome"


@pytest.mark.asyncio
async def test_interactive_login_forwards_all_browser_params(monkeypatch, tmp_path):
    """All browser config params must reach BrowserManager during --login."""
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    config = AppConfig()
    config.browser.chrome_path = "/custom/chrome"
    config.browser.slow_mo = 250
    config.browser.viewport_width = 1920
    config.browser.viewport_height = 1080

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(browser, captured=captured_kwargs),
        config=config,
    )

    profile = tmp_path / "profile"
    await interactive_login(profile)

    login_dir = pathlib.Path(captured_kwargs["user_data_dir"])
    assert login_dir.parent.parent == attempts_root(profile)
    assert login_dir.name == "profile"
    assert captured_kwargs["headless"] is False
    assert captured_kwargs["slow_mo"] == 250
    assert captured_kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert captured_kwargs["executable_path"] == "/custom/chrome"


@pytest.mark.asyncio
async def test_login_keeps_webrtc_on_the_proxy(monkeypatch, tmp_path):
    """The login browser needs the WebRTC restriction as much as page reading does.

    This is the path that matters most. The login is where the session is
    created, so a leak here has been attached to that session from its first
    moment — and this path used to build its own launch options, which is
    exactly how a setting ends up applying to page reading but not to login.
    """
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(browser, captured=captured_kwargs),
        config=config,
    )

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

    config = AppConfig()
    config.browser.slow_mo = 250

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(browser, captured=captured_kwargs),
        config=config,
    )

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("slow_mo") == 250


@pytest.mark.asyncio
async def test_interactive_login_threads_login_timeout(monkeypatch, tmp_path):
    """config.browser.login_timeout_seconds is converted to ms and passed through."""
    browser = _make_browser(export_cookies=True)
    config = AppConfig()
    config.browser.login_timeout_seconds = 600

    _patch_login_deps(monkeypatch, browser_factory=_factory(browser), config=config)
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

    _patch_login_deps(monkeypatch, browser_factory=_factory(browser), config=config)
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

    config = AppConfig()
    config.browser.viewport_width = 1920
    config.browser.viewport_height = 1080

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(browser, captured=captured_kwargs),
        config=config,
    )

    await interactive_login(tmp_path / "profile")

    assert captured_kwargs.get("viewport") == {"width": 1920, "height": 1080}


@pytest.mark.asyncio
async def test_the_source_is_untouched_while_the_login_browser_runs(
    isolate_profile_dir, monkeypatch
):
    """The login browser gets an empty directory of its own. The previous
    session stays readable at the source until the new one is published, and
    the singleton is closed before anything else, because it exports its
    cookies on teardown."""
    profile = isolate_profile_dir
    previous = _write_session(profile)
    order: list[str] = []
    seen: dict[str, object] = {}

    def at_launch(login_dir: pathlib.Path) -> None:
        order.append("launch")
        state = load_source_state(profile)
        seen["generation"] = None if state is None else state.login_generation
        seen["cookies"] = (profile / "Default" / "Cookies").read_text()
        seen["login_dir"] = login_dir

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(
            _make_browser(export_cookies=True), on_enter=at_launch
        ),
        close_browser=AsyncMock(side_effect=lambda: order.append("close")),
    )

    assert await interactive_login(profile) is True

    assert order[:2] == ["close", "launch"]
    assert seen["generation"] == previous
    assert seen["cookies"] == "previous session"
    assert seen["login_dir"] != profile
    published = load_source_state(profile)
    assert published is not None and published.login_generation != previous
    assert (profile / "Default" / "Cookies").read_text() == "new session"
    # The replaced session is retired, not deleted.
    retired = quarantine_dirs(profile)
    assert len(retired) == 1
    assert (retired[0] / "profile" / "Default" / "Cookies").read_text() == (
        "previous session"
    )
    assert _attempts_left(profile) == []


@pytest.mark.asyncio
async def test_a_failed_login_leaves_the_source_as_it_was(
    isolate_profile_dir, monkeypatch
):
    profile = isolate_profile_dir
    previous = _write_session(profile)

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(_make_browser(export_cookies=False)),
    )

    assert await interactive_login(profile) is False

    state = load_source_state(profile)
    assert state is not None and state.login_generation == previous
    assert (profile / "Default" / "Cookies").read_text() == "previous session"
    assert portable_cookie_path(profile).read_text() == '["previous"]'
    assert quarantine_dirs(profile) == []
    assert _attempts_left(profile) == []


@pytest.mark.asyncio
async def test_a_cancelled_login_leaves_the_source_as_it_was(
    isolate_profile_dir, monkeypatch
):
    profile = isolate_profile_dir
    previous = _write_session(profile)
    entered = asyncio.Event()

    async def never_signs_in(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    _patch_login_deps(
        monkeypatch,
        browser_factory=_factory(_make_browser(export_cookies=True)),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.wait_for_manual_login", never_signs_in
    )

    task = asyncio.ensure_future(interactive_login(profile))
    await entered.wait()
    assert len(_attempts_left(profile)) == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = load_source_state(profile)
    assert state is not None and state.login_generation == previous
    assert (profile / "Default" / "Cookies").read_text() == "previous session"
    assert quarantine_dirs(profile) == []
    assert _attempts_left(profile) == []


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
    """The login browser must use the same proxy as later reads.

    A session created from one address and then used from another is exactly
    what triggers LinkedIn's security checkpoint, so --login cannot be allowed
    to bypass the configured proxy.
    """
    browser = _make_browser(export_cookies=True)
    captured_kwargs: dict = {}

    fake_browser_manager = _factory(browser, captured=captured_kwargs)

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

    fake_browser_manager = _factory(browser, captured=captured_kwargs)

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

        # Installed because the handler reads process-global config and this
        # test needs the import selector below to be present there.
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
