"""Preview and publish plain-text personal posts through the rendered composer."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit
from weakref import WeakKeyDictionary
from linkedin_mcp_server.core.destination import linkedin_element

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession


# Composer action text is the only available signal on some LinkedIn variants.
# Unsupported locales/variants fail closed; these tables do not imply live coverage.
LABELS = {
    "en": {"publish": "Post", "audience": "Anyone"},
    "fr": {"publish": "Publier", "audience": "Tout le monde"},
}


# Run the final checks and click in ONE document evaluation: an awaited locator
# check followed by click permits a different composer/audience to slip between.
# Semantic media controls and selected files must be inspected because an empty
# rich-text box does not prove that the post is plain text. Unsupported markup
# fails closed; images within the personal author's link are avatar chrome only.
COMPOSER_GUARD_JS = r"""(button, args) => {
    const {dialog, editor, author, audience, action, text, deadline, click} = args;
    const visible = el => !!el && el.isConnected && el.getClientRects().length > 0;
    const all = selector => [...dialog.querySelectorAll(selector)].filter(visible);
    if (!visible(dialog) || !visible(editor) || !visible(button)
        || button.closest('dialog,[role="dialog"]') !== dialog
        || editor.closest('dialog,[role="dialog"]') !== dialog)
        return {ok: false, reason: 'composer_replaced'};
    const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"]')].filter(visible);
    if (dialogs.length !== 1 || dialogs[0] !== dialog
        || (button.getAttribute('aria-label') || button.innerText).trim() !== action)
        return {ok: false, reason: 'composer_changed'};
    const authors = all('a[href*="/in/"]');
    if (!authors.length || authors.some(el => {
        const url = new URL(el.href);
        return url.origin !== 'https://www.linkedin.com' || url.search || url.hash
            || url.origin + url.pathname.replace(/\/$/, '') + '/' !== author;
    })) return {ok: false, reason: 'author_changed'};
    const audiences = all('button,[role="button"]').filter(el =>
        (el.getAttribute('aria-label') || el.innerText).trim() === audience);
    if (audiences.length !== 1) return {ok: false, reason: 'audience_changed'};
    if (all('[contenteditable="true"][role="textbox"]').length !== 1
        || editor.innerText !== text) return {ok: false, reason: 'text_changed'};
    const media = [...dialog.querySelectorAll('video,audio,iframe,object,embed,canvas,fieldset,table,'
        + '[role="radiogroup"],[role="radio"],a[download]')];
    const images = [...dialog.querySelectorAll('img')].filter(el => !authors.some(a => a.contains(el)));
    const files = [...dialog.querySelectorAll('input[type="file"]')]
        .some(el => el.files && el.files.length);
    if (media.length || images.length || files)
        return {ok: false, reason: 'unsupported_attachment'};
    if (Date.now() >= deadline) return {ok: false, reason: 'preview_expired'};
    if (click) {
        if (button.disabled || button.getAttribute('aria-disabled') === 'true')
            return {ok: false, reason: 'publish_disabled'};
        button.click();
    }
    return {ok: true};
}"""


@dataclass
class PostDraft:
    text: str
    author_url: str
    expires: float
    attempted: bool = False


@dataclass
class PostState:
    drafts: dict[str, PostDraft]
    lock: asyncio.Lock


# Ready-extractor returns a fresh facade per call. Share previews/lock on the
# actual Page, without retaining the page after the browser is closed.
_STATES: WeakKeyDictionary[Any, PostState] = WeakKeyDictionary()


def profile_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.linkedin.com"
        or not re.fullmatch(r"/in/[^/%]+/?", parsed.path)
        or parsed.path.rstrip("/") == "/in/me"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Cannot verify the personal author profile.")
    return "https://www.linkedin.com" + parsed.path.rstrip("/") + "/"


class PostWriter:
    """Keep bounded, expiring, single-attempt previews per extractor session."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._state = _STATES.setdefault(session.page, PostState({}, asyncio.Lock()))

    def prepare(self, text: str, author_url: str) -> dict[str, Any]:
        # LinkedIn counts UTF-16 units, not Python code points, for text limits.
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 3000:
            raise ValueError("Post text must contain 1–3000 UTF-16 units.")
        author_url = profile_url(author_url)
        now = time.monotonic()
        kept = {
            key: value
            for key, value in self._state.drafts.items()
            if value.expires > now and not value.attempted
        }
        self._state.drafts.clear()
        self._state.drafts.update(kept)
        if len(self._state.drafts) >= 20:
            raise ValueError("Too many pending previews; wait for their expiry.")
        token = secrets.token_urlsafe(24)
        self._state.drafts[token] = PostDraft(text, author_url, now + 600)
        return {
            "status": "preview",
            "draft_id": token,
            "text": text,
            "author_url": author_url,
            "audience": "anyone",
            "expires_in_seconds": 600,
            "published": False,
        }

    async def publish(
        self, draft_id: str, approved_text: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        async with self._state.lock:
            draft = self._state.drafts.get(draft_id)
            if draft is None or draft.expires <= time.monotonic():
                raise ValueError("Unknown or expired preview. Prepare a new preview.")
            if draft.attempted:
                raise ValueError("Attempt already made. Inspect LinkedIn; never retry.")
            if not confirmed:
                return {"status": "confirmation_required", "published": False}
            if approved_text != draft.text:
                raise ValueError("Approved text differs from the exact preview.")
            page = self._session.page
            # Translate the monotonic expiry once for the atomic browser-side
            # deadline check, which also covers transport delay before evaluation.
            deadline = (time.time() + draft.expires - time.monotonic()) * 1000
            await self._navigator._navigate_to_page(
                "https://www.linkedin.com/feed/?shareActive=true"
            )
            await self._session.check_rate_limit()
            locale = (await page.locator("html").get_attribute("lang") or "").split(
                "-"
            )[0]
            if locale not in LABELS:
                raise ValueError("Unsupported composer language; no post attempted.")
            labels = LABELS[locale]
            # A rich-text editor and its owner/action must be read from the DOM;
            # body text alone cannot attribute a composer or perform a write.
            dialog = page.locator('dialog[open]:visible, [role="dialog"]:visible')
            await dialog.wait_for(state="visible", timeout=10000)
            if await dialog.count() != 1:
                raise ValueError("Expected exactly one visible composer dialog.")
            authors = await dialog.locator('a[href*="/in/"]').evaluate_all(
                "els => els.map(e => e.href)"
            )
            if not authors or any(
                profile_url(url) != draft.author_url for url in authors
            ):
                raise ValueError(
                    "Composer author differs from the approved personal profile."
                )
            audience = dialog.get_by_role("button", name=labels["audience"], exact=True)
            await audience.wait_for(state="visible", timeout=10000)
            if await audience.count() != 1:
                raise ValueError("Cannot verify the approved Anyone audience.")
            editor = dialog.locator('[contenteditable="true"][role="textbox"]:visible')
            await editor.wait_for(state="visible", timeout=10000)
            if await editor.count() != 1 or (await editor.inner_text()).strip():
                raise ValueError("Composer is ambiguous or already contains a draft.")
            button = dialog.get_by_role("button", name=labels["publish"], exact=True)
            await button.wait_for(state="visible", timeout=10000)
            if await button.count() != 1:
                raise ValueError("Cannot uniquely identify the publish action.")
            # Record before any composer mutation: cancellation or transport errors
            # must not permit a second click after an uncertain first attempt.
            if draft.expires <= time.monotonic():
                raise ValueError("Preview expired while waiting for the composer.")
            try:
                async with (
                    linkedin_element(dialog) as checked_dialog,
                    linkedin_element(editor) as checked_editor,
                    linkedin_element(button) as checked_button,
                ):
                    args = {
                        "dialog": checked_dialog,
                        "editor": checked_editor,
                        "author": draft.author_url,
                        "audience": labels["audience"],
                        "action": labels["publish"],
                        "text": "",
                        "deadline": deadline,
                        "click": False,
                    }
                    valid = await checked_button.evaluate(COMPOSER_GUARD_JS, args)
                    if not valid["ok"]:
                        raise ValueError(f"Composer refused: {valid['reason']}")
                    if draft.expires <= time.monotonic():
                        raise ValueError("Preview expired before filling.")
                    draft.attempted = True
                    await checked_editor.fill(draft.text)
                    if draft.expires <= time.monotonic():
                        return {
                            "status": "not_submitted",
                            "reason": "preview_expired",
                            "published": False,
                            "retry_allowed": False,
                        }
                    args = {**args, "text": draft.text, "click": True}
                    valid = await checked_button.evaluate(COMPOSER_GUARD_JS, args)
                    if not valid["ok"]:
                        return {
                            "status": "not_submitted",
                            "reason": valid["reason"],
                            "published": False,
                            "retry_allowed": False,
                        }
                # Only an observed navigation to a new post permalink confirms
                # publication. Dialog closure or a generic toast is insufficient.
                await page.wait_for_url(
                    re.compile(
                        r"^https://www\.linkedin\.com/feed/update/urn:li:(activity|share):\d+/?(?:\?.*)?$"
                    ),
                    timeout=10000,
                )
                article = page.locator("main article")
                if await article.count() != 1:
                    return {
                        "status": "outcome_unknown",
                        "published": False,
                        "retry_allowed": False,
                    }
                rendered = " ".join(await article.locator("p").all_inner_texts())
                if " ".join(draft.text.split()) != " ".join(rendered.split()):
                    return {
                        "status": "outcome_unknown",
                        "published": False,
                        "retry_allowed": False,
                    }
                post_authors = await article.locator(
                    'header a[href*="/in/"]'
                ).evaluate_all("els => els.map(e => e.href)")
                if [profile_url(url) for url in post_authors] != [draft.author_url]:
                    return {
                        "status": "outcome_unknown",
                        "published": False,
                        "retry_allowed": False,
                    }
                return {
                    "status": "published",
                    "published": True,
                    "url": urljoin(page.url, urlsplit(page.url).path),
                    "retry_allowed": False,
                }
            except ValueError:
                if not draft.attempted:
                    raise
                return {
                    "status": "outcome_unknown",
                    "published": None,
                    "retry_allowed": False,
                }
            except Exception as exc:
                return {
                    "status": "outcome_unknown",
                    "published": None,
                    "retry_allowed": False,
                    "error_type": type(exc).__name__,
                }
