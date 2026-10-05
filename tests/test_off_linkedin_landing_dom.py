"""Browser-DOM tests for a navigation that ends on a page LinkedIn did not serve.

The unit suite fakes ``page.url`` and every evaluation, so it cannot show when a
real browser reports a redirect relative to the reads around it. These run the
production readers and auth checks in headless chromium against synthetic
documents, with every request intercepted: the LinkedIn pages and the portal
are both answered by the route handler, and anything else is aborted, so
nothing leaves the machine.

Every fixture is synthetic, so each case is a claim about the algorithm and
never about LinkedIn's markup. Skipped automatically when chromium is not
installed; run locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.core.auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    resolve_remember_me_prompt,
)
from linkedin_mcp_server.core.destination import (
    LINKEDIN_HOST_PATTERN,
    LINKEDIN_LANDING_JS,
    is_linkedin_landing,
)
from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.conversations import ConversationReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.message_sender import MessageSender
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

PROFILE_URL = "https://www.linkedin.com/in/testuser/"
JOB_URL = "https://www.linkedin.com/jobs/view/123/"
PORTAL_URL = "https://portal.invalid/interstitial"
PORTAL_LOGIN_URL = "https://portal.invalid/login"
INTERSTITIAL_TEXT = "OFFLINE INTERSTITIAL, NOT A PROFILE"
#: What the chooser button records when pressed. On the DOM, because a page
#: script runs in the main world and ``evaluate`` in an isolated one.
PRESSED = "document.body.dataset.pressed = 'true'"


@pytest.fixture
async def dom_page(monkeypatch):
    # Off, because a trace screenshot taken while the page redirects can wait
    # out Playwright's 30s default before it gives up, and some of these cases
    # redirect at a moment chosen by a timer. Measured: one run in about ten.
    monkeypatch.setenv("LINKEDIN_TRACE_MODE", "off")
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


def document(main: str, *, title: str = "LinkedIn", script: str = "") -> str:
    """A document tall enough to scroll, carrying *script* after its content."""
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>{title}</title></head>
  <body>
    <main>{main}<div style="height:3000px"></div></main>
    <script>{script}</script>
  </body>
</html>
"""


def profile(script: str = "") -> str:
    return document(
        "<h1>Test User</h1><p>Synthetic profile text</p>",
        title="Test User | LinkedIn",
        script=script,
    )


def portal(title: str = "Network access", extra: str = "") -> str:
    return document(f"<p>{INTERSTITIAL_TEXT}</p>{extra}", title=title)


def chooser() -> str:
    """The saved-account chooser's id, with a button that records a press."""
    return (
        f'<div id="rememberme-div"><button onclick="{PRESSED}">Continue</button></div>'
    )


def redirect_after(milliseconds: int, url: str = PORTAL_URL) -> str:
    return f"setTimeout(() => location.assign({url!r}), {milliseconds});"


async def serve(page, *, linkedin: dict[str, str], portal_html: str) -> None:
    """Answer the LinkedIn pages and the portal; abort everything else.

    The documents redirect by script rather than by an HTTP redirect, because
    a route handler only sees the first request of a redirect chain: the
    browser fetches the target itself, and here that is a DNS lookup.
    """

    async def handle(route) -> None:
        url = route.request.url
        if url in linkedin:
            await route.fulfill(content_type="text/html", body=linkedin[url])
        elif url.startswith("https://portal.invalid/"):
            await route.fulfill(content_type="text/html", body=portal_html)
        else:
            await route.abort()

    await page.route("**/*", handle)


async def pressed(page) -> bool:
    return await page.evaluate("() => document.body.dataset.pressed === 'true'")


async def read_profile(page) -> dict:
    """The profile reader wired the way the facade does, over a real browser."""
    session = PageSession(page)
    navigator = PageNavigator(session)
    message_sender = MessageSender(session, navigator)
    reader = PersonReader(
        session,
        navigator,
        SectionCapture(session, navigator, PageContentReader(session)),
        ProfilePageReader(
            session, lambda: message_sender._read_profile_message_target()
        ),
    )
    return await reader.read_person("testuser", {"main_profile"}, max_scrolls=1)


class _AddressNotYetReported:
    """The real page, with the address the driver held before a redirect.

    Stands in for the window between an address check and the read after it,
    which a real browser opens for a few milliseconds at a time and cannot be
    asked to hold open. Everything but ``url`` is the live page.
    """

    def __init__(self, page: Any, url: str):
        self._page = page
        self.url = url

    def __getattr__(self, name: str) -> Any:
        return getattr(self._page, name)


