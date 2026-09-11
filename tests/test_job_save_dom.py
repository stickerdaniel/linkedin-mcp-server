# tests/test_job_save_dom.py
"""Browser-DOM tests for the job Save control state and click JS.

The unit suite mocks ``page.evaluate``, so the JS in ``_JOB_SAVE_STATE_JS``
and ``_JOB_SAVE_CLICK_JS`` never executes there. These tests run the real JS
against synthetic HTML in headless chromium. Skipped automatically when
chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.

Fixture structure mirrors a job top card: the Save control is the only
button-like element whose text matches a locale label, scoped to ``main``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.extractor import (
    _JOB_SAVE_CLICK_JS,
    _JOB_SAVE_STATE_JS,
)

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
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


class TestJobSaveStateJs:
    async def test_reads_saved_control(self, dom_page):
        await dom_page.set_content(_page_html(SAVED_CARD))
        assert (
            await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) == "saved"
        )

    async def test_reads_unsaved_control(self, dom_page):
        await dom_page.set_content(_page_html(UNSAVED_CARD))
        assert (
            await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) == "unsaved"
        )

    async def test_skips_expander_and_disabled_controls(self, dom_page):
        """The disabled Save and the More expander lose to the real control.

        All three buttons carry the unsaved label; without the attribute
        exclusions the scan sees three matches and returns null, so "unsaved"
        here proves exactly one control survived them.
        """
        await dom_page.set_content(_page_html(EXPANDER_AND_DISABLED_CARD))
        assert (
            await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) == "unsaved"
        )

    async def test_two_matching_controls_yield_null(self, dom_page):
        """A sidebar Save button in main makes the scan ambiguous, fail closed."""
        await dom_page.set_content(_page_html(AMBIGUOUS_CARD))
        assert await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) is None

    async def test_no_control_yields_null(self, dom_page):
        await dom_page.set_content(_page_html(NO_CONTROL_CARD))
        assert await dom_page.evaluate(_JOB_SAVE_STATE_JS, {"labels": LABELS}) is None


class TestJobSaveClickJs:
    async def test_click_dispatches_on_expected_label(self, dom_page):
        await dom_page.set_content(_page_html(UNSAVED_CARD))
        await dom_page.evaluate(
            """({ expectedLabel }) => {
                const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
                const target = Array.from(
                    document.querySelectorAll('main button')
                ).find(
                    element =>
                        normalize(element.innerText || element.textContent) ===
                        expectedLabel
                );
                if (!target) throw new Error('Expected Save control was not found');
                target.addEventListener('click', () => {
                    target.dataset.clicked = 'true';
                });
            }""",
            {"expectedLabel": LABELS["unsaved"]},
        )
        clicked = await dom_page.evaluate(
            _JOB_SAVE_CLICK_JS, {"expectedLabel": LABELS["unsaved"]}
        )
        assert clicked is True
        # The event listener proves that the expected control received click().
        state = await dom_page.evaluate(
            """() => {
                const buttons = document.querySelectorAll('main button');
                return Array.from(buttons).some((b) => b.dataset.clicked);
            }"""
        )
        assert state is True

    async def test_click_refuses_when_label_not_found(self, dom_page):
        """Asking to click 'Saved' on an unsaved card dispatches nothing."""
        await dom_page.set_content(_page_html(UNSAVED_CARD))
        clicked = await dom_page.evaluate(
            _JOB_SAVE_CLICK_JS, {"expectedLabel": LABELS["saved"]}
        )
        assert clicked is False

    async def test_click_refuses_when_ambiguous(self, dom_page):
        await dom_page.set_content(_page_html(AMBIGUOUS_CARD))
        clicked = await dom_page.evaluate(
            _JOB_SAVE_CLICK_JS, {"expectedLabel": LABELS["unsaved"]}
        )
        assert clicked is False

    async def test_click_skips_disabled_control(self, dom_page):
        """A disabled Save must not be clicked even when it is the only one."""
        await dom_page.set_content(
            _page_html('<main><button type="button" disabled>Save</button></main>')
        )
        clicked = await dom_page.evaluate(
            _JOB_SAVE_CLICK_JS, {"expectedLabel": LABELS["unsaved"]}
        )
        assert clicked is False
