"""Offline regression pages for review findings: no request leaves Chromium."""

import os
from unittest.mock import MagicMock

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin.post_writer import PostWriter
from linkedin_mcp_server.linkedin.session import PageSession


@pytest.mark.browser_dom
@pytest.mark.parametrize(
    "problem",
    [
        "author",
        "audience",
        "text",
        "replacement",
        "expiry",
        "new_attachment",
        "image",
        "hidden_image",
        "video",
        "poll",
        "document",
    ],
)
async def test_unapproved_composer_content_is_never_clicked(problem, monkeypatch):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.getenv("POST_TEST_BROWSER")
        )
        page = await browser.new_page()
        author = "https://www.linkedin.com/in/test-author/"
        changes = {
            "author": "document.querySelector('a').href='https://www.linkedin.com/in/other/'",
            "audience": "document.querySelector('#audience').textContent='Connections'",
            "text": "e.target.innerText='Unapproved replacement'",
            "replacement": "document.querySelector('dialog').outerHTML=document.querySelector('dialog').outerHTML",
            "expiry": "document.documentElement.dataset.expiryOffset=601000",
            "new_attachment": "document.querySelector('dialog').insertAdjacentHTML('beforeend','<video style=\"width:100px;height:100px\"></video>')",
        }
        attachments = {
            "hidden_image": '<img style="display:none" src="https://example.test/image.png">',
            "image": '<img style="width:100px;height:100px" src="https://example.test/image.png">',
            "video": '<video style="width:100px;height:100px"></video>',
            "poll": '<div role="radiogroup">Poll</div>',
            "document": '<a download href="https://example.test/report.pdf">Document</a>',
        }
        if problem == "expiry":
            # Advance only the guard's injected test clock: changing global
            # Date.now also expires the browser driver's own transport timers.
            from linkedin_mcp_server.linkedin.post_writer import COMPOSER_GUARD_JS

            monkeypatch.setattr(
                "linkedin_mcp_server.linkedin.post_writer.COMPOSER_GUARD_JS",
                COMPOSER_GUARD_JS.replace(
                    "Date.now()",
                    "(Date.now() + Number(document.documentElement.dataset.expiryOffset || 0))",
                ),
            )
        html = f'''<html lang="en"><dialog open><a href="{author}">Author</a>
        <button id="audience">Anyone</button><div role="textbox" contenteditable="true"></div>
        {attachments.get(problem, "")}<button onclick="document.documentElement.dataset.clicked=1">Post</button></dialog>
        <script>document.querySelector('[contenteditable]').addEventListener('input', e=>{{{changes.get(problem, "")}}}, {{once:true}})</script></html>'''
        await page.route(
            "**/*", lambda route: route.fulfill(body=html, content_type="text/html")
        )
        nav = MagicMock()
        nav._navigate_to_page = page.goto
        writer = PostWriter(PageSession(page), nav)
        draft = writer.prepare("Approved text", author)["draft_id"]
        if problem in attachments:
            with pytest.raises(ValueError, match="unsupported_attachment"):
                await writer.publish(draft, "Approved text", confirmed=True)
            assert await page.locator("[contenteditable]").inner_text() == ""
        else:
            result = await writer.publish(draft, "Approved text", confirmed=True)
            assert result["status"] == "not_submitted", result
            with pytest.raises(ValueError, match="Attempt already"):
                await writer.publish(draft, "Approved text", confirmed=True)
        assert await page.locator("html").get_attribute("data-clicked") is None
        await browser.close()
