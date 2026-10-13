"""Posting contracts with scripted pages: no live LinkedIn writes."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.linkedin.post_writer import PostWriter
from linkedin_mcp_server.tools.post_publishing import register_post_publishing_tools


AUTHOR = "https://www.linkedin.com/in/test-author/"
TEXT = "A post preview.\n\nWith a second paragraph."


@pytest.fixture(autouse=True)
def pinned_handles(monkeypatch):
    @asynccontextmanager
    async def checked(locator):
        yield locator

    monkeypatch.setattr(
        "linkedin_mcp_server.linkedin.post_writer.linkedin_element", checked
    )


def fixture():
    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/update/urn:li:activity:123/"
    html = MagicMock(get_attribute=AsyncMock(return_value="en-US"))
    editor = MagicMock(
        count=AsyncMock(return_value=1),
        inner_text=AsyncMock(side_effect=["", TEXT]),
        fill=AsyncMock(),
    )
    button = MagicMock(
        count=AsyncMock(return_value=1),
        is_enabled=AsyncMock(return_value=True),
        click=AsyncMock(),
    )
    audience = MagicMock(count=AsyncMock(return_value=1))
    authors = MagicMock(evaluate_all=AsyncMock(return_value=[AUTHOR]))
    dialog = MagicMock(count=AsyncMock(return_value=1))
    dialog.locator.side_effect = lambda selector: (
        authors if selector.startswith("a[") else editor
    )
    dialog.get_by_role.side_effect = lambda role, name, exact: (
        audience if name in {"Anyone", "Tout le monde"} else button
    )
    main = MagicMock(inner_text=AsyncMock(return_value=TEXT))
    main.evaluate_all = AsyncMock(return_value=[AUTHOR])
    main.count = AsyncMock(return_value=1)
    main.all_inner_texts = AsyncMock(return_value=[TEXT])
    main.locator.return_value = main
    page.locator.side_effect = lambda selector: (
        html if selector == "html" else main if selector.startswith("main") else dialog
    )
    page.wait_for_url = AsyncMock()
    session = MagicMock(page=page, check_rate_limit=AsyncMock())
    navigator = MagicMock(_navigate_to_page=AsyncMock())
    writer = PostWriter(session, navigator)
    return writer, page, session, navigator, editor, button, audience, dialog


@pytest.mark.parametrize("text", ["", " \n", "x" * 3001, "😀" * 1501])
def test_rejects_invalid_text_without_mutation(text):
    writer, page, *_ = fixture()
    with pytest.raises(ValueError):
        writer.prepare(text, AUTHOR)
    page.locator.assert_not_called()


async def test_prepare_and_default_confirmation_never_touch_composer():
    writer, page, _, nav, *_ = fixture()
    preview = writer.prepare(TEXT, AUTHOR)
    assert preview["text"] == TEXT and preview["published"] is False
    assert (await writer.publish(preview["draft_id"], TEXT))[
        "status"
    ] == "confirmation_required"
    nav._navigate_to_page.assert_not_awaited()
    page.locator.assert_not_called()


async def test_changed_text_and_expired_preview_cannot_publish():
    writer, _, _, nav, *_ = fixture()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(ValueError, match="differs"):
        await writer.publish(draft, TEXT + "!", confirmed=True)
    writer._state.drafts[draft].expires = 0
    with pytest.raises(ValueError, match="expired"):
        await writer.publish(draft, TEXT, confirmed=True)
    nav._navigate_to_page.assert_not_awaited()


async def test_fresh_facade_same_page_keeps_draft_and_clicks_once():
    writer, page, session, nav, _, button, *_ = fixture()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    other = PostWriter(session, nav)
    result = await other.publish(draft, TEXT, confirmed=True)
    assert result["status"] == "published" and result["url"] == page.url
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft, TEXT, confirmed=True)
    button.click.assert_awaited_once()


async def test_timeout_remains_unknown_and_blocks_second_attempt():
    writer, page, _, _, _, button, *_ = fixture()
    page.wait_for_url.side_effect = TimeoutError()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    assert (await writer.publish(draft, TEXT, confirmed=True))[
        "status"
    ] == "outcome_unknown"
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft, TEXT, confirmed=True)
    button.click.assert_awaited_once()


@pytest.mark.parametrize(
    "problem", ["locale", "audience", "author", "existing", "ambiguous"]
)
async def test_unsafe_composer_refused_before_fill_or_click(problem):
    writer, page, _, _, editor, button, audience, dialog = fixture()
    if problem == "locale":
        page.locator("html").get_attribute.return_value = "de"
    elif problem == "audience":
        audience.count.return_value = 0
    elif problem == "author":
        dialog.locator('a[href*="/in/"]').evaluate_all.return_value = [
            "https://www.linkedin.com/in/other/"
        ]
    elif problem == "existing":
        editor.inner_text.side_effect = None
        editor.inner_text.return_value = "User's unsaved draft"
    else:
        dialog.count.return_value = 2
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(ValueError):
        await writer.publish(draft, TEXT, confirmed=True)
    editor.fill.assert_not_awaited()
    button.click.assert_not_awaited()


async def test_concurrent_publish_cannot_double_click():
    writer, _, session, nav, _, button, *_ = fixture()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    results = await asyncio.gather(
        writer.publish(draft, TEXT, confirmed=True),
        PostWriter(session, nav).publish(draft, TEXT, confirmed=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    button.click.assert_awaited_once()


async def test_wrong_rendered_post_is_not_a_success():
    writer, page, *_ = fixture()
    page.locator("main").inner_text.return_value = "Some other post"
    page.locator("main article").all_inner_texts.return_value = ["Some other post"]
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    assert (await writer.publish(draft, TEXT, confirmed=True))[
        "status"
    ] == "outcome_unknown"


async def test_tool_default_does_not_even_acquire_browser(monkeypatch):
    ready = AsyncMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.post_publishing.get_ready_extractor", ready
    )
    server = FastMCP("posting-test")
    register_post_publishing_tools(server)
    tool = await server.get_tool("publish_post")
    assert tool.annotations.destructive_hint and not tool.annotations.idempotent_hint
    result = await tool.fn("unknown", TEXT, MagicMock())
    assert result["published"] is False
    ready.assert_not_awaited()


async def test_tool_surfaces_validation_error(monkeypatch):
    extractor = MagicMock(
        prepare_post=AsyncMock(side_effect=ValueError("Invalid post"))
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.post_publishing.get_ready_extractor",
        AsyncMock(return_value=extractor),
    )
    server = FastMCP("posting-test")
    register_post_publishing_tools(server)
    tool = await server.get_tool("prepare_post")
    with pytest.raises(ToolError, match="Invalid post"):
        await tool.fn(TEXT, MagicMock())


@pytest.mark.parametrize("problem", ["extra_text", "wrong_author"])
async def test_success_requires_exact_body_and_bound_author(problem):
    writer, page, *_ = fixture()
    article = page.locator("main article")
    if problem == "extra_text":
        article.all_inner_texts.return_value = [TEXT + " unexpected content"]
    else:
        article.evaluate_all.return_value = [
            "https://www.linkedin.com/in/someone-else/"
        ]
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    assert (await writer.publish(draft, TEXT, confirmed=True))[
        "status"
    ] == "outcome_unknown"
