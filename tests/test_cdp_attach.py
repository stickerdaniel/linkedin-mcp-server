"""The attach mode: a browser that already runs and is already signed in.

``--cdp-url`` exists for the case the launched browsers cannot serve: the
session lives in a browser a person signed in to, on this machine or another
process's, and no window this server opens could be that browser. Everything
below is a consequence of one fact -- the profile is somebody else's -- so the
tests are grouped by which decision that fact changes.
"""

import asyncio
import contextlib
from unittest import mock

import pytest

from linkedin_mcp_server.config.schema import (
    AppConfig,
    BrowserConfig,
    ConfigurationError,
)
from linkedin_mcp_server.config.loaders import load_from_args, load_from_env
from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.daemon.descriptor import SHARED_CONFIG_FIELDS


@pytest.fixture(autouse=True)
def _no_marker(monkeypatch):
    """Watch marker minting: an attach must not register a guardian or a drain."""
    minted: list[str] = []
    monkeypatch.setattr(
        "linkedin_mcp_server.core.browser.new_browser_process_marker",
        lambda: (minted.append("minted"), ("marker", {}))[1],
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.core.browser.forget_browser_process_marker",
        lambda marker: minted.append(f"forget:{marker!r}"),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.core.browser.drain_browser_process_marker",
        lambda *a, **k: minted.append("drain") or True,
    )
    return minted


class TestWhatTheSettingRefuses:
    """Settings that describe a browser this process would have launched."""

    def test_a_proxy_is_refused_rather_than_ignored(self):
        """Silently dropping it would send traffic out from a visible address."""
        config = BrowserConfig(
            cdp_url="http://127.0.0.1:9222", proxy_server="socks5://p:1080"
        )
        with pytest.raises(ConfigurationError, match="proxy_server"):
            config.validate()

    def test_an_executable_is_refused(self):
        config = BrowserConfig(
            cdp_url="http://127.0.0.1:9222", chrome_path="/usr/bin/chrome"
        )
        with pytest.raises(ConfigurationError, match="chrome_path"):
            config.validate()

    @pytest.mark.parametrize(
        "endpoint",
        ["127.0.0.1:9222", "ftp://127.0.0.1:9222", "http://127.0.0.1"],
    )
    def test_an_endpoint_that_is_not_a_devtools_address_is_refused(self, endpoint):
        config = BrowserConfig(cdp_url=endpoint)
        with pytest.raises(ConfigurationError):
            config.validate()

    def test_a_trailing_slash_is_not_a_different_browser(self):
        """The value reaches the daemon fingerprint, where spelling counts."""
        config = BrowserConfig(cdp_url="http://127.0.0.1:9222/")
        config.validate()
        assert config.cdp_url == "http://127.0.0.1:9222"

    def test_a_blank_setting_is_no_setting(self):
        config = BrowserConfig(cdp_url="   ")
        config.validate()
        assert config.cdp_url is None

    @pytest.mark.parametrize(
        ("attribute", "already_set"),
        [
            ("login", {}),
            ("logout", {}),
            ("login_viewer", {"login": True}),
        ],
    )
    def test_the_modes_that_need_a_profile_of_our_own_are_refused(
        self, attribute, already_set
    ):
        """Each would exit 0 having changed only a directory nobody reads."""
        config = AppConfig()
        config.browser.cdp_url = "http://127.0.0.1:9222"
        for flag, value in already_set.items():
            setattr(config.server, flag, value)
        setattr(config.server, attribute, True)
        with pytest.raises(ConfigurationError, match="--cdp-url"):
            config.validate()

    def test_import_from_browser_is_refused(self):
        config = AppConfig()
        config.browser.cdp_url = "http://127.0.0.1:9222"
        config.server.import_from_browser = "chrome"
        with pytest.raises(ConfigurationError, match="--import-from-browser"):
            config.validate()

    def test_without_the_setting_none_of_the_above_applies(self):
        config = AppConfig()
        config.server.logout = True
        config.validate()


