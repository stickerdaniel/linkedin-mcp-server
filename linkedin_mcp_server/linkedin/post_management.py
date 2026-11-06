"""Read the signed-in member's posts, post analytics and publishing queue."""

from __future__ import annotations

import re
import secrets
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import anyio

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.capture import CapturePlan, SectionCapture
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_post_url
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession


COMPOSER_URL = "https://www.linkedin.com/feed/?shareActive=true"
logger = logging.getLogger(__name__)


class PostQueueReadRefused(ValueError):
    """A known UI/safety refusal whose explanation must reach MCP clients."""

    def __init__(self, reason: str):
        self.reason = reason
        self.operator_work = reason in {
            "existing_dialog",
            "existing_draft",
            "dialog_replaced",
        }
        if self.operator_work:
            message = "Finish or save the open LinkedIn interaction before reading the scheduled queue."
        elif reason == "unsupported_locale":
            message = "Scheduled-post reading currently supports LinkedIn's English composer only."
        else:
            message = "LinkedIn's scheduling interface could not be verified; inspect Scheduled posts in LinkedIn."
        super().__init__(f"{message} ({reason})")


# The scheduled list has no distinct URL. These English labels cover the
# measured composer variant; other languages/unknown variants are refused.
SCHEDULE_TEXT_EN = {
    "heading": "Scheduled posts",
    "schedule_heading": "Schedule post",
    "scheduled_pattern": r"^Scheduled \(([0-9]+)\)$",
    "posting_pattern": r"^Posting\b",
    "view_all": "View all scheduled posts",
    "dismiss": "Dismiss",
    "empty": [
        "You have no scheduled posts",
        "No scheduled posts",
        "When you schedule a post, it automatically posts at the date and time you chose",
    ],
}

_AVATAR_IMAGE_JS = r"""(image, editor) => {
    if (!editor || editor.contains(image)
        || !(image.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
    if (image.closest('a[href*="/in/"],a[href*="/company/"]')) return true;
    try {
        const url = new URL(image.src);
        return url.hostname === 'media.licdn.com'
            && /\/profile-displayphoto(?:-|\/)/.test(url.pathname);
    } catch { return false; }
}"""

PREFLIGHT_JS = """() => {
    const visible = e => e.getClientRects().length > 0;
    // Rich-text attachments can be the entire draft while innerText is empty.
    const hasDraft = e => e.innerText.trim() || e.querySelector(
        'img,video,audio,iframe,object,embed,canvas,fieldset,table,'
        + '[role="radiogroup"],[role="radio"],a[download]'
    );
    return [...document.querySelectorAll('dialog[open],[role="dialog"]')].some(visible)
        || [...document.querySelectorAll('[contenteditable="true"]')].some(e => visible(e) && hasDraft(e))
        || [...document.querySelectorAll('input[type="file"]')].some(e => e.files.length)
        ? {ok: false, reason: 'existing_dialog'} : {ok: true};
}"""

