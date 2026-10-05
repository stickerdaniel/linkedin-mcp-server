"""Browser-DOM tests for reading how a job posting takes applications.

The unit suite mocks ``page.evaluate``, so the programs behind
``JobPageReader.read_apply_link`` never execute there. These run the whole read
against synthetic postings in headless chromium, with the navigation stubbed and
every request answered by a route that records it, so a test can say what was
never asked for. The markup carries the attributes measured on 2026-09-14 and
2026-09-19 and none of LinkedIn's classes, so it is a claim about which signals
are read, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import JobApplyRead, JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB_URL = "https://www.linkedin.com/jobs/view/123/"
#: The employer's short link, which a real one would redirect onwards.
EMPLOYER_HOST = "grnh.example"
EMPLOYER_LINK = f"https://{EMPLOYER_HOST}/short"

EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true"'
    ' aria-label="Easy Apply to this job">Easy Apply</a>'
)
OTHER_EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/999/apply/">Easy Apply</a>'
)
EXTERNAL = '<button type="button" aria-label="Apply on company website">Apply</button>'


def safety(destination: str) -> str:
    """LinkedIn's interstitial towards ``destination``, encoded as measured."""
    return (
        "https://www.linkedin.com/safety/go/?url="
        f"{quote(destination, safe='')}&isSdui=true"
    )


def external_link(href: str) -> str:
    """The external Apply as a link into the interstitial, as measured."""
    return (
        f'<a target="_blank" aria-label="Apply on company website" href="{href}">'
        "Apply</a>"
    )


#: Records a click on any link in the posting and keeps it from navigating.
NO_CLICK = """
    for (const anchor of document.querySelectorAll('main a')) {
        anchor.addEventListener('click', (event) => {
            event.preventDefault();
            document.body.dataset.clicked = 'true';
        });
    }
"""


def click_opens_dialog(href: str) -> str:
    return f"""
    document.querySelector('main button').addEventListener('click', () => {{
        document.body.dataset.clicked = 'true';
        const dialog = document.createElement('dialog');
        dialog.innerHTML = '<a href="https://www.linkedin.com/in/me/">Me</a>'
            + '<a target="_blank" href="{href}">Continue</a>';
        document.body.appendChild(dialog);
        dialog.showModal();
    }});
    """


async def clicked(page) -> bool:
    """Whether the page's Apply handler ran.

    The handler records it on the DOM rather than in a global: a page script
    runs in the main world while ``evaluate`` defaults to an isolated one,
    where the page's globals always read undefined. Both worlds share the DOM.
    """
    return await page.evaluate("() => document.body.dataset.clicked === 'true'")


def posting(control: str, *, state: str = "", below: str = "", script: str = "") -> str:
    """A top card, the description, and what sits below it.

    The title opens with "Applied" on purpose, so every posting here would read
    as applied to if the state line were matched by its first word.
    """
    return (
        "<main>"
        f"<h1>Applied AI Engineer</h1><p>Acme</p>{state}{control}"
        "<h2>About the job</h2><p>Build agents.</p>"
        f"{below}"
        "</main>"
        f"<script>{script}</script>"
    )


@pytest.fixture
def requested() -> list[str]:
    return []


@pytest.fixture
async def dom_page(requested):
    async def answer(route):
        requested.append(route.request.url)
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
    monkeypatch.setattr(job_pages, "_APPLY_READY_TIMEOUT", 1.0)


async def read(page, html: str) -> JobApplyRead:
    """Serve ``html`` as the posting and read it the way the workflow does."""
    await page.goto(JOB_URL)
    await page.set_content(html)
    session = PageSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.read_apply_link(JOB_URL, "123", JOB_APPLY_EN_US)


async def test_easy_apply_is_the_postings_own_apply_route(dom_page):
    assert await read(dom_page, posting(EASY_APPLY)) == JobApplyRead("easy_apply")


