"""Browser-DOM tests for dismissing a job from a LinkedIn job search.

The unit suite mocks ``page.evaluate``, so the program behind
``JobPageReader.dismiss_search_card`` never executes there. These run the whole
action against a synthetic results list in headless chromium, with the
navigation stubbed, and read a synthetic posting's document title the way
``JobPageReader.read_posting_title`` does. The markup carries the structure
measured on both searches. Classic (2026-09-28): each result is an
`li[data-occludable-job-id]`, its dismiss control a `button` around
`[data-test-icon="close-small"]`, and some cards also carry a "More options"
button. Redesigned (2026-10-14): each result is an element keyed
`componentkey="job-card-component-ref-<id>"` around a nested one with the same
key, its dismiss control a `button` around `svg#close-small`. In both a click
swaps the dismiss button for one around `undo-small`. None of LinkedIn's
classes, so it is a claim about which signals are read, not about LinkedIn's
layout.

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
WORLDWIDE_URL = SEARCH_URL + "&geoId=92000000"
POSTING_URL = "https://www.linkedin.com/jobs/view/111/"

# A dismiss click records the card in the DOM and swaps the button for the
# undo one, unless the page says the click is ignored. An attribute rather
# than a `window` global, because patchright evaluates in an isolated world.
DISMISS_SCRIPT = """
for (const icon of document.querySelectorAll(
    '[data-test-icon="close-small"], svg#close-small'
)) {
    const button = icon.closest('button');
    button.addEventListener('click', () => {
        const card = button.closest('li, [role="button"]');
        const chosen = JSON.parse(document.body.dataset.chosen || '[]');
        chosen.push(card.dataset.occludableJobId
            ?? card.getAttribute('componentkey').split('-').pop());
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


REDESIGNED_DISMISS = (
    '<button aria-label="Dismiss Engineer job"><svg id="close-small"></svg></button>'
)
REDESIGNED_UNDO = (
    '<button aria-label="Engineer job is dismissed, undo">'
    '<svg id="undo-small"></svg></button>'
)


def redesigned_card(job_id: str, buttons: str = REDESIGNED_DISMISS) -> str:
    key = f"job-card-component-ref-{job_id}"
    return (
        f'<div><div role="button" componentkey="{key}"><div componentkey="{key}">'
        f"<p>Job {job_id}</p><p>Acme · Paris</p>{buttons}</div></div></div>"
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
        patch.object(
            PageSession,
            "scroll_job_sidebar",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        outcome, _ = await reader.dismiss_search_card(SEARCH_URL, job_id)
        return outcome


async def chosen(page) -> list[str]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


async def test_the_jobs_own_dismiss_button_is_clicked(dom_page):
    html = search(card("111"), card("222"), card("333", DISMISS))

    assert await act(dom_page, html, "222") == "dismissed"
    assert await chosen(dom_page) == ["222"]
    assert await dom_page.locator('[data-test-icon="close-small"]').count() == 2


async def test_a_redesigned_card_is_dismissed_by_its_own_button(dom_page):
    html = search(redesigned_card("111"), redesigned_card("222"))

    assert await act(dom_page, html, "222") == "dismissed"
    assert await chosen(dom_page) == ["222"]
    assert await dom_page.locator("svg#close-small").count() == 1


async def test_a_redesigned_card_in_its_undo_state_is_already_dismissed(dom_page):
    html = search(redesigned_card("111", REDESIGNED_UNDO))

    assert await act(dom_page, html, "111") == "already_dismissed"


async def test_a_redesigned_reload_counts_each_card_once(dom_page):
    html = search(redesigned_card("222"), redesigned_card("333"))

    assert await lists(dom_page, html, "111", min_cards=2) is False
    with pytest.raises(LinkedInOperationError, match="3 were expected"):
        await lists(dom_page, html, "111", min_cards=3)


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
        patch.object(
            PageSession,
            "scroll_job_sidebar",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        assert await reader.search_lists_job(SEARCH_URL, "111") is True
        assert await reader.search_lists_job(SEARCH_URL, "222") is False


async def lists(
    page,
    html: str,
    job_id: str,
    *,
    landing: str = SEARCH_URL,
    asked: str = SEARCH_URL,
    scroll_raised: bool = False,
    min_cards: int = 1,
) -> bool:
    """Serve ``html`` at ``landing`` and ask whether the search at ``asked``
    lists ``job_id``."""
    await page.goto(landing)
    await page.set_content(html)
    reader = _reader(page)
    with (
        patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            PageSession,
            "scroll_job_sidebar",
            new_callable=AsyncMock,
            return_value=scroll_raised,
        ),
    ):
        return await reader.search_lists_job(asked, job_id, min_cards)


async def test_a_reload_that_rendered_no_card_confirms_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="rendered no result cards"):
        await lists(dom_page, search(), "111")


async def test_a_reload_that_rendered_fewer_cards_confirms_nothing(dom_page):
    """A rail that stopped short of the cards it showed before may also stop
    short of the job's."""
    with pytest.raises(LinkedInOperationError, match="2 were expected"):
        await lists(dom_page, search(card("222")), "111", min_cards=2)


async def test_a_reload_with_the_other_cards_does_not_list_the_job(dom_page):
    html = search(card("222"), card("333"))

    assert await lists(dom_page, html, "111", min_cards=2) is False


async def test_the_dismissal_reports_the_cards_the_search_showed(dom_page):
    await dom_page.goto(SEARCH_URL)
    await dom_page.set_content(search(card("111"), card("222"), card("333")))
    reader = _reader(dom_page)
    with (
        patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock),
        patch.object(
            PageSession,
            "scroll_job_sidebar",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        assert await reader.dismiss_search_card(SEARCH_URL, "222") == ("dismissed", 3)


async def test_a_rail_that_could_not_be_scrolled_confirms_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="could not be scrolled"):
        await lists(dom_page, search(card("222")), "111", scroll_raised=True)


async def test_a_search_that_lost_its_keywords_confirms_nothing(dom_page):
    landing = "https://www.linkedin.com/jobs/search/?keywords=Other"
    with pytest.raises(LinkedInOperationError, match="lost its keywords"):
        await lists(dom_page, search(card("222")), "111", landing=landing)


async def test_a_search_that_lost_its_query_confirms_nothing(dom_page):
    landing = "https://www.linkedin.com/jobs/search/"
    with pytest.raises(LinkedInOperationError, match="lost its keywords"):
        await lists(dom_page, search(card("222")), "111", landing=landing)


async def test_a_search_moved_to_another_place_confirms_nothing(dom_page):
    landing = "https://www.linkedin.com/jobs/search/?keywords=Engineer&geoId=105015875"
    with pytest.raises(LinkedInOperationError, match="lost its geoId"):
        await lists(
            dom_page, search(card("222")), "111", landing=landing, asked=WORLDWIDE_URL
        )


async def test_a_search_that_lost_its_place_confirms_nothing(dom_page):
    with pytest.raises(LinkedInOperationError, match="lost its geoId"):
        await lists(dom_page, search(card("222")), "111", asked=WORLDWIDE_URL)


async def test_linkedin_may_add_the_current_job_and_move_to_its_redesign(dom_page):
    landing = (
        "https://www.linkedin.com/jobs/search-results/"
        "?keywords=Engineer&geoId=92000000&currentJobId=222"
    )
    found = await lists(
        dom_page, search(card("222")), "111", landing=landing, asked=WORLDWIDE_URL
    )
    assert found is False


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
