"""Tests for BrowserManager cookie import/export helpers."""

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.core.browser import BrowserManager
from linkedin_mcp_server.core.exceptions import NetworkError
from linkedin_mcp_server.exceptions import BrowserShutdownUnconfirmedError
from linkedin_mcp_server.process_tree import forget_browser_process_marker


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
                recorder.setdefault("events", []).append("stop")
                if stop_hangs:
                    await asyncio.sleep(30)
                return None

        async def start():
            # Every driver start, not just the first: a fallback launch starts a
            # second one, and whether *that* inherited the flag is the point.
            recorder.setdefault("flags_at_driver_start", []).append(
                os.environ.get(ATTACH_TO_OTHER)
            )
            recorder.setdefault("events", []).append("driver-start")
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
        from linkedin_mcp_server.process_tree import _BROWSER_PROCESS_MARKER

        assert recorder["options"]["env"][_BROWSER_PROCESS_MARKER] == (
            manager._process_marker
        )
        # The driver saw the flag; setting it afterwards would be too late.
        assert recorder["flags_at_driver_start"] == ["1"]
        # And it did not leak past the launch.
        assert ATTACH_TO_OTHER not in os.environ
        # The startup page went, and only once the hidden one existed.
        assert pages[0].closed is True
        assert recorder["hidden_present_at_close"] is True

    async def test_detached_groups_are_retained_before_page_setup(
        self, tmp_path, monkeypatch
    ):
        """A post-launch failure must not make Chromium undiscoverable first."""
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)
        remembered: list[str] = []

        async def fail_after_launch(*args, **kwargs):
            assert remembered == [manager._process_marker], (
                "page setup ran before group registration"
            )
            raise RuntimeError("page setup failed")

        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.remember_detached_process_groups",
            lambda marker: remembered.append(marker),
        )
        closed = AsyncMock(return_value=True)
        with mock.patch.object(manager, "close", closed):
            with mock.patch(
                "linkedin_mcp_server.core.browser.hidden_target_is_supported",
                return_value=True,
            ):
                with mock.patch(
                    "linkedin_mcp_server.core.browser.open_hidden_page",
                    side_effect=fail_after_launch,
                ):
                    with mock.patch(
                        "linkedin_mcp_server.core.browser.async_playwright"
                    ) as playwright:
                        playwright.return_value.start = start
                        with pytest.raises(Exception, match="page setup failed"):
                            await manager.start()

        assert remembered == [manager._process_marker, manager._process_marker]
        closed.assert_awaited_once()

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

    async def test_a_driver_that_will_not_stop_aborts_the_fallback(self, tmp_path):
        """A second browser must not open while the first driver may survive."""
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
                    with pytest.raises(Exception):
                        await manager.start()
        elapsed = time.monotonic() - began

        assert recorder["options"]["headless"] is False
        assert recorder["flags_at_driver_start"] == ["1"]
        # Both the first stop and the cleanup retry are bounded. The fake hangs
        # for 30s; anything near that means one of those bounds was skipped.
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


