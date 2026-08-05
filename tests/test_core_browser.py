"""Tests for BrowserManager cookie import/export helpers."""

import asyncio
import contextlib
import json
import os
import time
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager


def test_a_no_viewport_override_is_refused(tmp_path):
    """The geometry decision must not be overridable through launch options.

    `_geometry()` is spread *before* `**launch_options`, so a caller-supplied
    `no_viewport` would win. `no_viewport=False` on a headed launch puts the
    emulated screen back and restores the window-larger-than-screen
    contradiction this change exists to remove; `no_viewport=True` on a
    headless one sends both geometry keys at once. Nothing produces either
    today, which is why it is refused now rather than after something does.
    """
    with pytest.raises(TypeError, match="no_viewport"):
        BrowserManager(user_data_dir=tmp_path / "profile", no_viewport=False)


def test_a_user_agent_is_refused_rather_than_applied(tmp_path):
    """The one funnel every browser goes through must reject an override.

    ``launch_options`` is spread into the context options, so without this a
    caller reintroducing ``user_agent=`` would silently reach Patchright and
    the browser would go back to contradicting its own client hints.
    """
    with pytest.raises(TypeError, match="user_agent"):
        BrowserManager(
            user_data_dir=tmp_path / "profile",
            user_agent="Mozilla/5.0 (test) Chrome/143.0.0.0",
        )


class TestGeometry:
    """Which window geometry a launch gets, and why it depends on the mode.

    The contradiction this fixes was measured: a headed window reported an
    outer height of 805 while the same browser reported a screen 720 tall,
    because an emulated viewport was forced onto a real window.
    """

    def test_headless_keeps_an_explicit_viewport(self, tmp_path):
        """Headless plus `no_viewport` collapses the screen to 800x600, so the
        explicit size stays for the mode that has no real window."""
        manager = BrowserManager(user_data_dir=tmp_path, headless=True)

        assert manager._geometry() == {"viewport": {"width": 1280, "height": 720}}

    def test_headless_honours_a_configured_viewport(self, tmp_path):
        manager = BrowserManager(
            user_data_dir=tmp_path,
            headless=True,
            viewport={"width": 1920, "height": 1080},
        )

        assert manager._geometry() == {"viewport": {"width": 1920, "height": 1080}}

    def test_headed_sends_no_viewport_at_all(self, tmp_path):
        """A real window reports the size it really is.

        `no_viewport` has to be the only geometry key: sending a viewport
        alongside it is what emulated a screen the window did not fit on.
        """
        manager = BrowserManager(user_data_dir=tmp_path, headless=False)

        assert manager._geometry() == {"no_viewport": True}

    def test_headed_ignores_a_configured_viewport(self, tmp_path):
        """VIEWPORT stops applying to headed launches, deliberately.

        Documented behaviour change: the setting now describes the windowless
        default mode only.
        """
        manager = BrowserManager(
            user_data_dir=tmp_path,
            headless=False,
            viewport={"width": 1920, "height": 1080},
        )

        assert manager._geometry() == {"no_viewport": True}

    def test_the_mode_comes_from_the_launch_not_the_configuration(
        self, tmp_path, monkeypatch
    ):
        """The constructor argument wins over the configuration, which differs.

        This is the confusion worth pinning. The manual login constructs with
        `headless=False` while `BrowserConfig.headless` stays `True`, so a
        decision read from configuration would be wrong for exactly the launch
        that opens a window. Asserted by making the two disagree.
        """
        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.headless = True
        monkeypatch.setattr(
            "linkedin_mcp_server.config.get_config", lambda: config, raising=False
        )

        headed = BrowserManager(user_data_dir=tmp_path, headless=False)

        assert headed._geometry() == {"no_viewport": True}

    async def test_the_launch_actually_receives_the_geometry(self, tmp_path):
        """The helper is not the behaviour.

        Every other test here would stay green if `start()` stopped calling
        `_geometry()` and went back to sending a viewport unconditionally. This
        one reads the kwargs Patchright is actually handed.
        """
        captured: dict = {}

        class _FakeChromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                captured.update(kwargs)
                raise _StopLaunch()

        class _FakePlaywright:
            chromium = _FakeChromium()

            async def stop(self):
                return None

        class _StopLaunch(Exception):
            """Ends the launch once the options have been observed."""

        manager = BrowserManager(user_data_dir=tmp_path, headless=False)

        async def fake_start():
            return _FakePlaywright()

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = fake_start
            with contextlib.suppress(Exception):
                await manager.start()

        assert captured.get("no_viewport") is True
        assert "viewport" not in captured
        # The locale sits after the spread on purpose and must stay pinned.
        assert captured.get("locale") == "en-US"


