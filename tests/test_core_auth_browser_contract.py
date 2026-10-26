"""Patchright contracts used by the manual-login wait."""

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.auth import _has_auth_cookie, wait_for_manual_login

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_contract,
    pytest.mark.xdist_group("browser_runtime"),
]


@pytest.fixture
async def persistent_context(tmp_path):
    """Real persistent Chromium context, or skip when unavailable."""
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                tmp_path / "profile", channel="chromium", headless=True
            )
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield context
        finally:
            await context.close()


async def test_cookie_from_closed_tab_completes_wait(persistent_context):
    """A cookie issued in tab B remains visible through tab A after B closes."""
    context = persistent_context
    tracked = context.pages[0]
    authenticated = await context.new_page()
    await context.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><meta charset=utf-8><title>LinkedIn</title>",
        ),
    )
    await authenticated.goto("https://www.linkedin.com/contract-test")
    await authenticated.evaluate(
        "document.cookie = "
        "'li_at=contract-session; Domain=.www.linkedin.com; Path=/; Secure'"
    )
    await authenticated.close()

    assert context.pages == [tracked]
    await wait_for_manual_login(tracked, timeout=1000)


async def test_auth_cookie_must_apply_to_feed(persistent_context):
    """Chromium, rather than a domain suffix check, decides applicability."""
    context = persistent_context
    page = context.pages[0]
    base = {
        "name": "li_at",
        "value": "session",
        "secure": True,
        "httpOnly": True,
    }
    unusable = [
        {**base, "domain": ".learning.linkedin.com", "path": "/"},
        {**base, "domain": ".www.linkedin.com", "path": "/learning"},
    ]

    for cookie in unusable:
        await context.clear_cookies()
        await context.add_cookies([cookie])
        assert await _has_auth_cookie(page) is False
