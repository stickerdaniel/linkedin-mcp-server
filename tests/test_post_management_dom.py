"""Scheduled-post reads against a synthetic, fully intercepted browser page.

The fixture reuses one dialog while moving from an empty composer to a date
form and then a queue without a "Scheduled posts" heading. This exercises the
workflow's browser boundary and ownership checks; it does not establish that
every LinkedIn account has this markup or that every queue has been loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import post_management
from linkedin_mcp_server.linkedin.post_management import PostManagement
from linkedin_mcp_server.linkedin.session import PageSession


pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

BASE_URL = "https://www.linkedin.com/feed/"
POST_TEXT = "A synthetic queued post with café and 🎵."
POST_TIME = "Posting Monday, November 2 at 9:15 AM"
AVATAR_URL = (
    "https://media.licdn.com/dms/image/v2/SYNTHETIC/"
    "profile-displayphoto-scale_100_100/SYNTHETIC/avatar.webp"
)

DOCUMENT = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>LinkedIn</title></head>
<body><main><p>Synthetic feed.</p></main>
<button id="mount-composer" hidden onclick="openComposer()"></button>
<button id="read-delay" hidden onclick="onReadDelay()"></button>
<script>
  document.body.dataset.variant = 'one';
  document.body.dataset.stage = 'feed';
  const dismiss = `<button aria-label="Dismiss" onclick="dismissDialog(this)"><svg width="20" height="20"></svg></button>`;
  window.dismissDialog = button => {
    document.body.dataset.dismissals = String(Number(document.body.dataset.dismissals || 0) + 1);
    button.closest('[role="dialog"]').remove();
  };
  window.forbiddenAction = () => { document.body.dataset.forbiddenAction = 'true'; };
  window.openComposer = () => {
    document.body.dataset.stage = 'composer';
    const dialog = document.createElement('div');
    dialog.id = 'owned-composer';
    dialog.setAttribute('role', 'dialog');
    dialog.innerHTML = dismiss + `
      <div contenteditable="true" role="textbox"></div>
      <button onclick="showSchedule(this)">
        <svg id="clock-outline-medium" width="20" height="20"></svg>
      </button>
      <button onclick="forbiddenAction()">Post</button>`;
    if (['avatar-header', 'avatar-after-editor'].includes(document.body.dataset.variant)) {
      const image = document.createElement('img');
      image.src = 'https://media.licdn.com/dms/image/v2/SYNTHETIC/profile-displayphoto-scale_100_100/SYNTHETIC/avatar.webp';
      image.width = 32;
      image.height = 32;
      const editor = dialog.querySelector('[contenteditable="true"]');
      if (document.body.dataset.variant === 'avatar-header') editor.before(image);
      else editor.after(image);
    }
    document.body.appendChild(dialog);
  };
  window.showSchedule = button => {
    document.body.dataset.stage = 'schedule';
    document.body.dataset.clockClicks = String(Number(document.body.dataset.clockClicks || 0) + 1);
    const count = document.body.dataset.variant.startsWith('empty-queue') || document.body.dataset.variant === 'empty' ? 0 : ['partial','empty-text-post','posting-in-body'].includes(document.body.dataset.variant) ? 2 : 1;
    button.closest('[role="dialog"]').innerHTML = dismiss + `
      <h2>Schedule post</h2>
      <button onclick="showQueue(this)">Scheduled (${count})</button>
      <input type="date" value="2026-11-02" oninput="forbiddenAction()">
      <input type="time" value="09:15" oninput="forbiddenAction()">
      <button onclick="forbiddenAction()">Schedule</button>`;
  };
  window.showQueue = button => {
    document.body.dataset.stage = 'queue';
    document.body.dataset.listClicks = String(Number(document.body.dataset.listClicks || 0) + 1);
    const dialog = button.closest('[role="dialog"]');
    if (document.body.dataset.variant === 'heading-only') {
      dialog.innerHTML = dismiss + '<h2>Scheduled posts</h2>';
      return;
    }
    if (document.body.dataset.variant === 'empty' || document.body.dataset.variant === 'empty-queue-current') {
      const message = document.body.dataset.variant === 'empty-queue-current'
        ? 'When you schedule a post, it automatically posts at the date and time you chose'
        : 'No scheduled posts';
      dialog.innerHTML = dismiss + '<button>Scheduled (0)</button><p>' + message + '</p>';
      return;
    }
    const count = ['partial','empty-text-post','posting-in-body'].includes(document.body.dataset.variant) ? 2 : 1;
    dialog.innerHTML = dismiss + `
      <button>Scheduled (${count})</button>
      <a href="/sharing/compose">
      <p>Posting Monday, November 2 at 9:15 AM</p>
      <p>A synthetic queued post with café and 🎵.</p>
      </a>
      <button onclick="forbiddenAction()">Delete</button>
      <button onclick="forbiddenAction()">Reschedule</button>`;
    if (document.body.dataset.variant === 'empty-text-post') dialog.querySelector('a').insertAdjacentHTML('beforeend', '<p>No scheduled posts</p>');
    if (document.body.dataset.variant === 'posting-in-body') dialog.querySelector('a').insertAdjacentHTML('beforeend', '<p>Posting is part of the post body.</p>');
  };
  window.onReadDelay = () => {
    if (document.body.dataset.variant === 'preview-hydrates' && document.body.dataset.stage === 'queue') {
      document.body.dataset.readTicks = String(Number(document.body.dataset.readTicks || 0) + 1);
      if (document.body.dataset.readTicks === '2') document.querySelector('#owned-composer a').insertAdjacentHTML('beforeend', '<p>Late link preview.</p>');
    }
    if (['new-image', 'new-video'].includes(document.body.dataset.variant)
        && document.body.dataset.stage === 'schedule') {
      document.body.dataset.stage = 'operator';
      const media = document.createElement(document.body.dataset.variant === 'new-image' ? 'img' : 'video');
      media.id = 'operator-media';
      media.src = 'https://synthetic.invalid/operator-upload';
      media.width = 100;
      media.height = 100;
      document.getElementById('owned-composer').appendChild(media);
    }
    if (document.body.dataset.variant === 'replacement' && document.body.dataset.stage === 'schedule') {
      document.body.dataset.stage = 'operator';
      const replacement = document.createElement('div');
      replacement.id = 'operator-dialog';
      replacement.setAttribute('role', 'dialog');
      replacement.innerHTML = dismiss + '<p>Operator replacement dialog.</p>';
      document.getElementById('owned-composer').replaceWith(replacement);
    }
    if (document.body.dataset.variant === 'new-draft' && document.body.dataset.stage === 'queue') {
      document.body.dataset.stage = 'operator';
      const editor = document.createElement('div');
      editor.setAttribute('contenteditable', 'true');
      editor.setAttribute('role', 'textbox');
      editor.innerText = 'Unsaved text typed by the operator.';
      document.getElementById('owned-composer').appendChild(editor);
    }
  };
</script></body></html>
"""


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

            async def serve(route):
                if route.request.url == BASE_URL:
                    await route.fulfill(content_type="text/html", body=DOCUMENT)
                else:
                    await route.abort()

            await page.route("**/*", serve)
            await page.goto(BASE_URL)
            yield page
        finally:
            await browser.close()


