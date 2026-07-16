"""Camoufox driver smoke test -- the process-level regression guard for the
playwright==1.60.0 profile-wipe incident (see the pin comment in
pyproject.toml).

Skipped by default: importing camoufox/playwright and launching the real
Firefox binary is deferred into the fixture, never done at collection time,
so this file never breaks plain `pytest` collection on a host without the
Camoufox runtime provisioned through the managed server flow or without this
project's NixOS LD_LIBRARY_PATH workaround (see run.sh). Run explicitly before
bumping the `playwright` pin past 1.59.0:

    LD_LIBRARY_PATH=<gcc-lib>:<gtk/firefox libs> uv run pytest -m camoufox_smoke -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.camoufox_smoke


@pytest.fixture
async def camoufox_page(tmp_path):
    """Real Camoufox (Firefox) page, or skip when unavailable.

    Only launch/setup is guarded by the skip -- the ``yield`` is outside it
    so an assertion failure or driver crash in a test body is never
    swallowed into a skip (mirrors test_action_signals_dom.py's dom_page
    fixture).
    """
    try:
        from camoufox.async_api import AsyncNewBrowser
        from playwright.async_api import async_playwright
    except ImportError as exc:
        pytest.skip(f"camoufox/playwright not importable: {exc}")

    try:
        playwright = await async_playwright().start()
    except Exception as exc:  # e.g. the NixOS libstdc++ import-chain failure
        pytest.skip(f"playwright driver unavailable: {exc}")

    try:
        context = await AsyncNewBrowser(
            playwright,
            persistent_context=True,
            user_data_dir=str(tmp_path / "camoufox-smoke-profile"),
            headless=True,
            os="linux",
        )
    except Exception as exc:  # Firefox binary not fetched, or launch failure
        await playwright.stop()
        pytest.skip(f"camoufox binary unavailable: {exc}")

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        yield page
    finally:
        await context.close()
        await playwright.stop()


async def test_survives_cross_origin_uncaught_error(camoufox_page):
    """Reproduces the failure SHAPE that crashed playwright==1.60.0's
    driver: an uncaught in-page JS error whose location info the browser
    withholds. A cross-origin <script> with no CORS headers is the
    standard way real pages trigger this ("Script error." with no
    file/line) -- LinkedIn very plausibly hit the same thing via a
    third-party script.

    This doesn't prove it's byte-for-byte the same trigger Firefox/Juggler
    hit for LinkedIn (that internal condition was never fully isolated),
    but it drives the same class of event -- an uncaught pageerror with
    missing location data -- through the real driver, which is what
    actually crashed it. If the driver crashes, the page connection dies
    and the final evaluate() below raises; on the pinned 1.59.0 it must not.
    """
    page = camoufox_page

    async def _fulfill_with_throwing_script(route):
        await route.fulfill(
            status=200,
            content_type="application/javascript",
            body="throw new Error('synthetic cross-origin error');",
        )

    await page.route(
        "https://camoufox-smoke-test.invalid/evil.js", _fulfill_with_throwing_script
    )

    errors = []
    page.on("pageerror", lambda exc: errors.append(exc))

    await page.set_content(
        '<html><body><script src="https://camoufox-smoke-test.invalid/evil.js">'
        "</script></body></html>"
    )
    # Give the driver a beat to fully process the pageerror event.
    await page.wait_for_timeout(200)

    # The real assertion: the driver/connection survived the uncaught error.
    # A crash like the incident's would make this raise (dead connection).
    assert await page.evaluate("1 + 1") == 2
