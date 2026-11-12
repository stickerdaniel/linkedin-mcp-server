"""Read a synthetic post with the real capture and browser boundaries.

All requests are intercepted. These fixtures establish that visible post text,
counters and comments survive extraction; they make no claim about LinkedIn's
current markup, hidden comments or complete engagement statistics.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_reader import PostReader
from linkedin_mcp_server.linkedin.session import PageSession


pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1234567890123/"


@pytest.fixture
async def dom_page(monkeypatch):
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


async def test_post_text_visible_engagement_and_comments_are_returned(dom_page):
    html = """<!DOCTYPE html>
    <html lang="en"><head><meta charset="utf-8"><title>LinkedIn</title></head>
    <body>
      <aside>Unrelated sidebar text</aside>
      <main>
        <article>
          <a href="/in/synthetic-author/">Synthetic Author</a>
          <p>A synthetic post about a small open-source project.</p>
          <p>18 reactions</p><p>3 comments</p><p>2 reposts</p>
          <button onclick="document.body.dataset.acted = 'true'">Like</button>
          <button onclick="document.body.dataset.acted = 'true'">Repost</button>
          <section><a href="/in/synthetic-commenter/">Synthetic Commenter</a>
            <p>This is a rendered comment.</p>
          </section>
        </article>
      </main>
    </body></html>"""

    async def serve(route):
        if route.request.url == POST_URL:
            await route.fulfill(content_type="text/html", body=html)
        else:
            await route.abort()

    await dom_page.route("**/*", serve)
    session = PageSession(dom_page)
    reader = PostReader(
        SectionCapture(session, PageNavigator(session), PageContentReader(session))
    )

    result = await reader.get_post(POST_URL, max_scrolls=1)

    text = result["sections"]["post"]
    for visible in [
        "A synthetic post about a small open-source project.",
        "18 reactions",
        "3 comments",
        "2 reposts",
        "This is a rendered comment.",
    ]:
        assert visible in text
    assert "Unrelated sidebar text" not in text
    assert {"/in/synthetic-author/", "/in/synthetic-commenter/"} <= {
        reference["url"] for reference in result["references"]["post"]
    }
    assert await dom_page.evaluate("document.body.dataset.acted") is None