@dataclass
class Workflow:
    reader: PostManagement
    navigator: Any
    elapsed: list[float]


@pytest.fixture
def workflow(dom_page, monkeypatch):
    elapsed = [0.0]

    async def delay(_session, seconds):
        elapsed[0] += seconds
        await dom_page.evaluate("document.getElementById('read-delay').click()")

    async def navigate(_url):
        await dom_page.evaluate("document.getElementById('mount-composer').click()")

    monkeypatch.setattr(PageSession, "delay", delay)
    monkeypatch.setattr(PageSession, "monotonic", lambda _session: elapsed[0])
    session = PageSession(dom_page)
    navigator = AsyncMock()
    navigator._navigate_to_page.side_effect = navigate
    reader = PostManagement(session, navigator, AsyncMock(), AsyncMock())
    return Workflow(reader, navigator, elapsed)


async def test_same_dialog_queue_without_heading_preserves_post_time_and_cleans_up(
    dom_page,
    workflow,
):
    result = await workflow.reader.get_scheduled_posts()

    text = result["sections"]["scheduled_posts"]
    assert POST_TEXT in text
    assert POST_TIME in text
    assert "section_errors" not in result
    workflow.navigator._navigate_to_page.assert_awaited_once()
    assert await dom_page.locator('[role="dialog"]').count() == 0
    assert await dom_page.evaluate("document.body.dataset.clockClicks") == "1"
    assert await dom_page.evaluate("document.body.dataset.listClicks") == "1"
    assert await dom_page.evaluate("document.body.dataset.dismissals") == "1"
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None