class TestTheHeadlessFallbackWaitsForTheFirstLaunchToGo:
    """A refused headed launch must be provably gone before the retry opens one.

    ``Playwright.stop()`` proves the Node driver and the leader it spawned have
    exited, and nothing else. Chromium is spawned detached into its own group,
    so the refused launch's tree can outlive its driver -- and the fallback
    reopens the very same profile directory. Two Chromiums on one profile is
    the corruption the whole module exists to prevent, and it was reachable
    from an ordinary headed refusal on a Mac with no window server.
    """

    _fake_playwright = TestTheWindowlessLaunchEndToEnd._fake_playwright

    @staticmethod
    def _drain(recorder: dict, monkeypatch, *, proves: bool) -> None:
        def drain(marker: str, *, containment: object = None) -> bool:
            recorder.setdefault("events", []).append("drain")
            return proves

        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.drain_browser_process_marker", drain
        )

    async def test_the_drain_runs_between_the_stop_and_the_second_driver(
        self, tmp_path, monkeypatch
    ):
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder, refuse_headed=True)
        self._drain(recorder, monkeypatch, proves=True)
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

        # Order is the whole assertion. Draining after the second driver came up
        # would be draining while two browsers share one profile.
        assert recorder["events"] == ["driver-start", "stop", "drain", "driver-start"]
        assert recorder["options"]["headless"] is True

    async def test_an_unproven_drain_opens_no_second_browser(
        self, tmp_path, monkeypatch
    ):
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder, refuse_headed=True)
        self._drain(recorder, monkeypatch, proves=False)
        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)

        with mock.patch(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            return_value=True,
        ):
            with mock.patch(
                "linkedin_mcp_server.core.browser.async_playwright"
            ) as playwright:
                playwright.return_value.start = start
                # Not the launch error: an unproven drain means something of
                # this launch may still be on the profile, and the caller has
                # to keep it rather than release it and try again.
                with pytest.raises(BrowserShutdownUnconfirmedError):
                    await manager.start()

        assert recorder["flags_at_driver_start"] == ["1"], "a second driver started"
        # One driver, one stop, then the abort drain and the teardown's retry.
        assert recorder["events"] == ["driver-start", "stop", "drain", "drain"]
        assert recorder["options"]["headless"] is False
        assert manager._close_confirmed is False

    async def test_a_retry_that_proves_it_still_does_not_resume_the_fallback(
        self, tmp_path, monkeypatch
    ):
        """The abort is final; only the profile's fate is left open.

        A drain that fails once and succeeds on the teardown's retry frees the
        profile, so the launch reports the window failure it started with
        rather than holding the directory. It does not go back and open the
        browser it declined to open.

        Freeing it is only half the job: the caller has to be able to see that
        it is free. The failure leaves through ``__aenter__``, so there is no
        ``__aexit__`` to record the verdict, and a caller reading
        ``close_confirmed`` from a ``finally`` would otherwise keep a profile
        this launch has proved it left.
        """
        recorder: dict = {}
        start, _ = self._fake_playwright(recorder, refuse_headed=True)
        answers = iter([False, True])

        def drain(marker: str, *, containment: object = None) -> bool:
            recorder.setdefault("events", []).append("drain")
            return next(answers)

        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.drain_browser_process_marker", drain
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
                with pytest.raises(NetworkError, match="headed launch refused"):
                    await manager.start()

        assert recorder["flags_at_driver_start"] == ["1"]
        assert recorder["events"] == ["driver-start", "stop", "drain", "drain"]
        assert manager.close_confirmed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
class TestTheFallbackDrainAgainstRealProcesses:
    """The same abort, measured against a process instead of a stub.

    The consumer tests above prove the ordering; this proves the thing being
    ordered actually kills something. The fake launch leaves a real detached
    process carrying this launch's environment marker, exactly as a refused
    headed Chromium does, and the fallback may not start until it is gone.
    """

    async def test_a_real_residual_process_is_gone_before_the_second_driver(
        self, tmp_path
    ):
        from linkedin_mcp_server.hidden_target import ATTACH_TO_OTHER

        residual: dict = {}
        alive_at_second_start: list[bool] = []

        def _running(pid: int) -> bool:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return bool(state) and not state.startswith("Z")

        class _Chromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                if not kwargs.get("headless"):
                    # A headed launch that leaves its detached tree behind.
                    process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(120)"],
                        env=kwargs["env"],
                        start_new_session=True,
                    )
                    residual["process"] = process
                    raise RuntimeError("headed launch refused: no window server")
                return _Context()

        class _Context:
            pages = [MagicMock(url="about:blank")]

        class _Driver:
            chromium = _Chromium()

            async def stop(self):
                return None

        async def start():
            if residual:
                alive_at_second_start.append(_running(residual["process"].pid))
            return _Driver()

        manager = BrowserManager(user_data_dir=tmp_path / "p", headless=True)
        try:
            with mock.patch(
                "linkedin_mcp_server.core.browser.hidden_target_is_supported",
                return_value=True,
            ):
                with mock.patch(
                    "linkedin_mcp_server.core.browser.async_playwright"
                ) as playwright:
                    playwright.return_value.start = start
                    await manager.start()

            assert alive_at_second_start == [False], (
                "the fallback driver started while the refused launch was alive"
            )
            assert not _running(residual["process"].pid)
            assert ATTACH_TO_OTHER not in os.environ
        finally:
            process = residual.get("process")
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            forget_browser_process_marker(manager._process_marker)


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