# innerText cannot operate a clock or identify which region scrolls. The icon
# name (not a layout class) identifies the clock in both observed renderers;
# the remaining actions are bounded by one dialog and explicit English labels.
# No write action, date field or queued-post menu is ever clicked here.
SCHEDULE_UI_JS = r"""({action, labels, token}) => {
    const avatar = AVATAR_IMAGE_FUNCTION;
    const visible = e => !!e && e.isConnected && e.getClientRects().length > 0;
    const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return {ok: false, reason: 'ambiguous_dialog'};
    const dialog = dialogs[0];
    if (action !== 'open_clock' && dialog.__linkedinMcpScheduleRead !== token)
        return {ok: false, reason: 'dialog_replaced'};
    const editors = [...dialog.querySelectorAll('[contenteditable="true"]')];
    const attachments = () => dialog.querySelector('video,audio,iframe,object,embed,canvas,fieldset,[role="radiogroup"],[role="radio"],a[download]')
        || [...dialog.querySelectorAll('img')].some(e => !avatar(e, editors[0]));
    if (editors.some(e => (e.innerText || '').trim())
        || [...dialog.querySelectorAll('input[type="file"]')].some(e => e.files.length)
        || ((editors.length || action === 'open_list') && attachments()))
        return {ok: false, reason: 'existing_draft'};
    const controls = [...dialog.querySelectorAll('a,button,[role="button"]')].filter(visible);
    const name = e => (e.getAttribute('aria-label') || e.innerText || '').trim();
    const clickOne = targets => {
        if (targets.length === 0) return {ok: false, reason: 'not_ready'};
        if (targets.length !== 1) return {ok: false, reason: 'ambiguous_action'};
        const target = targets[0];
        if (target.disabled || target.getAttribute('aria-disabled') === 'true')
            return {ok: false, reason: 'disabled_action'};
        target.click();
        return {ok: true};
    };
    if (action === 'open_clock') {
        if (editors.length === 0) return {ok: false, reason: 'not_ready'};
        if (editors.length !== 1) return {ok: false, reason: 'ambiguous_editor'};
        dialog.__linkedinMcpScheduleRead = token;
        return clickOne(controls.filter(e => e.querySelector(
            'svg[id="clock-outline-medium"],svg[data-test-icon="clock-medium"],li-icon[type="clock"]'
        )));
    }
    if (action === 'open_list') {
        return clickOne(controls.filter(e =>
            new RegExp(labels.scheduled_pattern).test(name(e)) || name(e) === labels.view_all
        ));
    }
    const text = (dialog.innerText || '').trim();
    const headings = [...dialog.querySelectorAll('h1,h2,h3,[role="heading"]')].filter(visible);
    // Entry links are needed for progress: post bodies can themselves contain
    // "Posting" or an empty-state sentence. Count the outer rendered queue
    // cards that navigate to the composer, never those words in their bodies.
    const cardLinks = controls.filter(e => {
        if (e.tagName !== 'A' || !new RegExp(labels.posting_pattern).test(name(e))) return false;
        try { return new URL(e.href).pathname.replace(/\/$/, '') === '/sharing/compose'; }
        catch { return false; }
    });
    const cards = cardLinks.filter(e => !cardLinks.some(other => other !== e && other.contains(e)));
    const countLine = controls.filter(e => !cards.some(card => card.contains(e)))
        .map(e => new RegExp(labels.scheduled_pattern).exec(name(e))).find(Boolean);
    const isList = headings.some(e => e.innerText.trim() === labels.heading)
        || text.split('\n').some(line => line.trim() === labels.heading)
        || (countLine && !dialog.querySelector('input,[contenteditable="true"]'));
    if (!isList) return {ok: true, ready: false};
    const expected = countLine ? Number(countLine[1]) : null;
    // A heading is not a loaded empty list. Entries need a rendered posting
    // line, or an explicit empty-state sentence, before any success is returned.
    const entries = cards.length;
    const empty = expected === 0 && entries === 0
        && labels.empty.some(value => text.split('\n').some(line => line.trim() === value));
    const scrollables = [dialog, ...dialog.querySelectorAll('*')].filter(e => {
        const overflow = getComputedStyle(e).overflowY;
        return visible(e) && /auto|scroll/.test(overflow) && e.scrollHeight > e.clientHeight + 2;
    });
    const atEnd = scrollables.every(e => e.scrollTop + e.clientHeight >= e.scrollHeight - 2);
    if (action === 'scroll') {
        scrollables.forEach(e => e.scrollTop += Math.max(200, e.clientHeight));
    }
    return {ok: true, text, ready: empty || entries > 0, empty, entries,
        expected, at_end: atEnd};
}""".replace("AVATAR_IMAGE_FUNCTION", _AVATAR_IMAGE_JS)

# Only dismiss our read workflow while the composer is still empty. A user
# typing during the read keeps their dialog; no discard confirmation is used.
CLOSE_EMPTY_DIALOG_JS = r"""({token, labels}) => {
    const avatar = AVATAR_IMAGE_FUNCTION;
    const visible = e => !!e && e.getClientRects().length > 0;
    const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1) return false;
    const dialog = dialogs[0];
    if (dialog.__linkedinMcpScheduleRead !== token) return false;
    const editors = [...dialog.querySelectorAll('[contenteditable="true"]')];
    if (editors.some(e => e.innerText.trim())
        || [...dialog.querySelectorAll('input[type="file"]')].some(e => e.files.length)
        || ((editors.length || dialog.querySelector('input:not([type="file"])')) && (dialog.querySelector('video,audio,iframe,object,embed,canvas,fieldset,[role="radiogroup"],[role="radio"],a[download]')
            || [...dialog.querySelectorAll('img')].some(e => !avatar(e, editors[0]))))) return false;
    const lines = dialog.innerText.split('\n').map(line => line.trim());
    const known = (editors.length === 1 && dialog.querySelector('svg[id="clock-outline-medium"],svg[data-test-icon="clock-medium"],li-icon[type="clock"]'))
        || lines.includes(labels.heading)
        || (!dialog.querySelector('input,[contenteditable="true"]') && lines.some(line => new RegExp(labels.scheduled_pattern).test(line)))
        || (lines.includes(labels.schedule_heading) && lines.some(line => new RegExp(labels.scheduled_pattern).test(line)));
    if (!known) return false;
    const close = [...dialog.querySelectorAll('button')].filter(e => visible(e)
        && e.getAttribute('aria-label') === labels.dismiss);
    if (close.length !== 1) return false;
    close[0].click();
    return true;
}""".replace("AVATAR_IMAGE_FUNCTION", _AVATAR_IMAGE_JS)


def post_analytics_url(post_url: str) -> str:
    """Build only the activity analytics route observed on the signed-in page."""
    normalized = normalize_post_url(post_url)
    path = urlsplit(normalized).path
    match = re.search(r"/feed/update/(urn:li:activity:[0-9]+)/?$", path)
    if match:
        urn = match.group(1)
    else:
        matches = re.findall(r"(?=-(activity|share|ugcPost)-([0-9]+)-)", path)
        if len(matches) != 1 or matches[0][0] != "activity":
            raise InvalidReferenceError(
                "Post analytics requires an activity permalink or activity URN."
            )
        urn = f"urn:li:activity:{matches[0][1]}"
    return f"https://www.linkedin.com/analytics/post-summary/{urn}/"