async def test_initial_operator_dialog_is_preserved_without_navigation(
    dom_page, workflow
):
    await dom_page.evaluate("""() => {
        const dialog = document.createElement('div');
        dialog.id = 'operator-dialog';
        dialog.setAttribute('role', 'dialog');
        dialog.innerHTML = '<div contenteditable="true">My existing draft.</div>';
        document.body.appendChild(dialog);
    }""")

    with pytest.raises(ValueError, match="existing_dialog"):
        await workflow.reader.get_scheduled_posts()

    workflow.navigator._navigate_to_page.assert_not_awaited()
    assert (
        await dom_page.locator("#operator-dialog").inner_text() == "My existing draft."
    )
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None


@pytest.mark.parametrize("variant", ["empty-text-post", "posting-in-body"])
async def test_post_content_cannot_claim_an_empty_or_complete_queue(
    dom_page, workflow, variant
):
    await dom_page.evaluate("value => document.body.dataset.variant = value", variant)
    result = await workflow.reader.get_scheduled_posts()
    assert POST_TEXT in result["sections"]["scheduled_posts"]
    assert result["section_errors"]["scheduled_posts"]["error_type"] == "partial_result"


async def test_a_hydrating_preview_does_not_duplicate_the_queued_post(
    dom_page, workflow
):
    await dom_page.evaluate("document.body.dataset.variant = 'preview-hydrates'")
    result = await workflow.reader.get_scheduled_posts()
    text = result["sections"]["scheduled_posts"]
    assert text.count(POST_TIME) == 1
    assert "Late link preview." in text


async def test_operator_replacement_dialog_is_not_closed_or_operated(
    dom_page, workflow
):
    await dom_page.evaluate("document.body.dataset.variant = 'replacement'")

    with pytest.raises(ValueError, match="dialog_replaced"):
        await workflow.reader.get_scheduled_posts()

    assert (
        await dom_page.locator("#operator-dialog p").inner_text()
        == "Operator replacement dialog."
    )
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None
    assert await dom_page.evaluate("document.body.dataset.listClicks") is None


async def test_text_typed_during_the_read_is_never_discarded(dom_page, workflow):
    await dom_page.evaluate("document.body.dataset.variant = 'new-draft'")

    with pytest.raises(ValueError, match="existing_draft"):
        await workflow.reader.get_scheduled_posts()

    assert await dom_page.locator('[contenteditable="true"]').inner_text() == (
        "Unsaved text typed by the operator."
    )
    assert await dom_page.locator("#owned-composer").count() == 1
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None


async def test_heading_without_loaded_entries_is_not_a_successful_empty_queue(
    dom_page,
    workflow,
):
    await dom_page.evaluate("document.body.dataset.variant = 'heading-only'")

    result = await workflow.reader.get_scheduled_posts()

    assert result["sections"] == {}
    assert (
        result["section_errors"]["scheduled_posts"]["error_type"] == "content_not_ready"
    )
    assert workflow.elapsed[0] < 17
    assert await dom_page.locator('[role="dialog"]').count() == 0


async def test_two_advertised_posts_with_one_loaded_entry_is_a_partial_result(
    dom_page,
    workflow,
):
    await dom_page.evaluate("document.body.dataset.variant = 'partial'")

    result = await workflow.reader.get_scheduled_posts()

    assert POST_TEXT in result["sections"]["scheduled_posts"]
    assert "Scheduled (2)" in result["sections"]["scheduled_posts"]
    assert result["section_errors"]["scheduled_posts"]["error_type"] == "partial_result"
    assert workflow.elapsed[0] < 17
    assert await dom_page.locator('[role="dialog"]').count() == 0


@pytest.mark.parametrize("variant", ["empty", "empty-queue-current"])
async def test_an_explicit_empty_queue_is_returned_as_visible_page_text(
    dom_page, workflow, variant
):
    await dom_page.evaluate("value => document.body.dataset.variant = value", variant)

    result = await workflow.reader.get_scheduled_posts()

    expected = (
        "When you schedule a post, it automatically posts at the date and time you chose"
        if variant == "empty-queue-current"
        else "No scheduled posts"
    )
    assert expected in result["sections"]["scheduled_posts"]
    assert "Scheduled (0)" in result["sections"]["scheduled_posts"]
    assert "section_errors" not in result
    assert await dom_page.locator('[role="dialog"]').count() == 0
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None