class TestTheReadAnswersForItsOwnDocument:
    async def test_a_linkedin_document_answers_with_the_scripts_value(self, dom_page):
        await serve(dom_page, linkedin={PROFILE_URL: profile()}, portal_html=portal())
        await dom_page.goto(PROFILE_URL)

        value = await PageSession(dom_page).run_on_linkedin(
            "async (suffix) => document.querySelector('h1').innerText + suffix", "!"
        )

        assert value == "Test User!"

    async def test_another_sites_document_is_refused(self, dom_page):
        await serve(dom_page, linkedin={}, portal_html=portal())
        await dom_page.goto(PORTAL_URL)

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await PageSession(dom_page).run_on_linkedin("() => document.title")


class TestAPortalIsNotReadAsTheProfile:
    async def test_a_redirect_after_the_document_committed(self, dom_page):
        """The reported case: the profile document leaves after 50ms.

        `goto` returns on the profile, so the navigation sees LinkedIn; the
        portal is what is there by the time the page is read.
        """
        await serve(
            dom_page,
            linkedin={PROFILE_URL: profile(redirect_after(50))},
            portal_html=portal(),
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await read_profile(dom_page)

        assert dom_page.url == PORTAL_URL

    async def test_a_redirect_during_the_readiness_scroll(self, dom_page):
        """Leaves only once the reader scrolls, after navigation has judged it.

        The one case only the content read can catch, and caught without
        depending on a timer racing the reads.
        """
        await serve(
            dom_page,
            linkedin={
                PROFILE_URL: profile(
                    "addEventListener('scroll', () => "
                    f"location.assign({PORTAL_URL!r}), {{ once: true }});"
                )
            },
            portal_html=portal(),
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await read_profile(dom_page)

        assert dom_page.url == PORTAL_URL

    async def test_a_portal_dressed_as_linkedin_sign_in(self, dom_page):
        """LinkedIn's title and picker id on another host are not a barrier.

        The refusal comes before any of it is read, so the session is not
        reported as expired and the picker's button is never pressed.
        """
        await serve(
            dom_page,
            linkedin={PROFILE_URL: profile(f"location.replace({PORTAL_URL!r});")},
            portal_html=portal(title="LinkedIn Login", extra=chooser()),
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await read_profile(dom_page)

        assert dom_page.url == PORTAL_URL
        assert not await pressed(dom_page)


class TestTheAuthChecksJudgeTheDocumentTheyRead:
    async def test_a_portal_title_is_not_paired_with_linkedins_address(self, dom_page):
        """The address was checked on LinkedIn; the title came from a portal."""
        await serve(
            dom_page,
            linkedin={},
            portal_html=portal(title="LinkedIn Login", extra=chooser()),
        )
        await dom_page.goto(PORTAL_LOGIN_URL)
        stale = cast(Page, _AddressNotYetReported(dom_page, PROFILE_URL))

        assert await detect_auth_barrier_quick(stale) is None
        assert await detect_auth_barrier(stale) is None

    async def test_linkedins_own_sign_in_title_is_still_a_barrier(self, dom_page):
        await serve(
            dom_page,
            linkedin={PROFILE_URL: document("", title="LinkedIn Login")},
            portal_html=portal(),
        )
        await dom_page.goto(PROFILE_URL)

        result = await detect_auth_barrier_quick(dom_page)

        assert result is not None and result.startswith("login title")

    async def test_a_chooser_that_arrives_on_a_portal_is_not_pressed(self, dom_page):
        """LinkedIn on entry, no chooser; the chooser turns up on a portal.

        The helper waits for the chooser to appear, and what appears after
        200ms is another site's button under the same id.
        """
        await serve(
            dom_page,
            linkedin={PROFILE_URL: profile(redirect_after(200, PORTAL_LOGIN_URL))},
            portal_html=portal(title="LinkedIn Login", extra=chooser()),
        )
        await dom_page.goto(PROFILE_URL)

        assert await resolve_remember_me_prompt(dom_page) is False

        assert dom_page.url == PORTAL_LOGIN_URL
        assert not await pressed(dom_page)

    async def test_linkedins_own_chooser_is_pressed(self, dom_page):
        await serve(
            dom_page,
            linkedin={PROFILE_URL: document(chooser(), title="LinkedIn")},
            portal_html=portal(),
        )
        await dom_page.goto(PROFILE_URL)

        assert await resolve_remember_me_prompt(dom_page, timeout=3000) is True
        assert await pressed(dom_page)


EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true"'
    ' aria-label="Easy Apply to this job">Easy Apply</a>'
)


class TestAPortalIsNotReadAsTheApplyLink:
    async def test_a_redirect_during_apply_readiness(self, dom_page, monkeypatch):
        """The posting renders no apply control, and the portal that replaces
        it carries one pointing back at the posting."""
        monkeypatch.setattr(job_pages, "_APPLY_READY_TIMEOUT", 3.0)
        posting = document(
            "<h1>Engineer</h1><p>Acme</p><h2>About the job</h2><p>Build.</p>",
            script=redirect_after(200),
        )
        await serve(
            dom_page,
            linkedin={JOB_URL: posting},
            portal_html=portal(
                extra=f"<h1>Engineer</h1>{EASY_APPLY}<h2>About the job</h2>"
            ),
        )
        session = PageSession(dom_page)
        reader = JobPageReader(
            session, PageNavigator(session), PageContentReader(session)
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await reader.read_apply_link(JOB_URL, "123", JOB_APPLY_EN_US)

        assert dom_page.url == PORTAL_URL


#: Every address the Python classifier is tested on, in forms a browser keeps
#: as written, plus spellings it serializes differently. Parity is claimed for
#: these, not for raw strings in general: the browser normalizes IDN,
#: percent-encoded hosts and Unicode separators where urllib does not, and a
#: page script is only ever handed a browser-serialized address.
HOST_RULE_ADDRESSES = [
    "https://www.linkedin.com/in/testuser/",
    "https://linkedin.com/feed/",
    "https://de.linkedin.com/in/testuser/",
    "https://WWW.LinkedIn.COM/feed/",
    "https://www.linkedin.com./feed/",
    "https://www.linkedin.com:443/feed/",
    "https://www.linkedin.com:0443/feed/",
    "https://www.linkedin.com/jobs/search/?keywords=python#top",
    "https://evil-linkedin.com/in/testuser/",
    "https://notlinkedin.com/feed/",
    "https://linkedin.com.evil.test/feed/",
    "https://www.linkedin.com.evil.test/in/testuser/",
    "https://portal.invalid/login",
    "http://www.linkedin.com/feed/",
    "https://www.linkedin.com:8443/feed/",
    "https://user:pass@www.linkedin.com/feed/",
    "https://user@www.linkedin.com/feed/",
    "https://@linkedin.com/feed/",
    "https://www.linkedin.com/in/someone@example/",
    "https://www.linkedin.com../feed/",
    # A Cyrillic i (U+0456) in place of the Latin one, written as an escape
    # so the source itself carries no look-alike letter.
    "https://www.l\u0456nkedin.com/feed/",
    "https://[::1/feed/",
    "about:blank",
    "data:text/html,<main>LinkedIn</main>",
    "blob:https://www.linkedin.com/1b2c3d",
    "file:///www.linkedin.com/feed/",
    "chrome-error://chromewebdata/",
    "",
]


async def test_the_page_scripts_apply_the_same_host_rule(dom_page):
    """Two copies of one rule agree on browser-serialized addresses.

    Python's, and the one a page script runs before it acts.
    """
    in_page = await dom_page.evaluate(
        f"""(addresses) => {{
            const onLinkedIn = {LINKEDIN_LANDING_JS};
            return addresses.map(href => onLinkedIn(href, {LINKEDIN_HOST_PATTERN!r}));
        }}""",
        HOST_RULE_ADDRESSES,
    )

    assert dict(zip(HOST_RULE_ADDRESSES, in_page)) == {
        address: is_linkedin_landing(address) for address in HOST_RULE_ADDRESSES
    }


def conversation_rows() -> str:
    """Two rows a conversation scan would click; each records the click."""
    return "".join(
        f'<li><label aria-label="Select conversation with {name}">'
        f'<div class="msg-conversation-listitem__link" '
        f"onclick=\"document.body.dataset.pressed = 'true'\">{name}</div>"
        "</label></li>"
        for name in ("Ada Lovelace", "Grace Hopper")
    )


class TestAPortalRowIsNeverClicked:
    async def test_the_scan_refuses_a_portal_before_clicking_a_row(self, dom_page):
        """The compose page redirected before the scan; the rows are a portal's."""
        await serve(
            dom_page,
            linkedin={},
            portal_html=document(f"<ul>{conversation_rows()}</ul>"),
        )
        await dom_page.goto(PORTAL_URL)
        session = PageSession(dom_page)
        reader = ConversationReader(
            session,
            PageNavigator(session),
            PageContentReader(session),
            ProfilePageReader(session, AsyncMock()),
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await reader._extract_conversation_thread_refs(5, "inbox")

        assert not await pressed(dom_page)

    async def test_a_page_script_runs_on_linkedin(self, dom_page):
        await serve(dom_page, linkedin={PROFILE_URL: profile()}, portal_html=portal())
        await dom_page.goto(PROFILE_URL)

        await PageSession(dom_page).run_on_linkedin(f"() => {{ {PRESSED}; }}")

        assert await pressed(dom_page)
