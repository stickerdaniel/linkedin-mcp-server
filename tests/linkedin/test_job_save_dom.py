"""Browser-DOM tests for the job Save control state and click programs.

The unit suite mocks ``page.evaluate``, so ``_JOB_SAVE_STATE_JS`` and
``_JOB_SAVE_CLICK_JS`` never execute there: a mocked return value asserts the
Python around them and nothing about the JS. These run the real programs
against synthetic markup in headless chromium.

Fixture structure mirrors a job top card: the Save control is the only
button-like element whose text matches a locale label, scoped to ``main``.
Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import asyncio

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import job_pages as job_pages_module
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import (
    JobPageReader,
    _JOB_SAVE_CLICK_JS,
    _JOB_SAVE_STATE_JS,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers. Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

LABELS = {"saved": "Saved", "unsaved": "Save"}

SAVED_CARD = """
<main><section class="top-card">
  <h1>Platform Engineer</h1>
  <div class="actions">
    <button type="button" aria-label="Follow Acme">Follow</button>
    <button type="button">Saved</button>
  </div>
</section></main>
"""

UNSAVED_CARD = """
<main><section class="top-card">
  <h1>Platform Engineer</h1>
  <div class="actions">
    <button type="button" aria-label="Follow Acme">Follow</button>
    <button type="button">Save</button>
  </div>
</section></main>
"""

# The expander and the disabled control both carry a Save label but must be
# excluded: the expander opens a menu, the disabled one cannot accept clicks.
EXPANDER_AND_DISABLED_CARD = """
<main><section class="top-card">
  <button type="button" aria-expanded="false">Save</button>
  <button type="button" disabled>Save</button>
  <button type="button" id="real">Save</button>
</section></main>
"""

AMBIGUOUS_CARD = """
<main><section class="top-card">
  <button type="button">Save</button>
</section>
<section class="job-details">
  <button type="button" role="button">Save</button>
</section></main>
"""

NO_CONTROL_CARD = """
<main><section class="top-card">
  <h1>Platform Engineer</h1>
  <a href="/jobs/">Back to search</a>