async def test_an_external_apply_link_is_read_off_its_href_without_a_click(
    dom_page, requested
):
    """The top card's link wins over a same-text link in the description."""
    below = external_link(safety("https://acme.example/"))
    html = posting(external_link(safety(EMPLOYER_LINK)), below=below, script=NO_CLICK)

    assert await read(dom_page, html) == JobApplyRead("external", EMPLOYER_LINK)
    assert not await clicked(dom_page)
    assert not any("/safety/go" in url for url in requested)
    assert not any(EMPLOYER_HOST in url for url in requested)


async def test_an_outbound_link_that_is_not_apply_is_not_the_answer(dom_page):
    link = safety("https://acme.example/")
    html = posting(f'<a href="{link}">Our website</a>', script=NO_CLICK)

    assert await read(dom_page, html) == JobApplyRead("unknown")


async def test_an_external_apply_link_below_the_description_is_not_this_postings(
    dom_page,
):
    html = posting("", below=external_link(safety(EMPLOYER_LINK)), script=NO_CLICK)

    assert await read(dom_page, html) == JobApplyRead("unknown")


async def test_an_external_apply_link_without_a_description_boundary_is_unknown(
    dom_page,
):
    html = posting(external_link(safety(EMPLOYER_LINK)), script=NO_CLICK).replace(
        "<h2>About the job</h2>", ""
    )

    assert await read(dom_page, html) == JobApplyRead("unknown")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:9", "%31%32%37.0.0.%31", "127.0.0.1\\@jobs.example.com"],
)
async def test_an_external_apply_link_naming_this_host_has_no_address(
    dom_page, requested, host
):
    html = posting(external_link(safety(f"http://{host}/x")), script=NO_CLICK)

    assert await read(dom_page, html) == JobApplyRead("external")
    assert not any("127.0.0.1" in url for url in requested)


async def test_an_apply_button_is_not_clicked(dom_page, requested):
    """The button variant, measured once on 2026-09-14, is not read.

    Clicking it counts as an apply on the posting, so a posting carrying it
    answers `unknown` rather than being clicked for its address.
    """
    html = posting(EXTERNAL, script=click_opens_dialog(safety(EMPLOYER_LINK)))

    assert await read(dom_page, html) == JobApplyRead("unknown")
    assert not await clicked(dom_page)
    assert not any(EMPLOYER_HOST in url for url in requested)


@pytest.mark.parametrize(
    "state",
    [
        "<p>Application status</p><p>Application submitted</p><p>2 days ago</p>",
        "<p>Applied 3 days ago</p>",
        "<p>Applied 5mo ago</p>",
    ],
)
async def test_an_applied_posting_is_its_state(dom_page, state):
    html = posting(
        external_link(safety("https://acme.example/")), state=state, script=NO_CLICK
    )

    assert await read(dom_page, html) == JobApplyRead("applied")


@pytest.mark.parametrize(
    "line", ["No longer accepting applications", "Not currently accepting applications"]
)
async def test_a_closed_posting_is_its_state(dom_page, line):
    html = posting("", state=f"<p>{line}</p>")

    assert await read(dom_page, html) == JobApplyRead("closed")


async def test_a_state_waits_for_the_description_boundary(dom_page):
    """Easy Apply is found before the heading renders; the state line is not."""
    late_heading = """<script>
        setTimeout(() => document.querySelector('main').insertAdjacentHTML(
            'beforeend', '<h2>About the job</h2>'), 300);
    </script>"""
    html = (
        posting(EASY_APPLY, state="<p>Application submitted</p>").replace(
            "<h2>About the job</h2>", ""
        )
        + late_heading
    )

    assert await read(dom_page, html) == JobApplyRead("applied")


async def test_state_lines_below_the_description_belong_to_other_postings(dom_page):
    below = "<p>No longer accepting applications</p><p>Applied 2 days ago</p>"

    assert await read(dom_page, posting(EASY_APPLY, below=below)) == JobApplyRead(
        "easy_apply"
    )


async def test_a_posting_with_nothing_to_read_is_unknown(dom_page):
    assert await read(dom_page, posting(OTHER_EASY_APPLY)) == JobApplyRead("unknown")
