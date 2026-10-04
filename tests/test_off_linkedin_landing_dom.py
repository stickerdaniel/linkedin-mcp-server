"""Browser-DOM tests for a navigation that ends on a page LinkedIn did not serve.

The unit suite fakes ``page.url`` and the content read, so it cannot show when
a real browser reports a redirect relative to the reads around it. These run
the production profile reader in headless chromium against synthetic
documents, with every request intercepted: the LinkedIn profile and the portal
are both answered by the route handler, and anything else is aborted, so
nothing leaves the machine.

Every fixture is synthetic, so each case is a claim about the algorithm and
never about LinkedIn's markup. Skipped automatically when chromium is not
installed; run locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.message_sender import MessageSender
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.profile_page import ProfilePageReader
from linkedin_mcp_server.linkedin.session import PageSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

PROFILE_URL = "https://www.linkedin.com/in/testuser/"
PORTAL_URL = "https://portal.invalid/interstitial"
INTERSTITIAL_TEXT = "OFFLINE INTERSTITIAL, NOT A PROFILE"


@pytest.fixture
async def dom_page(monkeypatch):
    # Off, because a trace screenshot taken while the page redirects can wait
    # out Playwright's 30s default before it gives up, and one of these cases
    # redirects at a moment chosen by a timer. Measured: one run in about ten.
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


def profile(script: str) -> str:
    """A profile document tall enough to scroll, carrying *script*."""
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Test User | LinkedIn</title></head>
  <body>
    <main>
      <h1>Test User</h1>
      <p>Synthetic profile text</p>
      <div style="height:3000px"></div>
    </main>
    <script>{script}</script>
  </body>
</html>
"""


def portal(title: str = "Network access", extra: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>{title}</title></head>
  <body><main><p>{INTERSTITIAL_TEXT}</p>{extra}</main></body>
</html>
"""


async def serve(page, *, profile_html: str, portal_html: str) -> None:
    """Answer the profile and the portal; abort everything else.

    The documents redirect by script rather than by an HTTP redirect, because
    a route handler only sees the first request of a redirect chain: the
    browser fetches the target itself, and here that is a DNS lookup.
    """

    async def handle(route) -> None:
        url = route.request.url
        if url.startswith(PROFILE_URL):
            await route.fulfill(content_type="text/html", body=profile_html)
        elif url.startswith("https://portal.invalid/"):
            await route.fulfill(content_type="text/html", body=portal_html)
        else:
            await route.abort()

    await page.route("**/*", handle)


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


class TestAPortalIsNotReadAsTheProfile:
    async def test_a_redirect_after_the_document_committed(self, dom_page):
        """The reported case: the profile document leaves after 50ms.

        `goto` returns on the profile, so the navigation sees LinkedIn; the
        portal is what is there by the time the page is read.
        """
        await serve(
            dom_page,
            profile_html=profile(
                f"setTimeout(() => location.assign({PORTAL_URL!r}), 50);"
            ),
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
            profile_html=profile(
                "addEventListener('scroll', () => "
                f"location.assign({PORTAL_URL!r}), {{ once: true }});"
            ),
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
            profile_html=profile(f"location.replace({PORTAL_URL!r});"),
            portal_html=portal(
                title="LinkedIn Login",
                extra=(
                    '<div id="rememberme-div">'
                    "<button onclick=\"document.title = 'pressed'\">"
                    "Continue</button></div>"
                ),
            ),
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await read_profile(dom_page)

        assert dom_page.url == PORTAL_URL
        assert await dom_page.title() == "LinkedIn Login"