</section></main>
"""


def _page_html(body: str) -> str:
    return f"<html><body>{body}</body></html>"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it
    so an assertion failure or JS error in a test body is never swallowed
    into a skip.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


class TestJobSaveStateJs:
    @pytest.mark.parametrize(
        ("card", "expected"),
        [
            pytest.param(SAVED_CARD, "saved", id="saved"),
            pytest.param(UNSAVED_CARD, "unsaved", id="unsaved"),
            pytest.param(
                EXPANDER_AND_DISABLED_CARD, "unsaved", id="skips-expander-and-disabled"
            ),
        ],
    )
    async def test_reads_the_sole_control(self, dom_page, card, expected):
        """One matching control reads as its state.

        The expander and disabled Save are excluded by attribute, so the third
        card's three Save-labelled controls reduce to the one real control.
        """
        await dom_page.set_content(_page_html(card))
        assert (
            await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) == expected
        )

    async def test_two_matching_controls_yield_null(self, dom_page):
        """A sidebar Save button in main makes the scan ambiguous, fail closed."""
        await dom_page.set_content(_page_html(AMBIGUOUS_CARD))
        state = await asyncio.wait_for(
            dom_page.evaluate(
                _JOB_SAVE_STATE_JS,
                {
                    "labels": LABELS,
                    "waitTimeoutMs": job_pages_module._JOB_SAVE_CONTROL_WAIT_MS,
                },
            ),
            timeout=2,
        )
        assert state is None

    async def test_no_control_yields_null(self, dom_page):
        await dom_page.set_content(_page_html(NO_CONTROL_CARD))
        assert await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) is None

    async def test_follows_a_replaced_main_while_waiting(self, dom_page):
        """A posting that swaps in a new `main` mid-wait is followed, not stale.

        The root is looked up on every scan. A version that cached the root
        would keep reading the detached empty `main` and time out instead of
        seeing the Saved control the replacement carries.
        """
        await dom_page.set_content(_page_html("<main></main>"))
        await dom_page.evaluate(
            """() => setTimeout(() => {
                const replacement = document.createElement('main');
                const button = document.createElement('button');
                button.textContent = 'Saved';
                replacement.appendChild(button);
                document.querySelector('main').replaceWith(replacement);
            }, 200)"""
        )
        state = await dom_page.evaluate(
            _JOB_SAVE_STATE_JS,
            {"labels": LABELS, "waitTimeoutMs": 2000, "target": "saved"},
        )
        assert state == "saved"


class TestJobSaveClickJs:
    async def test_click_dispatches_on_the_unsaved_label(self, dom_page):
        await dom_page.set_content(_page_html(UNSAVED_CARD))
        await dom_page.evaluate(
            """({ labels }) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const target = Array.from(
                    document.querySelectorAll('main button')
                ).find(
                    element =>
                        normalize(element.innerText || element.textContent) ===
                        labels.unsaved
                );
                if (!target) throw new Error('Expected Save control was not found');
                target.addEventListener('click', () => {
                    target.dataset.clicked = 'true';
                });
            }""",
            {"labels": LABELS},
        )
        clicked = await dom_page.evaluate(_JOB_SAVE_CLICK_JS, {"labels": LABELS})
        assert clicked is True
        # The event listener proves that the expected control received click().
        state = await dom_page.evaluate(
            """() => {
                const buttons = document.querySelectorAll('main button');
                return Array.from(buttons).some((b) => b.dataset.clicked);
            }"""
        )
        assert state is True

    @pytest.mark.parametrize(
        "card",
        [
            pytest.param(SAVED_CARD, id="already-saved"),
            pytest.param(AMBIGUOUS_CARD, id="ambiguous"),
            pytest.param(
                '<main><button type="button" disabled>Save</button></main>',
                id="disabled",
            ),
        ],
    )
    async def test_click_refuses_when_not_a_single_unsaved_control(
        self, dom_page, card
    ):
        """No click unless exactly one unsaved control.

        A saved label, two matching controls, or a single disabled one all
        dispatch nothing.
        """
        await dom_page.set_content(_page_html(card))
        await dom_page.evaluate(
            """() => {
                for (const button of document.querySelectorAll('main button')) {
                    button.addEventListener('click', () => {
                        button.dataset.clicked = 'true';
                    });
                }
            }"""
        )
        assert await dom_page.evaluate(_JOB_SAVE_CLICK_JS, {"labels": LABELS}) is False
        clicked_any = await dom_page.evaluate(
            """() => Array.from(
                document.querySelectorAll('main button')
            ).some((button) => button.dataset.clicked)"""
        )
        assert clicked_any is False

    async def test_click_refuses_on_mixed_save_and_saved_controls(self, dom_page):
        """One Save and one Saved together is ambiguous, so neither is clicked."""
        await dom_page.set_content(
            _page_html(
                '<main><button type="button">Save</button>'
                '<button type="button">Saved</button></main>'
            )
        )
        await dom_page.evaluate(
            """() => {
                for (const button of document.querySelectorAll('main button')) {
                    button.addEventListener('click', () => {
                        button.dataset.clicked = 'true';
                    });
                }
            }"""
        )
        clicked = await dom_page.evaluate(_JOB_SAVE_CLICK_JS, {"labels": LABELS})
        assert clicked is False
        clicked_any = await dom_page.evaluate(
            """() => Array.from(
                document.querySelectorAll('main button')
            ).some((button) => button.dataset.clicked)"""
        )
        assert clicked_any is False


class TestJobSaveStateOnPosting:
    async def test_waits_for_save_control_rendered_after_domcontentloaded(
        self, dom_page
    ):
        url = "https://www.linkedin.com/jobs/view/12345/"
        route_hits = []

        async def serve_posting(route):
            route_hits.append(route.request.url)
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><main><section></section></main></body></html>",
            )

        await dom_page.route(url, serve_posting)
        session = PageSession(dom_page)
        reader = JobPageReader(
            session,
            PageNavigator(session),
            PageContentReader(session),
        )

        await reader.open_job_posting(url)
        assert route_hits == [url]
        await dom_page.evaluate(
            """() => setTimeout(() => {
                const button = document.createElement('button');
                button.textContent = 'Save';
                document.querySelector('main section').append(button);
            }, 300)"""
        )
        assert await dom_page.locator("main button").count() == 0

        assert await reader.save_control_state() == "unsaved"

    async def test_missing_control_stops_at_configured_timeout(
        self, dom_page, monkeypatch
    ):
        url = "https://www.linkedin.com/jobs/view/67890/"

        async def serve_posting(route):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<html><body><main><section></section></main></body></html>",
            )

        await dom_page.route(url, serve_posting)
        monkeypatch.setattr(job_pages_module, "_JOB_SAVE_CONTROL_WAIT_MS", 50)
        session = PageSession(dom_page)
        reader = JobPageReader(
            session,
            PageNavigator(session),
            PageContentReader(session),
        )
        await reader.open_job_posting(url)

        # The configured bound is 50ms; the outer deadline fails fast if a
        # regression ignored the setting and waited the full default instead.
        with pytest.raises(LinkedInOperationError, match="Could not uniquely identify"):
            await asyncio.wait_for(reader.save_control_state(), timeout=2)


async def _reader_on_posting(dom_page, url: str, body: str) -> JobPageReader:
    """Route ``url`` to ``body`` and return a reader with the posting open."""

    async def serve_posting(route):
        await route.fulfill(status=200, content_type="text/html", body=body)

    await dom_page.route(url, serve_posting)
    session = PageSession(dom_page)
    reader = JobPageReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
    )
    await reader.open_job_posting(url)
    return reader


class TestJobSaveTransitionOnPosting:
    async def test_waits_for_a_saved_transition_beyond_one_second(self, dom_page):
        """The transition past the old one-second sleep is only seen by waiting.

        The old code slept one second and read the control once, so a
        transition landing at 1.5s read as still unsaved and raised. The
        bounded wait for the saved state is the regression this proves.
        """
        url = "https://www.linkedin.com/jobs/view/12345/"
        reader = await _reader_on_posting(
            dom_page,
            url,
            _page_html('<main><button type="button">Save</button></main>'),
        )
        await dom_page.evaluate(
            """() => setTimeout(() => {
                document.querySelector('main button').textContent = 'Saved';
            }, 1500)"""
        )
        # The transition lands at 1.5s, well inside the 10s bound. The outer
        # deadline turns an unbounded-wait regression into a test failure.
        assert (
            await asyncio.wait_for(
                reader.save_control_state(wait_for_saved=True), timeout=5
            )
            == "saved"
        )

    async def test_unchanged_unsaved_state_stops_at_the_timeout(
        self, dom_page, monkeypatch
    ):
        """A control that never transitions comes back unsaved, bounded."""
        url = "https://www.linkedin.com/jobs/view/67890/"
        monkeypatch.setattr(job_pages_module, "_JOB_SAVE_CONTROL_WAIT_MS", 50)
        reader = await _reader_on_posting(
            dom_page,
            url,
            _page_html('<main><button type="button">Save</button></main>'),
        )
        # The configured bound is 50ms; the outer deadline fails fast if a
        # regression ignored the setting and waited the full default instead.
        assert (
            await asyncio.wait_for(
                reader.save_control_state(wait_for_saved=True), timeout=2
            )
            == "unsaved"
        )

    async def test_ambiguous_control_refuses_immediately(self, dom_page):
        """Two Save controls fail closed at once, not after the full wait."""
        url = "https://www.linkedin.com/jobs/view/11111/"
        reader = await _reader_on_posting(dom_page, url, _page_html(AMBIGUOUS_CARD))
        with pytest.raises(LinkedInOperationError, match="Could not uniquely identify"):
            await asyncio.wait_for(
                reader.save_control_state(wait_for_saved=True), timeout=2
            )