async def test_an_unsupported_locale_never_operates_the_schedule_controls(
    dom_page, workflow
):
    await dom_page.evaluate("document.documentElement.lang = 'de-DE'")

    with pytest.raises(ValueError, match="English composer"):
        await workflow.reader.get_scheduled_posts()

    assert await dom_page.evaluate("document.body.dataset.clockClicks") is None
    assert await dom_page.evaluate("document.body.dataset.listClicks") is None
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None
    assert await dom_page.locator('[contenteditable="true"]').inner_text() == ""


async def test_unlinked_header_avatar_does_not_block_reading_the_queue(
    dom_page, workflow
):
    await dom_page.evaluate("document.body.dataset.variant = 'avatar-header'")

    result = await workflow.reader.get_scheduled_posts()

    assert POST_TEXT in result["sections"]["scheduled_posts"]
    assert "section_errors" not in result
    assert await dom_page.locator('[role="dialog"]').count() == 0
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None


async def test_avatar_shaped_image_after_the_editor_is_preserved_as_an_attachment(
    dom_page,
    workflow,
):
    await dom_page.evaluate("document.body.dataset.variant = 'avatar-after-editor'")

    with pytest.raises(ValueError, match="existing_draft"):
        await workflow.reader.get_scheduled_posts()

    image = dom_page.locator("#owned-composer img")
    assert await image.get_attribute("src") == AVATAR_URL
    assert await dom_page.evaluate("document.body.dataset.clockClicks") is None
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None


@pytest.mark.parametrize("variant", ["new-image", "new-video"])
async def test_media_added_after_the_clock_is_never_discarded(
    dom_page, workflow, variant
):
    await dom_page.evaluate(
        "variant => { document.body.dataset.variant = variant; }", variant
    )

    with pytest.raises(ValueError, match="existing_draft"):
        await workflow.reader.get_scheduled_posts()

    assert await dom_page.locator("#operator-media").count() == 1
    assert await dom_page.evaluate("document.body.dataset.clockClicks") == "1"
    assert await dom_page.evaluate("document.body.dataset.listClicks") is None
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None


@pytest.mark.parametrize("variant", ["one", "new-draft"])
async def test_cleanup_failure_preserves_the_read_result_or_original_failure(
    dom_page,
    workflow,
    monkeypatch,
    variant,
):
    await dom_page.evaluate(
        "variant => { document.body.dataset.variant = variant; }", variant
    )
    run_on_linkedin = PageSession.run_on_linkedin

    async def failing_cleanup(session, script, *args):
        if script == post_management.CLOSE_EMPTY_DIALOG_JS:
            raise RuntimeError("Synthetic cleanup failure")
        return await run_on_linkedin(session, script, *args)

    monkeypatch.setattr(PageSession, "run_on_linkedin", failing_cleanup)

    if variant == "new-draft":
        with pytest.raises(ValueError, match="existing_draft"):
            await workflow.reader.get_scheduled_posts()
        assert await dom_page.locator('[contenteditable="true"]').inner_text() == (
            "Unsaved text typed by the operator."
        )
    else:
        result = await workflow.reader.get_scheduled_posts()
        assert POST_TEXT in result["sections"]["scheduled_posts"]
        assert POST_TIME in result["sections"]["scheduled_posts"]
    assert await dom_page.locator("#owned-composer").count() == 1


async def _mount_linked_image(dom_page, position, href):
    await dom_page.evaluate(
        """({position, href, src}) => {
            document.getElementById('mount-composer').click();
            const dialog = document.getElementById('owned-composer');
            dialog.__linkedinMcpScheduleRead = 'synthetic-owned-dialog';
            const editor = dialog.querySelector('[contenteditable="true"]');
            const anchor = document.createElement('a');
            anchor.href = href;
            const image = document.createElement('img');
            image.id = 'linked-draft-image';
            image.src = src;
            image.width = 32;
            image.height = 32;
            anchor.appendChild(image);
            if (position === 'inside') editor.appendChild(anchor);
            else if (position === 'after') editor.after(anchor);
            else editor.before(anchor);
            const scheduled = document.createElement('button');
            scheduled.textContent = 'Scheduled (1)';
            scheduled.onclick = () => { document.body.dataset.listClicks = '1'; };
            dialog.appendChild(scheduled);
        }""",
        {"position": position, "href": href, "src": AVATAR_URL},
    )


