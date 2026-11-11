"""Posting contracts with scripted pages: no live LinkedIn writes."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
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
        evaluate=AsyncMock(return_value={"ok": True}),
    )
    audience = MagicMock(count=AsyncMock(return_value=1))
    authors = MagicMock(evaluate_all=AsyncMock(return_value=[AUTHOR]))
    organizations = MagicMock(count=AsyncMock(return_value=0))
    dialog = MagicMock(count=AsyncMock(return_value=1))
    for locator in [dialog, editor, button, audience]:
        locator.wait_for = AsyncMock()
    dialog.locator.side_effect = lambda selector: (
        organizations
        if "/company/" in selector
        else authors
        if selector.startswith("a[")
        else editor
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
    session = MagicMock(
        page=page,
        check_rate_limit=AsyncMock(),
        run_on_linkedin=AsyncMock(return_value={"blocked": False}),
    )
    navigator = MagicMock(_navigate_to_page=AsyncMock())
    writer = PostWriter(session, navigator)
    return writer, page, session, navigator, editor, button, audience, dialog


@pytest.mark.parametrize("text", ["", " \n", "x" * 3001, "😀" * 1501])
def test_rejects_invalid_text_without_mutation(text):
    writer, page, *_ = fixture()
    with pytest.raises(ValueError):
        writer.prepare(text, AUTHOR)
    page.locator.assert_not_called()


async def test_prepare_post_binds_author_without_opening_a_composer():
    _, page, session, navigator, *_ = fixture()
    resolve_author = AsyncMock(return_value=AUTHOR)
    writer = PostWriter(session, navigator, resolve_author=resolve_author)
    result = await writer.prepare_post(TEXT)
    assert result["text"] == TEXT
    assert result["author_url"] == AUTHOR
    assert result["audience"] == "anyone"
    assert result["published"] is False
    resolve_author.assert_awaited_once_with()
    navigator._navigate_to_page.assert_not_awaited()
    page.locator.assert_not_called()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("First\r\nSecond", "First\nSecond"),
        ("First\rSecond", "First\nSecond"),
        ("First\r\n\r\nSecond\nThird", "First\n\nSecond\nThird"),
        ("x" * 2998 + "\r\ny", "x" * 2998 + "\ny"),
    ],
)
async def test_preview_uses_publishable_line_breaks_before_approval(raw, expected):
    _, page, session, navigator, *_ = fixture()
    writer = PostWriter(
        session, navigator, resolve_author=AsyncMock(return_value=AUTHOR)
    )

    preview = await writer.prepare_post(raw)

    assert preview["text"] == expected
    with pytest.raises(ValueError, match="differs from the exact preview"):
        await writer.publish(preview["draft_id"], raw, confirmed=True)
    navigator._navigate_to_page.assert_not_awaited()


async def test_prepare_post_refuses_invalid_text_before_resolving_author():
    _, _, session, navigator, *_ = fixture()
    resolve_author = AsyncMock(return_value=AUTHOR)
    writer = PostWriter(session, navigator, resolve_author=resolve_author)
    with pytest.raises(ValueError, match="1–3000"):
        await writer.prepare_post("  ")
    resolve_author.assert_not_awaited()


async def test_preparing_preview_cannot_discard_existing_operator_work():
    _, _, session, navigator, *_ = fixture()
    session.run_on_linkedin.return_value = {"blocked": True, "reason": "visible_dialog"}
    resolve_author = AsyncMock(return_value=AUTHOR)
    writer = PostWriter(session, navigator, resolve_author=resolve_author)
    with pytest.raises(ValueError, match="Existing browser work"):
        await writer.prepare_post(TEXT)
    resolve_author.assert_not_awaited()
    navigator._navigate_to_page.assert_not_awaited()


async def test_unlinked_author_composer_is_unsupported_before_any_mutation():
    writer, _, _, _, editor, button, _, dialog = fixture()
    dialog.locator('a[href*="/in/"]').evaluate_all.return_value = []
    draft_id = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(ValueError, match="Unsupported composer.*author"):
        await writer.publish(draft_id, TEXT, confirmed=True)
    assert not writer._state.drafts[draft_id].attempted
    editor.fill.assert_not_awaited()
    button.evaluate.assert_not_awaited()


async def test_organization_actor_link_cannot_hide_behind_a_personal_profile_link():
    writer, _, _, _, editor, button, _, dialog = fixture()
    dialog.locator(
        'a[href*="/company/"],a[href*="/school/"],a[href*="/showcase/"]'
    ).count.return_value = 1
    draft_id = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(ValueError, match="organization"):
        await writer.publish(draft_id, TEXT, confirmed=True)
    assert not writer._state.drafts[draft_id].attempted
    editor.fill.assert_not_awaited()
    button.evaluate.assert_not_awaited()


@pytest.mark.parametrize("reason", ["visible_dialog", "unsaved_text", "selected_files"])
async def test_existing_browser_work_is_preserved_before_navigation(reason):
    writer, _, session, navigator, editor, button, *_ = fixture()
    session.run_on_linkedin.return_value = {"blocked": True, "reason": reason}
    draft_id = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(ValueError, match="Existing browser work"):
        await writer.publish(draft_id, TEXT, confirmed=True)
    navigator._navigate_to_page.assert_not_awaited()
    editor.fill.assert_not_awaited()
    button.evaluate.assert_not_awaited()
    assert not writer._state.drafts[draft_id].attempted


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
    assert sum(call.args[1]["click"] for call in button.evaluate.await_args_list) == 1


async def test_timeout_remains_unknown_and_blocks_second_attempt():
    writer, page, _, _, _, button, *_ = fixture()
    page.wait_for_url.side_effect = TimeoutError()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    assert (await writer.publish(draft, TEXT, confirmed=True))[
        "status"
    ] == "outcome_unknown"
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft, TEXT, confirmed=True)
    assert sum(call.args[1]["click"] for call in button.evaluate.await_args_list) == 1


async def test_expiry_cannot_turn_an_attempted_preview_into_retry_advice():
    writer, page, *_ = fixture()
    page.wait_for_url.side_effect = TimeoutError()
    draft_id = writer.prepare(TEXT, AUTHOR)["draft_id"]
    result = await writer.publish(draft_id, TEXT, confirmed=True)
    assert result["status"] == "outcome_unknown"
    writer._state.drafts[draft_id].expires = 0
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft_id, TEXT, confirmed=True)


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
    button.evaluate.assert_not_awaited()


async def test_concurrent_publish_cannot_double_click():
    writer, _, session, nav, _, button, *_ = fixture()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    results = await asyncio.gather(
        writer.publish(draft, TEXT, confirmed=True),
        PostWriter(session, nav).publish(draft, TEXT, confirmed=True),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(call.args[1]["click"] for call in button.evaluate.await_args_list) == 1


async def test_wrong_rendered_post_is_not_a_success():
    writer, page, *_ = fixture()
    page.locator("main").inner_text.return_value = "Some other post"
    page.locator("main article").all_inner_texts.return_value = ["Some other post"]
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    assert (await writer.publish(draft, TEXT, confirmed=True))[
        "status"
    ] == "outcome_unknown"


@pytest.mark.parametrize("seam", ["navigation", "before_fill", "after_fill"])
async def test_expiry_rechecked_at_mutation_boundaries(seam):
    writer, _, _, nav, editor, button, *_ = fixture()
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]

    def expire(*args):
        writer._state.drafts[draft].expires = 0
        return {"ok": True}

    if seam == "navigation":
        nav._navigate_to_page.side_effect = expire
    elif seam == "before_fill":
        button.evaluate.side_effect = expire
    else:

        def expire_after_insertion(_script, arguments):
            if arguments.get("write"):
                return expire()
            return {"ok": True}

        button.evaluate.side_effect = expire_after_insertion
    if seam == "after_fill":
        result = await writer.publish(draft, TEXT, confirmed=True)
        assert (
            result["status"] == "not_submitted"
            and result["reason"] == "preview_expired"
        )
        assert not any(
            call.args[1]["click"] for call in button.evaluate.await_args_list
        )
    else:
        with pytest.raises(ValueError, match="expired"):
            await writer.publish(draft, TEXT, confirmed=True)
        editor.fill.assert_not_awaited()


async def test_tool_default_does_not_even_acquire_browser(monkeypatch):
    ready = AsyncMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.post_publishing.get_ready_extractor", ready
    )
    server = FastMCP("posting-test")
    register_post_publishing_tools(server)
    tool = await server.get_tool("publish_post")
    assert isinstance(tool, FunctionTool)
    assert tool.annotations is not None
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
    assert isinstance(tool, FunctionTool)
    with pytest.raises(ToolError, match="Invalid post"):
        await tool.fn(TEXT, MagicMock())


@pytest.mark.parametrize(
    "text",
    ["", " \n", "x" * 3001, "😀" * 1501, "bad\x00text", "bad\ud800text"],
    ids=["empty", "blank", "too-long", "too-many-utf16-units", "control", "surrogate"],
)
async def test_invalid_preview_text_is_refused_before_acquiring_browser(
    monkeypatch, text
):
    ready = AsyncMock()
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.post_publishing.get_ready_extractor", ready
    )
    server = FastMCP("posting-test")
    register_post_publishing_tools(server)
    tool = await server.get_tool("prepare_post")
    assert isinstance(tool, FunctionTool)
    with pytest.raises(CodedToolError) as error:
        await tool.fn(text, MagicMock())
    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    ready.assert_not_awaited()


async def test_cancellation_during_insertion_consumes_preview_before_any_retry():
    writer, _, _, _, editor, button, *_ = fixture()

    def cancel_insertion(_script, arguments):
        if arguments.get("write"):
            raise asyncio.CancelledError()
        return {"ok": True}

    button.evaluate.side_effect = cancel_insertion
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    with pytest.raises(asyncio.CancelledError):
        await writer.publish(draft, TEXT, confirmed=True)
    with pytest.raises(ValueError, match="Attempt already"):
        await writer.publish(draft, TEXT, confirmed=True)
    editor.fill.assert_not_awaited()
    assert not any(call.args[1]["click"] for call in button.evaluate.await_args_list)


async def test_unknown_publication_never_reports_a_confirmed_nonpublication():
    writer, page, *_ = fixture()
    page.locator("main article").count.return_value = 0
    draft = writer.prepare(TEXT, AUTHOR)["draft_id"]
    result = await writer.publish(draft, TEXT, confirmed=True)
    assert result["status"] == "outcome_unknown"
    assert result["published"] is None
    assert result["retry_allowed"] is False


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
