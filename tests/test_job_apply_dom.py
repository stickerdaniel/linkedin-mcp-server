"""Browser-DOM tests for reading how a job posting takes applications.

The unit suite mocks ``page.evaluate``, so the programs behind
``JobPageReader.read_apply_link`` never execute there. These run the whole read
against synthetic postings in headless chromium, with the navigation stubbed.
LinkedIn's addresses are answered by a route and the employer's short link by a
loopback server, because a route sees only the first request of a redirect and
would let the second one out. That server answers to a public name mapped onto
the loopback by the browser's own resolver, because a destination written as a
loopback address is refused before it is ever loaded. The markup carries the
attributes measured on
2026-09-14 and none of LinkedIn's classes, so it is a claim about which signals
are read, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import asyncio
import threading

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping import job_pages
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.job_pages import JobApplyRead, JobPageReader
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import JOB_APPLY_EN_US

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB_URL = "https://www.linkedin.com/jobs/view/123/"
#: The employer's site. A name rather than `127.0.0.1`, which the apply policy
#: refuses; the browser is told to resolve it to the loopback server below.
EMPLOYER_HOST = "employer.example"
EMPLOYER_ORIGIN = f"http://{EMPLOYER_HOST}:"

EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true"'
    ' aria-label="Easy Apply to this job">Easy Apply</a>'
)
OTHER_EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/999/apply/">Easy Apply</a>'
)
EXTERNAL = '<button type="button" aria-label="Apply on company website">Apply</button>'


class _EmployerSite(BaseHTTPRequestHandler):
    """A short link that redirects to the job page behind it."""

    def do_GET(self) -> None:
        if self.path == "/short":
            self.send_response(302)
            self.send_header("Location", "/acme/jobs/1")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>Apply for this job</p>")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def employer():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmployerSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"{EMPLOYER_ORIGIN}{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def safety(destination: str) -> str:
    """LinkedIn's interstitial towards ``destination``, encoded as measured."""
    return (
        "https://www.linkedin.com/safety/go/?url="
        f"{quote(destination, safe='')}&isSdui=true"
    )


def click_opens_dialog(href: str) -> str:
    return f"""
    document.querySelector('main button').addEventListener('click', () => {{
        window.clicked = true;
        const dialog = document.createElement('dialog');
        dialog.innerHTML = '<a href="https://www.linkedin.com/in/me/">Me</a>'
            + '<a target="_blank" href="{href}">Continue</a>';
        document.body.appendChild(dialog);
        dialog.showModal();
    }});
    """


def click_opens_tab(href: str) -> str:
    return f"""
    document.querySelector('main button').addEventListener('click', () => {{
        window.open('{href}', '_blank');
    }});
    """


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
        if route.request.url.startswith(EMPLOYER_ORIGIN):
            await route.continue_()
        else:
            await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium",
                headless=True,
                args=[f"--host-resolver-rules=MAP {EMPLOYER_HOST} 127.0.0.1"],
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
    monkeypatch.setattr(job_pages, "_APPLY_ANSWER_TIMEOUT", 1.0)


async def read(page, html: str) -> JobApplyRead:
    """Serve ``html`` as the posting and read it the way the workflow does."""
    await page.goto(JOB_URL)
    await page.set_content(html)
    session = ScrapingSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.read_apply_link(JOB_URL, "123", JOB_APPLY_EN_US)


async def test_easy_apply_is_the_postings_own_apply_route(dom_page):
    assert await read(dom_page, posting(EASY_APPLY)) == JobApplyRead("easy_apply")


async def test_an_external_apply_is_read_off_its_dialog_and_followed(
    dom_page, requested, employer
):
    """Continue's link is decoded and never followed, and the short link is."""
    short = f"{employer}/short"
    html = posting(
        EXTERNAL, below=OTHER_EASY_APPLY, script=click_opens_dialog(safety(short))
    )

    assert await read(dom_page, html) == JobApplyRead(
        "external", f"{employer}/acme/jobs/1"
    )
    assert short in requested
    assert not any("/safety/go" in url for url in requested)


async def test_a_tab_linkedin_opens_is_read_and_closed(dom_page, employer):
    html = posting(EXTERNAL, script=click_opens_tab(safety(f"{employer}/short")))

    assert await read(dom_page, html) == JobApplyRead(
        "external", f"{employer}/acme/jobs/1"
    )
    assert dom_page.context.pages == [dom_page]


async def test_an_outbound_link_outside_a_dialog_is_not_the_answer(dom_page):
    link = safety("https://acme.example/")
    html = posting(EXTERNAL, below=f'<a href="{link}">Our website</a>')

    assert await read(dom_page, html) == JobApplyRead("external")


async def test_an_apply_below_the_description_belongs_to_another_posting(dom_page):
    """A "More jobs" card's Apply is neither this posting's type nor clicked."""
    html = posting(
        "", below=EXTERNAL, script=click_opens_dialog(safety("https://acme.example/"))
    )

    assert await read(dom_page, html) == JobApplyRead("unknown")
    assert await dom_page.evaluate("() => window.clicked === true") is False


async def test_a_destination_inside_this_host_is_never_loaded(dom_page, requested):
    """A posting naming the loopback answers external, and nothing is fetched."""
    html = posting(EXTERNAL, script=click_opens_dialog(safety("http://127.0.0.1:9/x")))

    assert await read(dom_page, html) == JobApplyRead("external")
    assert not any("127.0.0.1" in url for url in requested)


async def test_a_name_resolving_into_this_host_is_never_loaded(
    dom_page, requested, monkeypatch
):
    """A public name pointing at the loopback is refused by the resolver."""

    async def answers_loopback(host, port, **kwargs):
        return [(None, None, None, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", answers_loopback, raising=False
    )
    html = posting(
        EXTERNAL, script=click_opens_dialog(safety("https://jobs.acme.example/x"))
    )

    assert await read(dom_page, html) == JobApplyRead("external")
    assert not any("jobs.acme.example" in url for url in requested)


@pytest.mark.parametrize(
    "state",
    [
        "<p>Application status</p><p>Application submitted</p><p>2 days ago</p>",
        "<p>Applied 3 days ago</p>",
        "<p>Applied 5mo ago</p>",
    ],
)
async def test_an_applied_posting_is_not_clicked(dom_page, state):
    html = posting(
        EXTERNAL,
        state=state,
        script=click_opens_dialog(safety("https://acme.example/")),
    )

    assert await read(dom_page, html) == JobApplyRead("applied")
    # The page script sets window.clicked in the main world. evaluate defaults
    # to isolated_context=True, where that variable is always undefined and the
    # assertion cannot fail, so this one reads the world the click writes to.
    assert (
        await dom_page.evaluate("() => window.clicked === true", isolated_context=False)
        is False
    )


@pytest.mark.parametrize(
    "line", ["No longer accepting applications", "Not currently accepting applications"]
)
async def test_a_closed_posting_is_its_state(dom_page, line):
    html = posting("", state=f"<p>{line}</p>")

    assert await read(dom_page, html) == JobApplyRead("closed")


async def test_state_lines_below_the_description_belong_to_other_postings(dom_page):
    below = "<p>No longer accepting applications</p><p>Applied 2 days ago</p>"

    assert await read(dom_page, posting(EASY_APPLY, below=below)) == JobApplyRead(
        "easy_apply"
    )


async def test_a_posting_with_nothing_to_read_is_unknown(dom_page):
    assert await read(dom_page, posting(OTHER_EASY_APPLY)) == JobApplyRead("unknown")