class TestHowNoVisibleWindowIsAchieved:
    """`headless` keeps its public meaning but no longer names the mechanism.

    Chromium's headless *mode* is what makes a browser announce itself: it
    prepends the bare string `Headless` to the product name at runtime, so both
    the user agent and the `sec-ch-ua` brands read `HeadlessChrome`. Where the
    platform allows it the browser runs headed and hides its page in a target
    instead. Where it does not, headless is the only way to run at all, and the
    token comes back.
    """

    async def _captured_options(self, tmp_path, *, headless: bool) -> dict:
        """The options a successful launch was given.

        Deliberately not "the options recorded before a crash": the launch now
        retries once when a window is refused, so a fake that raises would be
        describing the fallback rather than the launch under test.
        """
        recorder: dict = {}
        start, _ = TestTheWindowlessLaunchEndToEnd()._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path, headless=headless)

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = start
            await manager.start()
        return recorder["options"]

    async def test_launches_headed_where_the_target_is_supported(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            lambda: True,
        )

        options = await self._captured_options(tmp_path, headless=True)

        assert options["headless"] is False

    async def test_falls_back_to_real_headless_where_it_is_not(
        self, tmp_path, monkeypatch
    ):
        """Not a preference. Measured in the container image: a headed launch
        with no display fails outright, and under Xvfb closing the last window
        kills Chromium and takes the hidden page with it."""
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            lambda: False,
        )

        options = await self._captured_options(tmp_path, headless=True)

        assert options["headless"] is True

    async def test_a_visible_window_is_always_headed(self, tmp_path):
        options = await self._captured_options(tmp_path, headless=False)

        assert options["headless"] is False

    async def test_the_attach_flag_is_not_left_behind(self, tmp_path):  # noqa: D401
        """It makes Playwright promote every `other` target, so it is scoped."""
        from linkedin_mcp_server.hidden_target import ATTACH_TO_OTHER

        before = os.environ.get(ATTACH_TO_OTHER)
        await self._captured_options(tmp_path, headless=True)

        assert os.environ.get(ATTACH_TO_OTHER) == before


