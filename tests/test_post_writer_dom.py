"""Synthetic composer DOM contracts; every browser request is intercepted."""

import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.post_writer import PostWriter
from linkedin_mcp_server.linkedin.session import PageSession


@pytest.mark.browser_dom
@pytest.mark.parametrize(
    "locale, action, audience, delayed",
    [
        ("en", "Post", "Anyone", False),
        ("fr", "Publier", "Tout le monde", False),
        ("en", "Post", "Anyone", True),
    ],
)
async def test_real_composer_pinned_actions_and_verified_post(
    locale, action, audience, delayed
):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.getenv("POST_TEST_BROWSER")
        )
        page = await browser.new_page()
        text = "An approved post"
        author = "https://www.linkedin.com/in/test-author/"

        async def route(request):
            if "/feed/update/" in request.request.url:
                html = f'<main><article><header><a href="{author}">Author</a></header><p>{text}</p></article></main>'
            else:
                html = f'''<html lang="{locale}"><dialog open>
                <a href="{author}">Author</a><button>{audience}</button>
                <div role="textbox" contenteditable="true"></div>
                <button onclick="location.href='/feed/update/urn:li:activity:123/'">{action}</button>
                </dialog></html>'''
            if delayed and "/feed/update/" not in request.request.url:
                import json

                dialog_html = html.split("<dialog", 1)[1].split("</dialog>", 1)[0]
                html = f'<html lang="{locale}"><body><script>setTimeout(() => document.body.insertAdjacentHTML("beforeend", {json.dumps("<dialog" + dialog_html + "</dialog>")}), 150)</script></body></html>'
            await request.fulfill(body=html, content_type="text/html")

        await page.route("**/*", route)
        nav = MagicMock()
        nav._navigate_to_page = page.goto
        session = PageSession(page)
        writer = PostWriter(session, nav)
        draft = writer.prepare(text, author)["draft_id"]
        result = await writer.publish(draft, text, confirmed=True)
        assert result["status"] == "published"
        assert (await page.locator("main").inner_text()).endswith(text)
        await browser.close()


@pytest.mark.browser_dom
async def test_off_domain_at_mutation_seam_cannot_receive_post(monkeypatch):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.getenv("POST_TEST_BROWSER")
        )
        page = await browser.new_page()
        author = "https://www.linkedin.com/in/test-author/"
        html = f'''<html lang="en"><dialog open><a href="{author}">Author</a>
        <button>Anyone</button><div role="textbox" contenteditable="true"></div>
        <button onclick="document.documentElement.dataset.clicked=1">Post</button></dialog></html>'''
        await page.route(
            "**/*", lambda route: route.fulfill(body=html, content_type="text/html")
        )
        from linkedin_mcp_server.core.destination import linkedin_element

        @asynccontextmanager
        async def redirect_then_check(locator):
            await page.goto("https://example.test/")
            async with linkedin_element(locator) as handle:
                yield handle

        monkeypatch.setattr(
            "linkedin_mcp_server.linkedin.post_writer.linkedin_element",
            redirect_then_check,
        )
        nav = MagicMock()
        nav._navigate_to_page = page.goto
        writer = PostWriter(PageSession(page), nav)
        draft = writer.prepare("Must never be typed", author)["draft_id"]
        result = await writer.publish(draft, "Must never be typed", confirmed=True)
        assert result["status"] == "outcome_unknown"
        assert await page.locator("div").inner_text() == ""
        assert await page.locator("html").get_attribute("data-clicked") is None
        await browser.close()
