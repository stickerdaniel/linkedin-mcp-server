"""Browser-DOM tests for the job search sidebar scroll.

The unit suite mocks ``page.evaluate``, so the scrolling JS in
``scroll_job_sidebar`` never executes there. These tests run it against a
synthetic sidebar in headless chromium, where each scroll appends the next
batch of cards after a delay. That delay is the point: it stands in for the
user's connection, and the loop has to survive a batch that arrives later
than the previous one did. Skipped automatically when chromium is not
installed; run locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import logging

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.utils import scroll_job_sidebar

pytestmark = pytest.mark.browser_dom


def sidebar(
    total: int,
    batch: int,
    delays: list[int],
    initial: int = 5,
    stray: bool = False,
) -> str:
    """A scrollable sidebar that appends `batch` cards after each scroll.

    ``delays`` gives the delay of each successive batch in ms, so a fixture
    can vary latency the way a real connection does. The last value repeats.
    ``stray`` adds a job link outside the sidebar, standing in for the detail
    pane's own permalink, which must not count toward the target.
    """
    outside = (
        '<a href="/jobs/view/999999/" style="display:block">Detail pane</a>'
        if stray
        else ""
    )
    return f"""
    <body style="margin:0">
      {outside}
      <div id="rail" style="height:120px; overflow-y:scroll">
        <div id="list"></div>
        <div style="height:600px"></div>
      </div>
      <script>
        const list = document.getElementById('list');
        const rail = document.getElementById('rail');
        const delays = {delays!r};
        let n = 0;
        let round = 0;
        function add(count) {{
          for (let i = 0; i < count && n < {total}; i++) {{
            n++;
            const a = document.createElement('a');
            a.href = '/jobs/view/' + (1000 + n) + '/';
            a.textContent = 'Job ' + n;
            a.style.display = 'block';
            a.style.height = '40px';
            list.appendChild(a);
          }}
        }}
        add({initial});
        let pending = false;
        rail.addEventListener('scroll', () => {{
          if (pending || n >= {total}) return;
          pending = true;
          const delay = delays[Math.min(round, delays.length - 1)];
          round++;
          setTimeout(() => {{ add({batch}); pending = false; }}, delay);
        }});
      </script>
    </body>
    """


async def cards(page) -> int:
    return await page.evaluate(
        "document.querySelectorAll('a[href*=\"/jobs/view/\"]').length"
    )


@pytest.fixture
async def dom_page():
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


class TestSidebarScroll:
    async def test_loads_full_page_on_a_slow_connection(self, dom_page):
        """A batch slower than the old fixed 0.5s sleep must not end the loop."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[800]))

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await cards(dom_page) == 25

    async def test_loads_full_page_on_a_fast_connection(self, dom_page):
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30]))

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await cards(dom_page) == 25

    async def test_stops_at_target_without_over_scrolling(self, dom_page):
        """More cards than a page holds must not pull the next page in.

        The upper bound is loose on purpose: the fixture appends on every
        scroll event, so a batch queued by the last scroll still lands after
        the loop returns. Without the target check the loop reaches all 60,
        which is what this discriminates against.
        """
        await dom_page.set_content(sidebar(total=60, batch=5, delays=[30]))

        await scroll_job_sidebar(dom_page, target_count=25)

        assert 25 <= await cards(dom_page) < 60

    async def test_survives_a_latency_spike(self, dom_page):
        """Two fast batches shrink the budget; the third must not end the page."""
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[30, 30, 1500, 30])
        )

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await cards(dom_page) == 25

    async def test_survives_a_slow_first_batch(self, dom_page):
        """The first round has no measurement to size its budget from."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[2800, 30]))

        await scroll_job_sidebar(dom_page, target_count=25, settle_timeout=2.0)

        assert await cards(dom_page) == 25

    async def test_ignores_job_links_outside_the_sidebar(self, dom_page):
        """The detail pane's own permalink must not count toward the target."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30], stray=True))

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await cards(dom_page) == 26  # 25 in the rail, 1 outside

    async def test_returns_at_the_deadline(self, dom_page):
        """A page that keeps loading slowly must not spend the tool timeout."""
        await dom_page.set_content(sidebar(total=200, batch=1, delays=[900]))

        await scroll_job_sidebar(
            dom_page, target_count=25, settle_timeout=2.0, deadline=3.0
        )

        assert await cards(dom_page) < 25

    async def test_returns_when_the_list_is_exhausted(self, dom_page):
        """The last search page holds fewer cards than the page size."""
        await dom_page.set_content(sidebar(total=16, batch=5, delays=[30]))

        await scroll_job_sidebar(dom_page, target_count=25, settle_timeout=0.6)

        assert await cards(dom_page) == 16

    async def test_no_scrollable_container(self, dom_page, caplog):
        """Card counts alone cannot fail here, so assert which branch ran."""
        await dom_page.set_content('<body><a href="/jobs/view/111/">One</a></body>')

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, target_count=25, settle_timeout=0.3)

        assert "No scrollable container found" in caplog.text

    async def test_no_cards_at_all(self, dom_page, caplog):
        await dom_page.set_content("<body><main>No matching jobs found</main></body>")

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, target_count=25)

        assert "skipping sidebar scroll" in caplog.text