class TestCloseConfirmationIsSticky:
    """A close that was cut off may not be answered from its own leftovers.

    ``close()`` takes the handles before its first await, so a cancel landing in
    the middle leaves an object with nothing left to close and a Chromium that
    may still be running. Reading that emptiness as success is what released the
    profile lease and deleted the runtime directory underneath a live browser.
    """

    @staticmethod
    def _wired(tmp_path, monkeypatch, *, drains: bool):
        """A started manager, with the drain and the marker registry observable."""
        manager = BrowserManager(user_data_dir=tmp_path / "profile")
        drained: list[str] = []
        forgotten: list[str] = []

        def drain(marker: str, *, containment: object = None) -> bool:
            # The containment travels with the marker: on Windows it is the
            # whole of the attribution, and a drain that ignored it would be
            # answering about the wrong launch.
            drained.append(marker)
            assert containment is manager._containment
            return drains

        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.drain_browser_process_marker", drain
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.forget_browser_process_marker",
            forgotten.append,
        )
        return manager, drained, forgotten

    @staticmethod
    def _hanging_context() -> tuple[MagicMock, asyncio.Event]:
        entered = asyncio.Event()

        async def never_returns() -> None:
            entered.set()
            await asyncio.sleep(3600)

        context = MagicMock()
        context.close = AsyncMock(side_effect=never_returns)
        return context, entered

    async def _cancel_a_close(self, manager, context) -> None:
        context_mock, entered = context
        manager._context = context_mock
        manager._playwright = MagicMock(stop=AsyncMock())
        task = asyncio.ensure_future(manager.close())
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_a_cancelled_close_stays_unconfirmed_on_every_retry(
        self, tmp_path, monkeypatch
    ):
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=False)
        await self._cancel_a_close(manager, self._hanging_context())

        assert manager._context is None, "the cancel did not take the handles"
        assert await manager.close() is False
        assert await manager.close() is False
        assert forgotten == [], "an unproven close forgot the launch marker"
        assert drained == [manager._process_marker, manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_retry_confirms_only_once_the_drain_proves_it(
        self, tmp_path, monkeypatch
    ):
        """The drain is the evidence, not the empty handles."""
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=True)
        await self._cancel_a_close(manager, self._hanging_context())

        assert await manager.close() is True
        assert drained == [manager._process_marker]
        assert forgotten == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_proved_close_answers_from_its_own_record(
        self, tmp_path, monkeypatch
    ):
        """Once proved, a later close does not scan for a forgotten marker."""
        manager, drained, _forgotten = self._wired(tmp_path, monkeypatch, drains=True)
        manager._context = MagicMock(close=AsyncMock())
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is True
        assert await manager.close() is True
        assert drained == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_close_that_cannot_drain_is_not_confirmed(
        self, tmp_path, monkeypatch
    ):
        """Patchright's own verdict is not proof that the tree went with it."""
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=False)
        manager._context = MagicMock(close=AsyncMock())
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is False
        assert drained == [manager._process_marker]
        assert forgotten == []

    @pytest.mark.asyncio
    async def test_a_manager_that_never_launched_needs_no_drain(
        self, tmp_path, monkeypatch
    ):
        """No driver ran, so no process can be carrying this marker."""
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=False)

        assert await manager.close() is True
        assert drained == []
        assert forgotten == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_failed_cleanup_step_stays_unconfirmed_on_retry(
        self, tmp_path, monkeypatch
    ):
        """A raising ``context.close()`` is an interrupted teardown too."""
        manager, _drained, forgotten = self._wired(tmp_path, monkeypatch, drains=False)
        manager._context = MagicMock(close=AsyncMock(side_effect=RuntimeError("boom")))
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is False
        assert await manager.close() is False
        assert forgotten == []


class TestTheDrainRunsWhateverPatchrightDid:
    """The API step that failed is the one whose tree most needs ending.

    ``context.close()`` and ``playwright.stop()`` are bounded and can raise, and
    a wedged Chromium is exactly what makes them do it. Leaving the OS-level
    drain out of that case skipped the only thing that can both see the detached
    group and kill it, so the launch was left running *and* the profile marked
    busy for the rest of the process's life.
    """

    _wired = staticmethod(TestCloseConfirmationIsSticky._wired)

    @staticmethod
    def _hanging_context() -> MagicMock:
        async def never_returns() -> None:
            await asyncio.sleep(3600)

        return MagicMock(close=AsyncMock(side_effect=never_returns))

    @pytest.mark.asyncio
    async def test_a_context_that_will_not_close_is_still_drained(
        self, tmp_path, monkeypatch
    ):
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=True)
        manager._context = MagicMock(close=AsyncMock(side_effect=RuntimeError("boom")))
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is True
        assert drained == [manager._process_marker]
        assert forgotten == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_driver_that_will_not_stop_is_still_drained(
        self, tmp_path, monkeypatch
    ):
        """The Node driver carries no marker, so Chromium can be gone without it."""
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=True)
        manager._context = MagicMock(close=AsyncMock())
        manager._playwright = MagicMock(
            stop=AsyncMock(side_effect=RuntimeError("boom"))
        )

        assert await manager.close() is True
        assert drained == [manager._process_marker]
        assert forgotten == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_context_close_that_times_out_is_still_drained(
        self, tmp_path, monkeypatch
    ):
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=True)
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser._CLEANUP_TIMEOUT_SECONDS", 0.01
        )
        manager._context = self._hanging_context()
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is True
        assert drained == [manager._process_marker]
        assert forgotten == [manager._process_marker]

    @pytest.mark.asyncio
    async def test_a_failed_cleanup_and_a_failed_drain_stay_unconfirmed(
        self, tmp_path, monkeypatch
    ):
        """Only the drain may lift the verdict, and this one proved nothing."""
        manager, drained, forgotten = self._wired(tmp_path, monkeypatch, drains=False)
        manager._context = MagicMock(close=AsyncMock(side_effect=RuntimeError("boom")))
        manager._playwright = MagicMock(stop=AsyncMock())

        assert await manager.close() is False
        assert await manager.close() is False
        assert drained == [manager._process_marker, manager._process_marker]
        assert forgotten == []


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