class TestTheWindowlessLaunchEndToEnd:
    """The contract `start()` owes, not the helper it delegates to.

    Every other test here passes if `open_hidden_page()` is deleted from
    `start()` outright, or if its result is never assigned to `self._page`,
    because they stop the launch before page selection or exercise the helper
    alone. This one runs the launch through and reads what came out.
    """

    def _fake_playwright(
        self,
        recorder: dict,
        *,
        refuse_headed: bool = False,
        refuse_headless: bool = False,
        stop_hangs: bool = False,
    ):
        from linkedin_mcp_server.hidden_target import ATTACH_TO_OTHER

        pages: list = []

        class _Page:
            def __init__(self, url: str):
                self.url = url
                self.closed = False

            async def close(self):
                self.closed = True
                recorder["hidden_present_at_close"] = any(
                    p.url.startswith("about:blank#") for p in pages
                )

        class _Session:
            async def send(self, method, params):
                if method == "Target.createTarget":

                    async def surface():
                        await asyncio.sleep(0.01)
                        pages.append(_Page(params["url"]))

                    asyncio.get_running_loop().create_task(surface())
                return {"targetId": "T"}

        class _Browser:
            async def new_browser_cdp_session(self):
                return _Session()

        class _Context:
            def __init__(self):
                self.pages = pages
                self.browser = _Browser()

        class _Chromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                recorder["options"] = kwargs
                if refuse_headed and not kwargs.get("headless"):
                    raise RuntimeError("headed launch refused: no window server")
                if refuse_headless and kwargs.get("headless"):
                    raise RuntimeError("headless launch refused too")
                pages.append(_Page("about:blank"))
                return _Context()

        class _Playwright:
            chromium = _Chromium()

            async def stop(self):
                return None

        stops = {"count": 0}

        class _Playwright2(_Playwright):
            async def stop(self):
                stops["count"] += 1
                if stop_hangs:
                    await asyncio.sleep(30)
                return None

        async def start():
            # Every driver start, not just the first: a fallback launch starts a
            # second one, and whether *that* inherited the flag is the point.
            recorder.setdefault("flags_at_driver_start", []).append(
                os.environ.get(ATTACH_TO_OTHER)
            )
            recorder["driver_stops"] = stops
            return _Playwright2()

        return start, pages

    async def test_the_hidden_page_becomes_the_working_page(self, tmp_path):
        from linkedin_mcp_server.hidden_target import ATTACH_TO_OTHER

        recorder: dict = {}
        start, pages = self._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=True,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = start
                await manager.start()

        # The page handed to callers is the windowless one, not the startup one.
        assert manager.page.url.startswith("about:blank#")
        # The driver saw the flag; setting it afterwards would be too late.
        assert recorder["flags_at_driver_start"] == ["1"]
        # And it did not leak past the launch.
        assert ATTACH_TO_OTHER not in os.environ
        # The startup page went, and only once the hidden one existed.
        assert pages[0].closed is True
        assert recorder["hidden_present_at_close"] is True

    async def test_a_refused_window_falls_back_to_headless(self, tmp_path):
        """The machine decides, not the platform name.

        A Mac reached over SSH, a launchd daemon and a CI runner with no GUI
        session all look like macOS and all refuse to open a window. Rather
        than enumerate those, the attempt answers it -- which is the one check
        that cannot be wrong about a case nobody thought of.
        """
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder, refuse_headed=True)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=True,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = start
                await manager.start()

        # It ran, in headless, and the page is the ordinary startup one.
        assert recorder["options"]["headless"] is True
        assert manager.page.url == "about:blank"
        # The driver was replaced rather than reused. The first one read the
        # attach flag into its own process, where restoring the parent
        # environment cannot reach it, and a driver that keeps promoting
        # `other` targets would put an extension page into `context.pages` --
        # which is where the working page is taken from.
        assert recorder["flags_at_driver_start"] == ["1", None]
        assert recorder["driver_stops"]["count"] == 1

    async def test_a_driver_that_will_not_stop_does_not_block_the_fallback(
        self, tmp_path
    ):
        """A wedged driver must not turn a recoverable launch into a hang.

        `close()` bounds its own cleanup for exactly this reason. Leaving one
        driver behind is the lesser cost against never returning.
        """
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder, refuse_headed=True, stop_hangs=True)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        began = time.monotonic()
        with mock.patch(
            "linkedin_mcp_server.core.browser._CLEANUP_TIMEOUT_SECONDS", 0.05
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.hidden_target_is_supported",
                return_value=True,
            ):
                with mock.patch(
                    "linkedin_mcp_server.core.browser.async_playwright"
                ) as playwright:
                    playwright.return_value.start = start
                    await manager.start()
        elapsed = time.monotonic() - began

        assert recorder["options"]["headless"] is True
        assert recorder["flags_at_driver_start"] == ["1", None]
        # The timing is the assertion, not decoration. Without the bound this
        # still *succeeds* -- it just waits out the wedged driver first, which
        # is precisely the behaviour being ruled out. The fake hangs for 30s;
        # anything near that means the bound was skipped.
        assert elapsed < 5, f"the wedged driver was waited out ({elapsed:.1f}s)"

    async def test_a_launch_that_fails_either_way_reports_the_first_error(
        self, tmp_path
    ):
        """The retry is a chance, not a cover-up.

        If the browser will not start with or without a window then the problem
        was never the window, and the first error is the one that says what it
        actually was. Reporting the second would send someone looking at
        displays over a corrupt profile.
        """
        recorder: dict = {}
        start, _ = self._fake_playwright(
            recorder, refuse_headed=True, refuse_headless=True
        )
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=True,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = start
                with pytest.raises(Exception, match="headed launch refused"):
                    await manager.start()

    async def test_a_visible_launch_keeps_the_startup_page(self, tmp_path):
        recorder: dict = {}
        start, pages = self._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=False)

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = start
            await manager.start()

        assert manager.page is pages[0]
        assert pages[0].closed is False

    async def test_a_visible_launch_does_not_get_the_attach_flag(self, tmp_path):
        """The driver keeps the flag for its whole life, so scope matters.

        Restoring the parent's environment afterwards does nothing to the child
        process. A login that never creates a hidden target would otherwise
        spend its entire run promoting extension and other `other` targets into
        `context.pages`.
        """
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=False)

        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = start
            await manager.start()

        assert recorder["flags_at_driver_start"] == [None]

    async def test_a_platform_fallback_does_not_get_the_attach_flag(self, tmp_path):
        """Same reasoning where the platform cannot support a hidden target."""
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=False,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = start
                await manager.start()

        assert recorder["flags_at_driver_start"] == [None]


