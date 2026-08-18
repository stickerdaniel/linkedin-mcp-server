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
import time

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.utils import scroll_job_sidebar

pytestmark = pytest.mark.browser_dom


def sidebar(
    total: int,
    batch: int,
    delays: list[int],
    initial: int = 5,
    strays: int = 0,
    links_per_card: int = 1,
    scrollable_pane: bool = False,
) -> str:
    """A scrollable sidebar that appends `batch` cards after each scroll.

    ``delays`` gives the delay of each successive batch in ms, so a fixture
    can vary latency the way a real connection does. The last value repeats.
    ``strays`` adds job links outside the rail, standing in for the detail
    pane's own permalink. ``links_per_card`` mirrors a card carrying both a
    title link and an overlay link to the same job. ``scrollable_pane`` puts
    those stray links in a container that scrolls on its own, which is what
    makes "first scrollable ancestor" the wrong container to pick.
    """
    stray_links = "".join(
        f'<a href="/jobs/view/{900000 + i}/" style="display:block;height:40px">'
        f"Pane {i}</a>"
        for i in range(strays)
    )
    outside = (
        f'<div id="pane" style="height:80px;overflow-y:scroll">{stray_links}'
        '<div style="height:400px"></div></div>'
        if scrollable_pane
        else stray_links
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
            for (let k = 0; k < {links_per_card}; k++) {{
              const a = document.createElement('a');
              a.href = '/jobs/view/' + (1000 + n) + '/';
              a.textContent = 'Job ' + n;
              a.style.display = 'block';
              a.style.height = '40px';
              list.appendChild(a);
            }}
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


async def rail_cards(page) -> int:
    """Links inside the results rail alone, ignoring anything the pane holds."""
    return await page.evaluate(
        "document.querySelectorAll('#rail a[href*=\"/jobs/view/\"]').length"
    )


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
        """Card count alone would pass for any loop, so bound the time too.

        Waiting the full budget every round would take four rounds times
        settle_timeout here. Measured span on this fixture is 0.17-0.35s.
        """
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30]))

        started = time.monotonic()
        await scroll_job_sidebar(dom_page, target_count=25)
        elapsed = time.monotonic() - started

        assert await cards(dom_page) == 25
        assert elapsed < 2.0

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
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30], strays=5))

        await scroll_job_sidebar(dom_page, target_count=25)

        # Counting the whole document would stop the loop 5 cards early.
        assert await rail_cards(dom_page) == 25

    async def test_returns_at_the_deadline(self, dom_page, caplog):
        """A page that keeps loading slowly must not spend the tool timeout."""
        await dom_page.set_content(sidebar(total=200, batch=1, delays=[900]))

        started = time.monotonic()
        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(
                dom_page, target_count=25, settle_timeout=2.0, deadline=3.0
            )
        elapsed = time.monotonic() - started

        # A card count alone cannot fail here: max_scrolls caps this fixture
        # below 25 whatever the deadline does.
        assert "hit the 3s deadline" in caplog.text
        assert elapsed < 5.0

    async def test_returns_when_the_list_is_exhausted(self, dom_page):
        """The last search page holds fewer cards than the page size.

        A loop that never concludes the list is done would hang here rather
        than fail an assertion, which is what this covers.
        """
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

    async def test_counts_jobs_not_links(self, dom_page):
        """A card with two links to one job must not count twice."""
        await dom_page.set_content(
            # Slow enough that one batch lands per round, so a loop
            # counting links instead of jobs visibly stops at half a page.
            sidebar(total=25, batch=5, delays=[300], links_per_card=2)
        )

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await rail_cards(dom_page) == 50  # 25 jobs, two links each

    async def test_picks_the_rail_over_a_scrollable_pane(self, dom_page):
        """The detail pane scrolls too, and comes first in document order."""
        await dom_page.set_content(
            sidebar(
                total=25,
                batch=5,
                delays=[30],
                strays=6,
                scrollable_pane=True,
            )
        )

        await scroll_job_sidebar(dom_page, target_count=25)

        assert await rail_cards(dom_page) == 25
