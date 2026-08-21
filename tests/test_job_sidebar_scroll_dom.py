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
from linkedin_mcp_server.scraping.extractor import _JOB_IDS_JS

pytestmark = pytest.mark.browser_dom


def sidebar(
    total: int,
    batch: int,
    delays: list[int],
    initial: int = 5,
    strays: int = 0,
    links_per_card: int = 1,
    scrollable_pane: bool = False,
    wrap_cards: bool = False,
    rerender_after: int | None = None,
    slugged: bool = False,
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
    wrap_cards_js = "true" if wrap_cards else "false"
    slug_js = "true" if slugged else "false"
    rerender_js = "null" if rerender_after is None else str(rerender_after)
    return f"""
    <body style="margin:0">
      {outside}
      <div id="rail" style="height:120px; overflow-y:scroll">
        <div id="list"></div>
        <div style="height:600px"></div>
      </div>
      <script>
        let list = document.getElementById('list');
        let rail = document.getElementById('rail');
        const delays = {delays!r};
        let n = 0;
        let round = 0;
        function add(count) {{
          for (let i = 0; i < count && n < {total}; i++) {{
            n++;
            for (let k = 0; k < {links_per_card}; k++) {{
              const a = document.createElement('a');
              a.href = {slug_js}
                ? '/jobs/view/senior-engineer-at-acme-' + (1000 + n)
                : '/jobs/view/' + (1000 + n) + '/';
              a.textContent = 'Job ' + n;
              a.style.display = 'block';
              a.style.height = '40px';
              if ({wrap_cards_js}) {{
                const wrap = document.createElement('div');
                wrap.style.cssText = 'height:20px;overflow-y:auto';
                const filler = document.createElement('div');
                filler.style.height = '60px';
                wrap.appendChild(a);
                wrap.appendChild(filler);
                list.appendChild(wrap);
              }} else {{
                list.appendChild(a);
              }}
            }}
          }}
        }}
        add({initial});
        let pending = false;
        let batches = 0;
        function mount() {{
        rail.addEventListener('scroll', () => {{
          if (pending || n >= {total}) return;
          pending = true;
          const delay = delays[Math.min(round, delays.length - 1)];
          round++;
          setTimeout(() => {{
            add({batch});
            batches++;
            if ({rerender_js} !== null && batches === {rerender_js}) {{
              const fresh = rail.cloneNode(true);
              rail.replaceWith(fresh);
              rail = fresh;
              list = fresh.querySelector('#list');
              mount();
            }}
            pending = false;
          }}, delay);
        }});
        }}
        mount();
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

        await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await cards(dom_page) == 25

    async def test_loads_full_page_on_a_fast_connection(self, dom_page):
        """Without scrolling the rail stays at its initial five cards."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30]))

        await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await rail_cards(dom_page) == 25

    async def test_loads_everything_the_rail_offers(self, dom_page):
        """No target count: the rail is done when it stops growing."""
        await dom_page.set_content(sidebar(total=60, batch=5, delays=[30]))

        await scroll_job_sidebar(dom_page, max_scrolls=30)

        assert await rail_cards(dom_page) == 60

    async def test_survives_a_latency_spike(self, dom_page):
        """Two fast batches shrink the budget; the third must not end the page."""
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[30, 30, 1500, 30])
        )

        # Default settle_timeout on purpose: 1500ms is the point of the test.
        await scroll_job_sidebar(dom_page)

        assert await cards(dom_page) == 25

    async def test_survives_a_slow_first_batch(self, dom_page):
        """The first round has no measurement to size its budget from."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[2800, 30]))

        await scroll_job_sidebar(dom_page, settle_timeout=2.0)

        assert await cards(dom_page) == 25

    async def test_ignores_job_links_outside_the_sidebar(self, dom_page):
        """The detail pane's own permalink must not count toward the target."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30], strays=5))

        await scroll_job_sidebar(dom_page)

        # Counting the whole document would stop the loop 5 cards early.
        assert await rail_cards(dom_page) == 25

    async def test_returns_at_the_deadline(self, dom_page, caplog):
        """A page that keeps loading slowly must not spend the tool timeout."""
        await dom_page.set_content(sidebar(total=200, batch=1, delays=[900]))

        started = time.monotonic()
        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, settle_timeout=2.0, deadline=3.0)
        elapsed = time.monotonic() - started

        # A card count alone cannot fail here: max_scrolls caps this fixture
        # below 25 whatever the deadline does.
        assert "(deadline)" in caplog.text
        assert elapsed < 5.0

    async def test_returns_when_the_list_is_exhausted(self, dom_page, caplog):
        """The last search page holds fewer cards than the page size.

        A loop that never concludes the list is done reaches 16 cards too,
        by running into the deadline or the cap. Which exit ran is the
        assertion; the count alone cannot tell them apart.
        """
        await dom_page.set_content(sidebar(total=16, batch=5, delays=[30]))

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await cards(dom_page) == 16
        assert "(deadline)" not in caplog.text
        assert "scroll cap reached" not in caplog.text

    async def test_no_scrollable_container(self, dom_page, caplog):
        """Card counts alone cannot fail here, so assert which branch ran."""
        await dom_page.set_content('<body><a href="/jobs/view/111/">One</a></body>')

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, settle_timeout=0.3)

        assert "No scrollable container found" in caplog.text

    async def test_no_cards_at_all(self, dom_page, caplog):
        await dom_page.set_content("<body><main>No matching jobs found</main></body>")

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page)

        assert "skipping sidebar scroll" in caplog.text

    async def test_counts_jobs_not_links(self, dom_page, caplog):
        """A card with two links to one job must not count twice.

        The anchor count cannot fail here: growth ends the loop either way,
        so both a job counter and a link counter finish the list. The number
        the algorithm actually held is only visible in the log line.
        """
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[300], links_per_card=2)
        )

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await rail_cards(dom_page) == 50  # 25 jobs, two links each
        assert "holds 25 cards" in caplog.text

    async def test_picks_the_rail_over_a_scrollable_pane(self, dom_page):
        """The detail pane scrolls too, and comes first in document order.

        ``initial=11`` is what a live search rendered before any scroll, so
        the rail outweighs a pane holding its permalink plus a handful of
        similar jobs. The heuristic is "most job ids", so a pane holding more
        jobs than the rail has rendered would still win; that is documented
        in the function and has not been observed.
        """
        await dom_page.set_content(
            sidebar(
                total=25,
                batch=5,
                delays=[30],
                initial=11,
                strays=6,
                scrollable_pane=True,
            )
        )

        await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await rail_cards(dom_page) == 25

    async def test_picks_the_rail_over_per_card_wrappers(self, dom_page):
        """A card in its own scrollable wrapper must not become the container.

        The nearest scrollable ancestor is then a container holding one card,
        and the rail is never scrolled at all.
        """
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[30], wrap_cards=True)
        )

        await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await rail_cards(dom_page) == 25

    async def test_shrinks_the_budget_after_fast_batches(self, dom_page):
        """A fast connection must not pay the full budget to conclude.

        The terminating round costs budget + settle_timeout. Without the
        shrink that is 2x settle_timeout; with it the first half collapses
        to min_budget.
        """
        await dom_page.set_content(sidebar(total=15, batch=5, delays=[40]))

        started = time.monotonic()
        await scroll_job_sidebar(dom_page, settle_timeout=3.0)
        elapsed = time.monotonic() - started

        assert await rail_cards(dom_page) == 15
        assert elapsed < 5.0

    async def test_recovers_when_the_rail_is_replaced(self, dom_page):
        """A re-render detaches the rail; polling it would measure a corpse."""
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[30], rerender_after=1)
        )

        await scroll_job_sidebar(dom_page, settle_timeout=1.0)

        assert await rail_cards(dom_page) == 25

    async def test_stops_at_the_scroll_cap(self, dom_page, caplog):
        """The cap must be distinguishable from an exhausted list in the log."""
        await dom_page.set_content(sidebar(total=200, batch=1, delays=[30]))

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, max_scrolls=3)

        assert "scroll cap reached" in caplog.text
        # A cap that fires before scrolling logs the same words at 0 scrolls.
        assert "from 3 scrolls" in caplog.text

    async def test_a_closed_page_does_not_raise(self, dom_page, caplog):
        """Scrolling is best effort and must never end the caller's search."""
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30]))
        await dom_page.close()  # stands in for a navigation mid-scroll

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, deadline=1.0)

        assert "scroll failed" in caplog.text

    async def test_reads_ids_from_slugged_hrefs(self, dom_page):
        """LinkedIn also serves /jobs/view/<slug>-<id>; the id is the target.

        The scroll and the extractor have to agree, because the offset now
        advances by the number of ids the extractor returned. Running only
        the scroll here left the consumer free to disagree, and it did.
        """
        await dom_page.set_content(
            sidebar(total=25, batch=5, delays=[30], slugged=True)
        )

        await scroll_job_sidebar(dom_page)

        assert await rail_cards(dom_page) == 25
        assert await dom_page.evaluate(_JOB_IDS_JS) == [
            str(1000 + n) for n in range(1, 26)
        ]

    async def test_a_slug_opening_with_a_year_is_not_the_id(self, dom_page):
        """`/jobs/view/2026-...-4252026496/` is job 4252026496, not job 2026."""
        await dom_page.set_content(
            '<body><a href="/jobs/view/2026-software-engineer-at-acme-4252026496/">A</a>'
            '<a href="/jobs/view/senior-engineer-at-acme-4252026497/">B</a>'
            '<a href="/jobs/view/4252026498/?refId=x">C</a></body>'
        )

        assert await dom_page.evaluate(_JOB_IDS_JS) == [
            "4252026496",
            "4252026497",
            "4252026498",
        ]

    async def test_reads_ids_from_a_localized_slug(self, dom_page, caplog):
        r"""An accented title arrives percent-encoded, and still carries an id.

        Both readers are exercised at once, because they read different
        strings: the extractor takes `a.href`, which the browser serializes
        with the non-ASCII escaped, while the scroll takes the raw attribute.
        JS `\w` is ASCII and consumes neither, so a rail of localized cards
        counts zero ids and loses the pick to the two-link detail pane.

        The three hrefs are live samples from the French guest search.
        """
        await dom_page.set_content(
            "<body>"
            "<div id='rail' style='height:60px;overflow-y:scroll'>"
            '<a href="/jobs/view/d%C3%A9veloppeur-d%C3%A9veloppeuse-full-stack-at-pix-4449125172/"'
            " style='display:block;height:40px'>A</a>"
            '<a href="/jobs/view/société-des-grands-projets-4365799661/"'
            " style='display:block;height:40px'>B</a>"
            '<a href="/jobs/view/tricefal%C2%AE%EF%B8%8F-4444869211/"'
            " style='display:block;height:40px'>C</a>"
            "<div style='height:600px'></div></div>"
            "<div id='pane' style='height:60px;overflow-y:scroll'>"
            '<a href="/jobs/view/4400000001/" style="display:block;height:40px">D</a>'
            '<a href="/jobs/view/4400000002/" style="display:block;height:40px">E</a>'
            "<div style='height:600px'></div></div>"
            "</body>"
        )

        with caplog.at_level(logging.DEBUG, logger="linkedin_mcp_server.core.utils"):
            await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        # Three beats the pane's two only while the accented ids are read.
        assert "holds 3 cards" in caplog.text
        assert await dom_page.evaluate(_JOB_IDS_JS) == [
            "4449125172",
            "4365799661",
            "4444869211",
            "4400000001",
            "4400000002",
        ]

    async def test_finds_the_rail_when_one_card_has_rendered(self, dom_page):
        """A slow first paint shows one card, and the rail is still the rail.

        ``wait_for_selector`` returns on that first link, so a pick that
        demands two jobs gives up here and never scrolls the rest in.
        """
        await dom_page.set_content(sidebar(total=25, batch=5, delays=[30], initial=1))

        await scroll_job_sidebar(dom_page, settle_timeout=0.6)

        assert await rail_cards(dom_page) == 25

    async def test_the_deadline_covers_the_wait_for_the_first_card(self, dom_page):
        """A slow first paint spends the budget, it does not extend it."""
        await dom_page.set_content(
            "<body><div id='rail' style='height:60px;overflow-y:scroll'>"
            "<div id='list'></div><div style='height:600px'></div></div>"
            "<script>setTimeout(() => {"
            "const a = document.createElement('a');"
            "a.href = '/jobs/view/1001/';"
            "a.style.cssText = 'display:block;height:40px';"
            "document.getElementById('list').appendChild(a);"
            "}, 900);</script></body>"
        )

        started = time.monotonic()
        await scroll_job_sidebar(dom_page, deadline=1.0, settle_timeout=5.0)
        elapsed = time.monotonic() - started

        # 0.9s of it went on the selector wait, leaving 0.1s to scroll in.
        assert elapsed < 1.5
