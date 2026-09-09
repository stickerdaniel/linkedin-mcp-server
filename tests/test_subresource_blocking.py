"""Which subresources a launched context refuses to fetch, and on whose say-so.

The failure this guards is silent in both directions. Blocking too little
leaves every navigation pulling the full asset set, which is what walks a run
into LinkedIn's HTTP 429; blocking one type too many -- `stylesheet` is the
candidate -- changes what `innerText` reports and quietly corrupts the text
every tool returns. So the resource types are asserted by name, through the
handler the launch actually registered rather than the constant it came from.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, cast
from unittest import mock

from linkedin_mcp_server.browser_launch import (
    block_heavy_subresources,
    build_launch_options,
)
from linkedin_mcp_server.config.loaders import load_from_env
from linkedin_mcp_server.config.schema import AppConfig, BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager

#: Every type a LinkedIn navigation actually produces. The ones that must
#: survive are named here too, because a handler that aborts everything would
#: satisfy an assertion about the blocked three alone.
_RESOURCE_TYPES = (
    "image",
    "font",
    "media",
    "stylesheet",
    "document",
    "script",
    "xhr",
    "fetch",
    "websocket",
)


class _FakeRoute:
    """One intercepted request, recording which verdict it received."""

    def __init__(self, resource_type: str) -> None:
        self.request = mock.Mock(resource_type=resource_type)
        self.verdict: str | None = None

    async def abort(self) -> None:
        self.verdict = "abort"

    async def continue_(self) -> None:
        self.verdict = "continue"


async def _verdicts(handler: Callable[[Any], Coroutine[Any, Any, None]]) -> dict:
    """Run every resource type past *handler* and report what happened to it."""
    routes = {kind: _FakeRoute(kind) for kind in _RESOURCE_TYPES}
    for route in routes.values():
        await handler(route)
    return {kind: route.verdict for kind, route in routes.items()}


def _fake_playwright(recorder: dict):
    """A driver that launches instantly and records what was routed on it."""

    class _Page:
        url = "about:blank"

        async def close(self) -> None:
            return None

    class _Context:
        def __init__(self) -> None:
            self.pages = [_Page()]

        async def route(self, pattern, handler) -> None:
            recorder.setdefault("routes", []).append((pattern, handler))

        async def close(self) -> None:
            return None

    class _Chromium:
        executable_path = "/browser"

        async def launch_persistent_context(self, user_data_dir, **kwargs):
            return _Context()

    class _Playwright:
        chromium = _Chromium()

        async def stop(self) -> None:
            return None

    async def start():
        return _Playwright()

    return start


async def _start_and_record(tmp_path, **manager_kwargs) -> dict:
    recorder: dict = {}
    manager = BrowserManager(
        user_data_dir=tmp_path / "profile", headless=True, **manager_kwargs
    )
    with mock.patch(
        "linkedin_mcp_server.core.browser.hidden_target_is_supported",
        return_value=False,
    ):
        with mock.patch(
            "linkedin_mcp_server.core.browser.async_playwright"
        ) as playwright:
            playwright.return_value.start = _fake_playwright(recorder)
            await manager.start()
    return recorder


class TestWhatTheLaunchBlocks:
    async def test_the_launch_aborts_exactly_the_three_heavy_types(self, tmp_path):
        """Read off the handler `start()` registered, not the constant.

        A test against `BLOCKED_RESOURCE_TYPES` stays green if the route is
        never installed, which is the whole failure.
        """
        recorder = await _start_and_record(tmp_path)

        (pattern, handler), *rest = recorder["routes"]
        assert not rest
        assert pattern == "**/*"
        assert await _verdicts(handler) == {
            "image": "abort",
            "font": "abort",
            "media": "abort",
            "stylesheet": "continue",
            "document": "continue",
            "script": "continue",
            "xhr": "continue",
            "fetch": "continue",
            "websocket": "continue",
        }

    async def test_nothing_is_intercepted_when_it_is_turned_off(self, tmp_path):
        """Off means no route at all, not a route that continues everything.

        Interception has a cost of its own -- every request pauses through the
        driver -- so a user who asked for the full page should not be paying it.
        """
        recorder = await _start_and_record(tmp_path, block_subresources=False)

        assert "routes" not in recorder


class TestWhereTheSettingComesFrom:
    def test_it_is_on_without_being_asked_for(self):
        assert BrowserConfig().block_subresources is True

    def test_the_environment_can_turn_it_off(self, monkeypatch):
        monkeypatch.setenv("BLOCK_SUBRESOURCES", "false")

        assert load_from_env(AppConfig()).browser.block_subresources is False

    def test_the_setting_reaches_the_manager_through_the_launch_options(self, tmp_path):
        """The builder's dict is the only channel both launch paths share.

        `BrowserManager` takes the flag as a named argument, so this is what
        proves the two ends still meet: a setting that stopped riding along
        here would leave the manager on its default and the user's `false`
        would do nothing.
        """
        options, _ = build_launch_options(BrowserConfig(block_subresources=False))

        manager = BrowserManager(user_data_dir=tmp_path, **options)

        assert manager.block_subresources is False
        assert "block_subresources" not in manager.launch_options


class _RaisingRoute:
    """A request whose page went away between interception and verdict."""

    def __init__(self, resource_type: str) -> None:
        self.request = mock.Mock(resource_type=resource_type)

    async def abort(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")

    async def continue_(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")


async def _captured_handler():
    """The handler `block_heavy_subresources` registers, pulled off the context."""
    captured: dict[str, Any] = {}

    class _Context:
        async def route(self, pattern: str, handler: Any) -> None:
            captured["handler"] = handler

    await block_heavy_subresources(cast(Any, _Context()))
    return captured["handler"]


class TestAVanishingContextDoesNotStallTheRequest:
    """Both verdicts raise once the page behind the request is gone.

    Routine here: contexts close on rotation, on bridge reopen and at
    shutdown, and requests already in flight arrive in the handler after
    that. An exception escaping a route handler leaves the request
    unfulfilled, so the navigation that made it waits out its own timeout and
    reports nothing about why -- a 30s stall attributed to nothing.
    """

    async def test_an_abort_that_raises_is_swallowed(self):
        handler = await _captured_handler()

        await handler(_RaisingRoute("image"))  # must not raise

    async def test_a_continue_that_raises_is_swallowed(self):
        handler = await _captured_handler()

        await handler(_RaisingRoute("document"))  # must not raise