class TestStartingAgainAfterAClose:
    """A second launch is a new launch, and an unproved one is refused.

    ``start()`` tells callers to close first, which is only an instruction if
    closing lets them open again. The hazard is the record the first launch
    leaves: ``_close_proven`` answers a later ``close()`` without touching a
    handle, so a restart that kept it would report a confirmed shutdown while
    the new context and driver are still live, with no Patchright teardown and
    no OS-level drain behind the answer.
    """

    @staticmethod
    def _wired(tmp_path, monkeypatch, *, drains: bool = True):
        """A manager whose launches, drains and marker traffic are observable."""
        from linkedin_mcp_server.process_tree import _BROWSER_PROCESS_MARKER

        manager = BrowserManager(user_data_dir=tmp_path / "profile", headless=True)
        record: dict = {
            "contexts": [],
            "drivers": [],
            "launch_markers": [],
            "remembered": [],
            "drained": [],
            "forgotten": [],
        }

        class _Context:
            def __init__(self) -> None:
                self.pages = [MagicMock(name="startup-page")]
                self.closes = 0
                self.hangs = False
                self.entered_close = asyncio.Event()

            async def close(self) -> None:
                self.closes += 1
                self.entered_close.set()
                if self.hangs:
                    await asyncio.sleep(3600)

        class _Chromium:
            async def launch_persistent_context(self, user_data_dir, **kwargs):
                record["launch_markers"].append(kwargs["env"][_BROWSER_PROCESS_MARKER])
                context = _Context()
                record["contexts"].append(context)
                return context

        class _Driver:
            def __init__(self) -> None:
                self.chromium = _Chromium()
                self.stops = 0

            async def stop(self) -> None:
                self.stops += 1

        async def start_driver():
            driver = _Driver()
            record["drivers"].append(driver)
            return driver

        def drain(marker: str, *, containment: object = None) -> bool:
            record["drained"].append((marker, containment))
            return drains

        # A real ``ps`` scan per launch, twice over, for an answer no assertion
        # here reads. The marker it is handed is the assertion instead.
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.remember_detached_process_groups",
            record["remembered"].append,
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.drain_browser_process_marker", drain
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.forget_browser_process_marker",
            record["forgotten"].append,
        )
        # Real headless, so the launch does not go looking for a hidden target
        # and the startup page is the page under test.
        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.hidden_target_is_supported",
            lambda: False,
        )
        return manager, record, start_driver

    @staticmethod
    @contextlib.contextmanager
    def _driving(start_driver):
        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = start_driver
            yield

    @pytest.mark.asyncio
    async def test_the_second_cycle_launches_and_tears_down_its_own_browser(
        self, tmp_path, monkeypatch
    ):
        """Two cycles, and everything the second one owns is its own."""
        manager, record, start_driver = self._wired(tmp_path, monkeypatch)

        with self._driving(start_driver):
            await manager.start()
            first_marker = manager._process_marker
            assert await manager.close() is True

            await manager.start()
            second_marker = manager._process_marker
            assert manager._context is record["contexts"][1]
            assert manager.page is record["contexts"][1].pages[0]
            assert await manager.close() is True

        assert len(record["drivers"]) == 2
        assert record["contexts"][0] is not record["contexts"][1]
        # The teardown ran for the second launch as well. Without the reset the
        # second close answers from the first launch's record and both of these
        # stay at zero for the browser that is still up.
        assert [context.closes for context in record["contexts"]] == [1, 1]
        assert [driver.stops for driver in record["drivers"]] == [1, 1]
        # A launch this process has already written off must not be what the
        # second Chromium announces itself as.
        assert second_marker != first_marker
        assert record["launch_markers"] == [first_marker, second_marker]
        assert record["remembered"] == [first_marker, second_marker]
        assert record["drained"] == [(first_marker, None), (second_marker, None)]
        assert record["forgotten"] == [first_marker, second_marker]

    @pytest.mark.asyncio
    async def test_a_new_launch_stops_claiming_the_previous_shutdown(
        self, tmp_path, monkeypatch
    ):
        """``close_confirmed`` may never speak for a browser that is running."""
        manager, _record, start_driver = self._wired(tmp_path, monkeypatch)

        with self._driving(start_driver):
            async with manager:
                pass
            assert manager.close_confirmed is True

            await manager.start()
            assert manager.close_confirmed is False
            assert await manager.close() is True

    @pytest.mark.asyncio
    async def test_a_new_launch_is_not_authenticated_by_the_old_one(
        self, tmp_path, monkeypatch
    ):
        """The flag lets ``validate_session`` skip its live check."""
        manager, _record, start_driver = self._wired(tmp_path, monkeypatch)

        with self._driving(start_driver):
            await manager.start()
            manager.is_authenticated = True
            assert await manager.close() is True

            await manager.start()
            assert manager.is_authenticated is False
            assert await manager.close() is True

    @pytest.mark.asyncio
    async def test_a_close_that_never_launched_still_permits_a_start(
        self, tmp_path, monkeypatch
    ):
        """The other route into a proved close: nothing was ever started."""
        manager, record, start_driver = self._wired(tmp_path, monkeypatch)
        constructed_marker = manager._process_marker

        assert await manager.close() is True
        assert record["drained"] == [], "a manager that never launched was scanned"

        with self._driving(start_driver):
            await manager.start()
            launch_marker = manager._process_marker
            assert launch_marker != constructed_marker
            assert record["launch_markers"] == [launch_marker]
            assert await manager.close() is True

        assert record["drained"] == [(launch_marker, None)]

    @pytest.mark.asyncio
    async def test_an_unproved_close_refuses_the_next_start(
        self, tmp_path, monkeypatch
    ):
        """The first Chromium may still be on this profile."""
        manager, record, start_driver = self._wired(tmp_path, monkeypatch, drains=False)

        with self._driving(start_driver):
            await manager.start()
            first_marker = manager._process_marker
            assert await manager.close() is False

            with pytest.raises(BrowserShutdownUnconfirmedError, match="not proved"):
                await manager.start()

        assert len(record["drivers"]) == 1, "a second browser opened on the profile"
        assert record["launch_markers"] == [first_marker]
        assert manager._process_marker == first_marker

    @pytest.mark.asyncio
    async def test_a_cancelled_close_refuses_the_next_start(
        self, tmp_path, monkeypatch
    ):
        """The emptiness a cancel leaves is not a shutdown.

        ``close()`` takes the handles before its first await, so this manager
        looks exactly like one that never launched: no context, no driver, and
        a Chromium that may still be running.
        """
        manager, record, start_driver = self._wired(tmp_path, monkeypatch)

        with self._driving(start_driver):
            await manager.start()
            first_marker = manager._process_marker
            context = record["contexts"][0]
            context.hangs = True
            closing = asyncio.ensure_future(manager.close())
            await context.entered_close.wait()
            closing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await closing

            assert manager._context is None, "the cancel did not take the handles"
            with pytest.raises(BrowserShutdownUnconfirmedError, match="not proved"):
                await manager.start()

        assert len(record["drivers"]) == 1, "a second browser opened on the profile"
        assert manager._process_marker == first_marker

    @pytest.mark.asyncio
    async def test_a_failed_relaunch_does_not_drain_the_previous_containment(
        self, tmp_path, monkeypatch
    ):
        """The containment describes this launch, and a spent one describes none.

        On Windows it is the whole of the attribution, so a relaunch that dies
        before it has one would otherwise take the drain through the previous
        launch's Job: a question about a browser that was proved gone, asked on
        behalf of one that never started.
        """
        from linkedin_mcp_server.process_tree import ProcessTreeError

        manager, record, start_driver = self._wired(tmp_path, monkeypatch)
        job = object()
        containments: list[object] = []

        def contain(playwright: object) -> object:
            containments.append(playwright)
            if len(containments) > 1:
                raise ProcessTreeError("no Job for this launch")
            return job

        monkeypatch.setattr(
            "linkedin_mcp_server.core.browser.contain_browser_launch", contain
        )

        with self._driving(start_driver):
            await manager.start()
            first_marker = manager._process_marker
            assert await manager.close() is True

            with pytest.raises(NetworkError, match="no Job for this launch"):
                await manager.start()

        assert manager._containment is None
        assert record["drained"] == [(first_marker, job)]
        assert record["drivers"][1].stops == 1, "the uncontained driver kept running"