def _make_cookie(
    name: str,
    value: str = "value",
    *,
    domain: str = ".linkedin.com",
) -> dict[str, str]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
    }


def _make_browser_manager(tmp_path) -> tuple[BrowserManager, MagicMock]:
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    context = MagicMock()
    context.clear_cookies = AsyncMock()
    context.add_cookies = AsyncMock()
    context.storage_state = AsyncMock()
    browser._context = context
    return browser, context


@pytest.mark.asyncio
async def test_import_cookies_imports_bridge_subset_only(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookies = [
        _make_cookie("li_at"),
        _make_cookie("JSESSIONID"),
        _make_cookie("bcookie"),
        _make_cookie("bscookie"),
        _make_cookie("lidc"),
        _make_cookie("session", domain=".example.com"),
        _make_cookie("timezone"),
    ]
    cookie_path.write_text(json.dumps(cookies))

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_awaited_once_with(
        [cookies[0], cookies[1], cookies[2], cookies[3], cookies[4]]
    )


@pytest.mark.asyncio
async def test_import_cookies_uses_bridge_core_debug_preset(tmp_path, monkeypatch):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookies = [
        _make_cookie("li_at"),
        _make_cookie("JSESSIONID"),
        _make_cookie("bcookie"),
        _make_cookie("bscookie"),
        _make_cookie("lidc"),
        _make_cookie("liap"),
        _make_cookie("timezone"),
    ]
    cookie_path.write_text(json.dumps(cookies))
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.add_cookies.assert_awaited_once_with(cookies)


@pytest.mark.asyncio
async def test_import_cookies_requires_li_at(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("JSESSIONID"),
                _make_cookie("bcookie"),
            ]
        )
    )

    imported = await browser.import_cookies(cookie_path)

    assert imported is False
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_cookies_preserves_existing_cookies(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(
        json.dumps(
            [
                _make_cookie("li_at"),
                _make_cookie("li_rm"),
                _make_cookie("JSESSIONID"),
            ]
        )
    )

    imported = await browser.import_cookies(cookie_path)

    assert imported is True
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_storage_state_calls_context_storage_state(tmp_path):
    browser, context = _make_browser_manager(tmp_path)
    storage_state_path = tmp_path / "storage-state.json"

    exported = await browser.export_storage_state(storage_state_path, indexed_db=True)

    assert exported is True
    context.storage_state.assert_awaited_once_with(
        path=storage_state_path,
        indexed_db=True,
    )


@pytest.mark.asyncio
async def test_export_storage_state_requires_context(tmp_path):
    browser = BrowserManager(user_data_dir=tmp_path / "profile")

    exported = await browser.export_storage_state(tmp_path / "storage-state.json")

    assert exported is False


@pytest.mark.asyncio
async def test_close_is_idempotent_and_resets_state(tmp_path):
    browser = BrowserManager(user_data_dir=tmp_path / "profile")
    browser._page = MagicMock()
    context = MagicMock()
    context.close = AsyncMock(side_effect=RuntimeError("boom"))
    playwright = MagicMock()
    playwright.stop = AsyncMock()
    browser._context = context
    browser._playwright = playwright

    await browser.close()
    await browser.close()

    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()
    assert browser._context is None
    assert browser._page is None
    assert browser._playwright is None
