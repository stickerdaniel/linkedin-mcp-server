"""Browser-DOM tests for dismissing a job from a LinkedIn job search.

The unit suite mocks ``page.evaluate``, so the program behind
``JobPageReader.dismiss_search_card`` never executes there. These run the whole
action against a synthetic results list in headless chromium, with the
navigation stubbed, and read a synthetic posting's document title the way
``JobPageReader.read_posting_title`` does. The markup carries the structure measured on 2026-09-28:
each result is an `li[data-occludable-job-id]`, its dismiss control a `button`
around `[data-test-icon="close-small"]`, some cards also carry a "More options"
button, and a click swaps the dismiss button for one around `undo-small`. None
of LinkedIn's classes, so it is a claim about which signals are read, not about
LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

SEARCH_URL = "https://www.linkedin.com/jobs/search/?keywords=Engineer"
POSTING_URL = "https://www.linkedin.com/jobs/view/111/"

# A dismiss click records the card in the DOM and swaps the button for the
# undo one, unless the page says the click is ignored. An attribute rather
# than a `window` global, because patchright evaluates in an isolated world.
DISMISS_SCRIPT = """
for (const icon of document.querySelectorAll('[data-test-icon="close-small"]')) {
    const button = icon.closest('button');
    button.addEventListener('click', () => {
        const card = button.closest('li');
        const chosen = JSON.parse(document.body.dataset.chosen || '[]');
        chosen.push(card.dataset.occludableJobId);
        document.body.dataset.chosen = JSON.stringify(chosen);
        if (document.body.hasAttribute('data-ignore')) return;
        button.outerHTML = '<button aria-label="Job is dismissed, undo">'
            + '<svg data-test-icon="undo-small"></svg></button>'
            + '<p>We won\\'t show you this job again.</p>';
    });
}
"""

MORE = (
    '<button aria-label="More options">'
    '<svg data-test-icon="overflow-web-ios-small"></svg></button>'
)
DISMISS = (
    '<button aria-label="Dismiss Engineer job">'
    '<svg data-test-icon="close-small"></svg></button>'
)
UNDO = (
    '<button aria-label="Engineer job is dismissed, undo">'
    '<svg data-test-icon="undo-small"></svg></button>'
)


def card(job_id: str, buttons: str = MORE + DISMISS) -> str:
    return (
        f'<li data-occludable-job-id="{job_id}">'
        f'<a href="https://www.linkedin.com/jobs/view/{job_id}/">Job {job_id}</a>'
        f"<p>Acme · Paris</p>{buttons}</li>"
    )


def search(*cards: str, script: str = DISMISS_SCRIPT) -> str:
    """A results list beside a detail pane that holds no card."""
    return (
        f"<main><ul>{''.join(cards)}</ul>"
        '<section><a href="https://www.linkedin.com/jobs/view/999/">Pane</a>'
        f"</section></main><script>{script}</script>"
    )


@pytest.fixture
async def dom_page():
    async def answer(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(job_pages, "_SEARCH_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_SEARCH_CARD_RENDER_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_DISMISS_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_POSTING_TITLE_TIMEOUT", 1.0)


def _reader(page) -> JobPageReader:
    session = PageSession(page)
    return JobPageReader(session, PageNavigator(session), PageContentReader(session))


async def act(page, html: str, job_id: str) -> str:
    """Serve ``html`` as the search and dismiss the way the workflow does."""
    await page.goto(SEARCH_URL)
    await page.set_content(html)
    reader = _reader(page)
    with (
        patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "scroll_job_sidebar", new_callable=AsyncMock),
    ):
        return await reader.dismiss_search_card(SEARCH_URL, job_id)


async def chosen(page) -> list[str]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


async def test_the_jobs_own_dismiss_button_is_clicked(dom_page):
    html = search(card("111"), card("222"), card("333", DISMISS))

    assert await act(dom_page, html, "222") == "dismissed"
    assert await chosen(dom_page) == ["222"]
    assert await dom_page.locator('[data-test-icon="close-small"]').count() == 2


async def test_a_job_only_in_the_detail_pane_is_absent(dom_page):
    assert await act(dom_page, search(card("111")), "999") == "absent"
    assert await chosen(dom_page) == []


async def test_a_card_without_a_dismiss_button_clicks_nothing(dom_page):
    assert await act(dom_page, search(card("111", MORE)), "111") == "no_button"
    assert await chosen(dom_page) == []


async def test_a_card_in_its_undo_state_is_already_dismissed(dom_page):
    html = search(card("111", UNDO))

    assert await act(dom_page, html, "111") == "already_dismissed"


async def test_a_click_that_changes_nothing_is_not_a_dismissal(dom_page):
    html = search(
        card("111"),
        script="document.body.setAttribute('data-ignore', '');" + DISMISS_SCRIPT,
    )

    assert await act(dom_page, html, "111") == "not_dismissed"
    assert await chosen(dom_page) == ["111"]


async def test_a_job_listed_twice_is_not_guessed_at(dom_page):
    assert await act(dom_page, search(card("111"), card("111")), "111") == ("no_button")
    assert await chosen(dom_page) == []


async def test_an_occluded_card_is_waited_for_until_it_renders(dom_page):
    """A card far from the viewport is an empty `li` until it nears it."""
    html = search(
        '<li data-occludable-job-id="111"></li>',
        script=(
            "setTimeout(() => {"
            "document.querySelector('li').outerHTML = "
            + repr(card("111"))
            + ";"
            + DISMISS_SCRIPT
            + "}, 300);"
        ),
    )

    assert await act(dom_page, html, "111") == "dismissed"
    assert await chosen(dom_page) == ["111"]


async def test_a_reloaded_search_without_the_card_does_not_list_it(dom_page):
    await dom_page.goto(SEARCH_URL)
    await dom_page.set_content(search(card("111")))
    reader = _reader(dom_page)
    with (
        patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(PageSession, "scroll_job_sidebar", new_callable=AsyncMock),
    ):
        assert await reader.search_lists_job(SEARCH_URL, "111") is True
        assert await reader.search_lists_job(SEARCH_URL, "222") is False


async def read_title(page, landing: str, html: str) -> tuple[str, str]:
    """Serve ``html`` at ``landing`` and read it as job 111's posting."""
    await page.goto(landing)
    await page.set_content(html)
    reader = _reader(page)
    with patch.object(
        reader._navigator, "_navigate_to_page", new_callable=AsyncMock
    ) as navigate:
        found = await reader.read_posting_title("111")
    navigate.assert_awaited_once_with(POSTING_URL)
    return found


async def test_the_posting_title_names_the_card_title_and_company(dom_page):
    html = "<title>(3) Engineer | Platform | Acme | LinkedIn</title><main></main>"

    assert await read_title(dom_page, POSTING_URL, html) == (
        "Engineer | Platform",
        "Acme",
    )


async def test_a_posting_title_set_late_is_waited_for(dom_page):
    html = (
        "<title>LinkedIn</title><main></main><script>setTimeout(() => {"
        "document.title = 'Engineer | Acme | LinkedIn'}, 300);</script>"
    )

    assert await read_title(dom_page, POSTING_URL, html) == ("Engineer", "Acme")


async def test_a_posting_title_that_never_takes_shape_is_an_error(dom_page):
    html = "<title>(3) LinkedIn</title><main></main>"

    with pytest.raises(LinkedInOperationError, match=r"'\(3\) LinkedIn'"):
        await read_title(dom_page, POSTING_URL, html)


async def test_a_navigation_off_the_posting_is_an_error(dom_page):
    html = "<title>Engineer | Acme | LinkedIn</title><main></main>"

    with pytest.raises(LinkedInOperationError, match="/jobs/view/222/"):
        await read_title(dom_page, "https://www.linkedin.com/jobs/view/222/", html)