@pytest.mark.parametrize("href", ["/in/synthetic-author/", "/company/synthetic/"])
@pytest.mark.parametrize("position", ["inside", "after"])
@pytest.mark.parametrize("action", ["open_clock", "open_list"])
async def test_linked_images_in_or_after_the_editor_block_schedule_actions(
    dom_page, href, position, action
):
    await _mount_linked_image(dom_page, position, href)
    assert await dom_page.locator('[contenteditable="true"]').inner_text() == ""

    state = await dom_page.evaluate(
        post_management.SCHEDULE_UI_JS,
        {
            "action": action,
            "labels": post_management.SCHEDULE_TEXT_EN,
            "token": "synthetic-owned-dialog",
        },
    )

    assert state == {"ok": False, "reason": "existing_draft"}
    assert await dom_page.locator("#linked-draft-image").count() == 1
    assert await dom_page.evaluate("document.body.dataset.clockClicks") is None
    assert await dom_page.evaluate("document.body.dataset.listClicks") is None
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None


@pytest.mark.parametrize("href", ["/in/synthetic-author/", "/company/synthetic/"])
@pytest.mark.parametrize("position", ["inside", "after"])
async def test_linked_images_in_or_after_the_editor_block_empty_dialog_cleanup(
    dom_page, href, position
):
    await _mount_linked_image(dom_page, position, href)

    closed = await dom_page.evaluate(
        post_management.CLOSE_EMPTY_DIALOG_JS,
        {"labels": post_management.SCHEDULE_TEXT_EN, "token": "synthetic-owned-dialog"},
    )

    assert closed is False
    assert await dom_page.locator("#linked-draft-image").count() == 1
    assert await dom_page.evaluate("document.body.dataset.dismissals") is None


@pytest.mark.parametrize("href", ["/in/synthetic-author/", "/company/synthetic/"])
async def test_linked_header_avatar_before_the_editor_allows_the_schedule_clock(
    dom_page, href
):
    await _mount_linked_image(dom_page, "before", href)

    state = await dom_page.evaluate(
        post_management.SCHEDULE_UI_JS,
        {
            "action": "open_clock",
            "labels": post_management.SCHEDULE_TEXT_EN,
            "token": "synthetic-owned-dialog",
        },
    )

    assert state == {"ok": True}
    assert await dom_page.evaluate("document.body.dataset.clockClicks") == "1"
    assert await dom_page.evaluate("document.body.dataset.forbiddenAction") is None


@pytest.mark.parametrize(
    "tag, role",
    [
        ("img", None),
        ("video", None),
        ("audio", None),
        ("iframe", None),
        ("object", None),
        ("embed", None),
        ("canvas", None),
        ("table", None),
        ("fieldset", None),
        ("div", "radiogroup"),
        ("a", None),
    ],
)
async def test_media_only_inline_editor_is_preserved_before_navigation(
    dom_page, workflow, tag, role
):
    await dom_page.evaluate(
        """({tag, role}) => {
            const editor = document.createElement('div');
            editor.id = 'operator-inline-editor';
            editor.contentEditable = 'true';
            const attachment = document.createElement(tag);
            attachment.id = 'operator-inline-attachment';
            if (role) attachment.setAttribute('role', role);
            if (tag === 'a') attachment.setAttribute('download', 'synthetic');
            editor.appendChild(attachment);
            document.querySelector('main').appendChild(editor);
        }""",
        {"tag": tag, "role": role},
    )
    assert await dom_page.locator("#operator-inline-editor").inner_text() == ""

    with pytest.raises(ValueError, match="existing_dialog"):
        await workflow.reader.get_scheduled_posts()

    workflow.navigator._navigate_to_page.assert_not_awaited()
    assert await dom_page.locator("#operator-inline-attachment").count() == 1
    assert await dom_page.evaluate("document.body.dataset.clockClicks") is None


@pytest.mark.parametrize("tag", ["img", "video"])
async def test_rendered_post_media_outside_an_editor_does_not_block_navigation(
    dom_page, workflow, tag
):
    await dom_page.evaluate(
        """tag => {
            const media = document.createElement(tag);
            document.querySelector('main').appendChild(media);
            const editor = document.createElement('div');
            editor.contentEditable = 'true';
            document.querySelector('main').appendChild(editor);
        }""",
        tag,
    )

    result = await workflow.reader.get_scheduled_posts()

    assert POST_TEXT in result["sections"]["scheduled_posts"]
    assert "section_errors" not in result
    workflow.navigator._navigate_to_page.assert_awaited_once()
