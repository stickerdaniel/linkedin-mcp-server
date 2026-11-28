"""Browser-DOM tests for the wheel load that reads the skills list (#590).

The unit suite (``TestWheelLoad`` in ``tests/linkedin/test_capture.py``) fakes
every page read, so it never runs the ``<main>`` length script and never sends
a real wheel event. These run ``SectionCapture._wheel_load`` in headless
chromium against a synthetic document that appends one batch to ``<main>`` per
wheel event, the way the skills details page does. Every request is
intercepted: the LinkedIn page and the portal are answered by the route
handler, and anything else is aborted, so nothing leaves the machine.

The fixture is synthetic, so each case is a claim about the algorithm and never
about LinkedIn's markup. Skipped automatically when chromium is not installed;
run locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin import capture as capture_module
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

SKILLS_URL = "https://www.linkedin.com/in/testuser/details/skills/"
PORTAL_URL = "https://portal.invalid/interstitial"


@pytest.fixture
async def dom_page(monkeypatch):
    monkeypatch.setenv("LINKEDIN_TRACE_MODE", "off")
    # A batch lands 50 ms after its wheel event, well inside this pause, so
    # every round that should grow does. The production pause is 1.5 s.
    monkeypatch.setattr(capture_module, "_WHEEL_LOAD_PAUSE", 0.3)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def wheel_loaded_list(batches: int | None) -> str:
    """A list that appends one batch to ``<main>`` per wheel event.

    *batches* is how many it has to give, ``None`` for a list that never ends.
    The first batch is there on load, like the first skills on the real page.
    """
    limit = "Infinity" if batches is None else str(batches)
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Skills | LinkedIn</title></head>
  <body>
    <main><ul id="list"></ul></main>
    <script>
      let loaded = 0;
      const addBatch = () => {{
        loaded += 1;
        const item = document.createElement("li");
        item.textContent = "Skill batch " + loaded;
        document.getElementById("list").append(item);
      }};
      addBatch();
      window.addEventListener("wheel", () => {{
        if (loaded < {limit}) setTimeout(addBatch, 50);
      }});
    </script>
  </body>
</html>
"""


async def serve(page, linkedin_html: str) -> None:
    async def handle(route) -> None:
        url = route.request.url
        if url == SKILLS_URL:
            await route.fulfill(content_type="text/html", body=linkedin_html)
        elif url.startswith("https://portal.invalid/"):
            await route.fulfill(
                content_type="text/html",
                body="<!DOCTYPE html><main>Not LinkedIn</main>",
            )
        else:
            await route.abort()

    await page.route("**/*", handle)


def capture(page) -> SectionCapture:
    session = PageSession(page)
    return SectionCapture(session, PageNavigator(session), PageContentReader(session))


async def main_text(page) -> str:
    return await page.evaluate("() => document.querySelector('main').innerText")


class TestWheelLoadInABrowser:
    async def test_loads_every_batch_then_reports_the_list_complete(self, dom_page):
        await serve(dom_page, wheel_loaded_list(batches=6))
        await dom_page.goto(SKILLS_URL)

        error = await capture(dom_page)._wheel_load(None)

        assert error is None
        text = await main_text(dom_page)
        assert "Skill batch 6" in text
        assert text.count("Skill batch") == 6

    async def test_a_list_that_keeps_growing_is_reported_cut_off(self, dom_page):
        await serve(dom_page, wheel_loaded_list(batches=None))
        await dom_page.goto(SKILLS_URL)

        error = await capture(dom_page)._wheel_load(4)

        assert error is not None
        assert error["error_type"] == "list_incomplete"
        assert (await main_text(dom_page)).count("Skill batch") == 5

    async def test_another_sites_document_is_not_read(self, dom_page):
        await serve(dom_page, wheel_loaded_list(batches=6))
        await dom_page.goto(PORTAL_URL)

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await capture(dom_page)._wheel_load(None)