class TestTheSettingReachesTheBrowser:
    def test_the_environment_names_it(self, monkeypatch):
        monkeypatch.setenv("CDP_URL", "http://127.0.0.1:9222")
        assert load_from_env(AppConfig()).browser.cdp_url == "http://127.0.0.1:9222"

    def test_an_unsubstituted_placeholder_is_not_an_endpoint(self, monkeypatch):
        """Read through _env(), or a broken host becomes a strange address."""
        monkeypatch.setenv("CDP_URL", "${user_config.cdp_url}")
        assert load_from_env(AppConfig()).browser.cdp_url is None

    def test_the_flag_sets_it(self):
        config = load_from_args(AppConfig(), ["--cdp-url", "http://127.0.0.1:9222"])
        assert config.browser.cdp_url == "http://127.0.0.1:9222"

    def test_two_clients_disagreeing_about_it_are_not_one_browser(self):
        """Left out of the fingerprint, a client would wait for the wrong browser."""
        assert "cdp_url" in SHARED_CONFIG_FIELDS


class _Stopped(Exception):
    """Ends a launch once the launch options have been observed."""


class TestTheStartPath:
    def _manager(self, tmp_path, **kwargs):
        return BrowserManager(
            user_data_dir=tmp_path / "profile",
            cdp_url="http://127.0.0.1:9222",
            **kwargs,
        )

    async def test_it_connects_instead_of_launching(self, tmp_path, _no_marker):
        launched = []
        connected = []
        pages = []

        class _Page:
            def __init__(self):
                self.url = "https://www.linkedin.com/feed/"
                self.listeners = []

            def on(self, event, handler):
                self.listeners.append((event, handler))

        class _Context:
            def __init__(self, with_page=True):
                self.pages = [pages[0]] if with_page else []

            async def new_page(self):
                page = _Page()
                pages.append(page)
                self.pages.append(page)
                return page

        class _Browser:
            def __init__(self):
                self.contexts = [_Context()]
                self.closed = False

            async def close(self):
                self.closed = True

            async def new_context(self):
                context = _Context(with_page=False)
                self.contexts.append(context)
                return context

        pages.append(_Page())
        browser = _Browser()

        class _Chromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                launched.append(user_data_dir)
                raise _Stopped

            async def connect_over_cdp(self, endpoint):
                connected.append(endpoint)
                return browser

        class _Playwright:
            chromium = _Chromium()

            async def stop(self):
                return None

        manager = self._manager(tmp_path)

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = lambda: asyncio.sleep(
                0, result=_Playwright()
            )
            await manager.start()

        assert connected == ["http://127.0.0.1:9222"]
        assert launched == []
        assert manager.page.url.startswith("https://www.linkedin.com/feed/")
        assert manager.user_data_dir.endswith("profile")
        # Nothing of ours was registered: no marker, no guardian, no drain.
        assert _no_marker == []

        assert await manager.close() is True
        assert browser.closed is True
        # The fake browser is *disconnected*, never closed: closing it would
        # shut the tabs a person is working in.
        assert _no_marker == []

    async def test_it_does_not_touch_the_profile_directory(self, tmp_path, _no_marker):
        touched = []

        class _Chromium:
            async def connect_over_cdp(self, endpoint):
                return mock.MagicMock(contexts=[])

        class _Playwright:
            chromium = _Chromium()

            async def stop(self):
                return None

        def record(name):
            def _f(*args, **kwargs):
                touched.append(name)

            return _f

        manager = self._manager(tmp_path)

        with (
            mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright,
            mock.patch(
                "linkedin_mcp_server.core.browser.secure_mkdir", record("mkdir")
            ),
            mock.patch(
                "linkedin_mcp_server.core.browser.harden_linkedin_tree",
                record("harden"),
            ),
            mock.patch(
                "linkedin_mcp_server.core.browser.refuse_a_downgrade",
                record("downgrade"),
            ),
        ):
            playwright.return_value.start = lambda: asyncio.sleep(
                0, result=_Playwright()
            )
            with contextlib.suppress(Exception):
                await manager.start()

        assert touched == []

    async def test_a_dead_endpoint_says_where_it_looked(self, tmp_path, _no_marker):
        class _Chromium:
            async def connect_over_cdp(self, endpoint):
                raise ConnectionError("ECONNREFUSED")

        class _Playwright:
            chromium = _Chromium()

            async def stop(self):
                return None

        manager = self._manager(tmp_path)

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = lambda: asyncio.sleep(
                0, result=_Playwright()
            )
            with pytest.raises(NetworkError, match="127.0.0.1:9222"):
                await manager.start()

    def test_a_launch_still_mints_its_marker(self, tmp_path, _no_marker):
        """The other branch of the same constructor, so the guard is narrow."""
        BrowserManager(user_data_dir=tmp_path / "p")
        assert _no_marker == ["minted"]