class PostManagement:
    """Own reads of personal activity, one analytics page and the scheduled modal."""

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        capture: SectionCapture,
        read_profile: Callable[..., Awaitable[dict[str, Any]]],
    ):
        self._session = session
        self._navigator = navigator
        self._capture = capture
        self._read_profile = read_profile

    async def get_my_posts(self, max_scrolls: int = 5) -> dict[str, Any]:
        """Read the member's activity through the existing own-profile resolver."""
        return await self._read_profile({"posts"}, max_scrolls=max_scrolls)

    async def get_post_analytics(self, post_url: str) -> dict[str, Any]:
        """Return LinkedIn's visible analytics, preserving missing/suppressed values."""
        url = post_analytics_url(post_url)
        section = "post_analytics"
        extracted = await self._capture.capture(
            url, section, CapturePlan(max_scrolls=1)
        )
        result: dict[str, Any] = {"url": url, "sections": {}}
        error = extracted.error
        if extracted.text == RATE_LIMITED_SECTION_TEXT:
            error = rate_limited_section_error()
        elif extracted.text:
            result["sections"][section] = extracted.text
            if extracted.references:
                result["references"] = {section: extracted.references}
        elif not error:
            error = {
                "error_type": "empty_content",
                "error_message": "No post analytics were rendered; availability and access could not be verified.",
            }
        if error:
            result["section_errors"] = {section: error}
        return result

    async def get_scheduled_posts(self, max_scrolls: int = 3) -> dict[str, Any]:
        """Read the bounded queue without filling or confirming the schedule form."""
        preflight = await self._session.run_on_linkedin(PREFLIGHT_JS)
        if not preflight["ok"]:
            raise PostQueueReadRefused(preflight["reason"])
        await self._navigator._navigate_to_page(COMPOSER_URL)
        opened = False
        token = secrets.token_urlsafe(18)
        try:
            opened = True
            await self._session.check_rate_limit()
            locale = (
                await self._session.page.locator("html").get_attribute("lang") or ""
            ).split("-")[0]
            if locale != "en":
                raise PostQueueReadRefused("unsupported_locale")
            await self._session.page.locator(
                'dialog[open]:visible, [role="dialog"]:visible'
            ).wait_for(state="visible", timeout=10000)
            await self._step("open_clock", token)
            await self._session.delay(0.3)
            await self._step("open_list", token)
            rendered_text = ""
            last_text = ""
            stable_end = 0
            complete = False
            budget = self._session.monotonic() + 15
            scrolls = 0
            while self._session.monotonic() < budget:
                await self._session.delay(0.5)
                await self._session.check_rate_limit()
                state = await self._step("read", token)
                if not state["ready"]:
                    continue
                text = state["text"]
                # Concatenating snapshots duplicates posts when their previews
                # hydrate. A virtualized list that never renders every entry
                # stays partial instead of becoming an accumulated export.
                rendered_text = text
                if state["empty"]:
                    complete = True
                    break
                stable_end = (
                    stable_end + 1 if state["at_end"] and text == last_text else 0
                )
                if (
                    stable_end >= 2
                    and state["expected"] is not None
                    and state["entries"] >= state["expected"]
                ):
                    complete = True
                    break
                last_text = text
                if not state["at_end"]:
                    if scrolls >= max_scrolls:
                        break
                    await self._step("scroll", token)
                    scrolls += 1
            result: dict[str, Any] = {"url": COMPOSER_URL, "sections": {}}
            if rendered_text:
                result["sections"]["scheduled_posts"] = rendered_text
            if not complete:
                result["section_errors"] = {
                    "scheduled_posts": {
                        "error_type": "partial_result"
                        if rendered_text
                        else "content_not_ready",
                        "error_message": "Only loaded scheduled posts were read; the list may be incomplete."
                        if rendered_text
                        else "The scheduled list did not render entries or an explicit empty state within the read budget.",
                    }
                }
            return result
        finally:
            if opened:
                # Bounded cleanup also runs when the tool timeout cancels this
                # task, without swallowing that cancellation or discarding text.
                with anyio.move_on_after(2, shield=True):
                    try:
                        await self._session.run_on_linkedin(
                            CLOSE_EMPTY_DIALOG_JS,
                            {"token": token, "labels": SCHEDULE_TEXT_EN},
                        )
                    except Exception:
                        logger.warning(
                            "Could not close the scheduled-post dialog", exc_info=True
                        )

    async def _step(self, action: str, token: str) -> dict[str, Any]:
        deadline = self._session.monotonic() + 5
        while True:
            state = await self._session.run_on_linkedin(
                SCHEDULE_UI_JS,
                {"action": action, "labels": SCHEDULE_TEXT_EN, "token": token},
            )
            if state["ok"] or state.get("reason") != "not_ready":
                break
            if self._session.monotonic() >= deadline:
                break
            await self._session.delay(0.2)
            await self._session.check_rate_limit()
        if not state["ok"]:
            raise PostQueueReadRefused(state["reason"])
        return state
