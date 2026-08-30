"""LinkedIn post composer: write a post, attach images, hand it to LinkedIn's
own scheduler.

Why the native scheduler rather than our own timed sender: LinkedIn holds the
post, so the founder's Mac can be asleep in a bag at a conference and the post
still goes out. It is also the same flow he performs by hand, so it introduces
no new behaviour on the account.

Interaction style follows the pattern upstream established in
``extractor.send_message``: patchright's actionability checks time out against
LinkedIn's React-hydrated contenteditable, so we focus via ``page.evaluate``
and type via ``page.keyboard.type``, which fires the real key events React
needs. Buttons are clicked from JavaScript for the same reason.

Selectors use ARIA attributes, roles and visible text only -- never layout
class names -- so they survive LinkedIn's frequent restyling.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"

# LinkedIn's own limit on a post body.
MAX_POST_CHARS = 3000

# Images per post that LinkedIn accepts in one share.
MAX_IMAGES = 20

_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ComposerError(RuntimeError):
    """A step of the composer flow could not be completed."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


async def _click_by_text(page: Any, candidates: list[str], *, within_dialog: bool) -> str | None:
    """Click the first visible, enabled control matching a candidate.

    Each candidate is either ``aria:<substring>`` (matched case-insensitively
    against aria-label) or ``text:<substring>`` (matched against trimmed
    innerText). Returns the candidate that matched, or None.
    """
    return await page.evaluate(
        """
        ([candidates, withinDialog]) => {
            const root = withinDialog
                ? (document.querySelector('div[role="dialog"]') || document)
                : document;
            const visible = (el) => !!(
                el.offsetWidth || el.offsetHeight || el.getClientRects().length
            );
            const controls = Array.from(
                root.querySelectorAll('button, [role="button"], [role="menuitem"]')
            ).filter((el) => visible(el) && !el.disabled
                     && el.getAttribute('aria-disabled') !== 'true');
            for (const cand of candidates) {
                const [kind, ...rest] = cand.split(':');
                const needle = rest.join(':').toLowerCase();
                for (const el of controls) {
                    const hay = kind === 'aria'
                        ? (el.getAttribute('aria-label') || '').toLowerCase()
                        : (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (hay && hay.includes(needle)) {
                        el.click();
                        return cand;
                    }
                }
            }
            return null;
        }
        """,
        [candidates, within_dialog],
    )


async def _wait_for_dialog(page: Any, *, timeout: float = 10.0) -> bool:
    """Wait for the composer modal to be present and visible."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        present = await page.evaluate(
            """() => {
                const d = document.querySelector('div[role="dialog"]');
                return !!(d && (d.offsetWidth || d.offsetHeight
                                || d.getClientRects().length));
            }"""
        )
        if present:
            return True
        await asyncio.sleep(0.25)
    return False


async def _focus_editor(page: Any) -> bool:
    """Focus the composer's contenteditable without an actionability check."""
    return await page.evaluate(
        """() => {
            const root = document.querySelector('div[role="dialog"]') || document;
            const el = root.querySelector(
                'div[role="textbox"][contenteditable="true"][aria-label*="Text editor" i],'
                + 'div[role="textbox"][contenteditable="true"],'
                + '[contenteditable="true"][aria-label*="post" i]'
            );
            if (!el) return false;
            el.focus();
            return true;
        }"""
    )


async def _editor_text(page: Any) -> str:
    """Read back what the editor actually contains, to verify the type landed."""
    text = await page.evaluate(
        """() => {
            const root = document.querySelector('div[role="dialog"]') || document;
            const el = root.querySelector('div[role="textbox"][contenteditable="true"]');
            return el ? (el.innerText || '') : '';
        }"""
    )
    return text if isinstance(text, str) else ""


# --------------------------------------------------------------------------
# Inspection -- used while pinning selectors against the live DOM
# --------------------------------------------------------------------------


async def inspect_composer(page: Any, goto: Any) -> dict[str, Any]:
    """Open the composer and report every control it exposes.

    Development aid, and a live check that LinkedIn has not moved anything.
    Opens the composer and closes it again without posting; it types nothing,
    so it is a read as far as the account is concerned.
    """
    await goto(FEED_URL)
    await asyncio.sleep(1.5)

    trigger = await _click_by_text(
        page,
        [
            "aria:start a post",
            "aria:create a post",
            "text:start a post",
            "text:start a post, start a post",
        ],
        within_dialog=False,
    )
    opened = await _wait_for_dialog(page) if trigger else False

    snapshot = await page.evaluate(
        """() => {
            const root = document.querySelector('div[role="dialog"]') || document.body;
            const visible = (el) => !!(
                el.offsetWidth || el.offsetHeight || el.getClientRects().length
            );
            const describe = (el) => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role'),
                aria: el.getAttribute('aria-label'),
                text: (el.innerText || '').trim().slice(0, 60),
                id: el.id || null,
                type: el.getAttribute('type'),
                disabled: !!el.disabled,
            });
            return {
                dialogLabel: (document.querySelector('div[role="dialog"]')
                    || {}).getAttribute?.('aria-label') || null,
                buttons: Array.from(root.querySelectorAll('button, [role="button"]'))
                    .filter(visible).map(describe),
                textboxes: Array.from(root.querySelectorAll(
                    '[contenteditable="true"], [role="textbox"]')).map(describe),
                inputs: Array.from(root.querySelectorAll('input, select'))
                    .map(describe),
            };
        }"""
    )

    await _dismiss_composer(page)
    return {"trigger_matched": trigger, "dialog_opened": opened, **snapshot}


async def _dismiss_composer(page: Any) -> None:
    """Close the composer, discarding any draft, without posting."""
    try:
        await _click_by_text(
            page, ["aria:dismiss", "aria:close"], within_dialog=True
        )
        await asyncio.sleep(0.6)
        # LinkedIn asks whether to save a draft when there is text.
        await _click_by_text(
            page, ["text:discard", "aria:discard"], within_dialog=True
        )
        await asyncio.sleep(0.4)
    except Exception:
        logger.debug("Composer dismiss failed; continuing", exc_info=True)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_images(image_paths: list[str] | None) -> list[Path]:
    """Resolve and check image paths before the browser is touched."""
    if not image_paths:
        return []
    if len(image_paths) > MAX_IMAGES:
        raise ComposerError(
            f"{len(image_paths)} images requested; LinkedIn accepts at most {MAX_IMAGES}."
        )
    resolved: list[Path] = []
    for raw in image_paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ComposerError(f"Image not found: {path}")
        if path.suffix.lower() not in _ALLOWED_IMAGE_SUFFIXES:
            raise ComposerError(
                f"Unsupported image type {path.suffix!r} for {path.name}; "
                f"expected one of {sorted(_ALLOWED_IMAGE_SUFFIXES)}."
            )
        resolved.append(path.resolve())
    return resolved


def parse_schedule_at(schedule_at: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 local datetime and refuse anything LinkedIn will not take.

    LinkedIn requires a scheduled time at least ~10 minutes out and no more
    than 3 months ahead, on a 5-minute boundary in the account's timezone.
    """
    if not schedule_at:
        return None
    try:
        when = dt.datetime.fromisoformat(schedule_at)
    except ValueError as exc:
        raise ComposerError(
            f"schedule_at must be ISO-8601 local time such as "
            f"'2026-09-02T08:30' -- got {schedule_at!r}."
        ) from exc
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)

    now = dt.datetime.now()
    if when <= now + dt.timedelta(minutes=10):
        raise ComposerError(
            f"schedule_at must be at least 10 minutes ahead; "
            f"{when.isoformat()} is not (now is {now.isoformat(timespec='minutes')})."
        )
    if when > now + dt.timedelta(days=90):
        raise ComposerError(
            "LinkedIn does not accept a scheduled time more than ~3 months ahead."
        )
    if when.minute % 5 != 0:
        raise ComposerError(
            f"LinkedIn's scheduler only offers 5-minute increments; "
            f"{when.strftime('%H:%M')} is not on one."
        )
    return when


# --------------------------------------------------------------------------
# Media
# --------------------------------------------------------------------------


async def _attach_images(page: Any, images: list[Path]) -> None:
    """Attach images by feeding LinkedIn's hidden file input directly.

    Clicking "Add media" and driving the OS file picker is not automatable;
    setting files on the input the picker would have populated is, and it is
    what LinkedIn's own JavaScript reads.
    """
    await _click_by_text(
        page,
        [
            "aria:add media",
            "aria:add a photo",
            "aria:add photo",
            "text:add media",
        ],
        within_dialog=True,
    )
    await asyncio.sleep(1.0)

    file_input = page.locator('input[type="file"]').first
    try:
        await file_input.set_input_files(
            [str(p) for p in images], timeout=15000
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller as status
        raise ComposerError(
            f"Could not hand {len(images)} image(s) to LinkedIn's file input: {exc}"
        ) from exc

    # Upload + preview render. LinkedIn shows a media editor with its own
    # confirm button before returning to the composer.
    await asyncio.sleep(3.0)
    for _ in range(3):
        matched = await _click_by_text(
            page,
            ["text:next", "aria:next", "text:done", "aria:done"],
            within_dialog=True,
        )
        if not matched:
            break
        await asyncio.sleep(1.2)


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


async def _set_react_input(page: Any, selector: str, value: str) -> bool:
    """Set a React-controlled input so React actually sees the change.

    Assigning ``.value`` alone is invisible to React: it tracks the previous
    value on the node and swallows the event as a no-op. Going through the
    prototype's native setter first is what makes the dispatched input event
    register.
    """
    return await page.evaluate(
        """
        ([selector, value]) => {
            const root = document.querySelector('div[role="dialog"]') || document;
            const el = root.querySelector(selector);
            if (!el) return false;
            const proto = el instanceof HTMLSelectElement
                ? window.HTMLSelectElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, value);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
        """,
        [selector, value],
    )


async def _apply_schedule(page: Any, when: dt.datetime) -> dict[str, Any]:
    """Drive LinkedIn's native schedule dialog to the requested time."""
    opened = await _click_by_text(
        page,
        ["aria:schedule post", "aria:schedule", "text:schedule"],
        within_dialog=True,
    )
    if not opened:
        raise ComposerError(
            "Could not find the schedule (clock) control in the composer."
        )
    await asyncio.sleep(1.2)

    date_str = when.strftime("%m/%d/%Y")
    time_str = when.strftime("%-I:%M %p")

    date_ok = False
    for selector in (
        'input[id*="date" i]',
        'input[name*="date" i]',
        'input[placeholder*="date" i]',
        'input[type="text"]',
    ):
        if await _set_react_input(page, selector, date_str):
            date_ok = True
            break

    time_ok = False
    for selector in (
        'select[id*="time" i]',
        'input[id*="time" i]',
        'select[name*="time" i]',
        'input[name*="time" i]',
        "select",
    ):
        if await _set_react_input(page, selector, time_str):
            time_ok = True
            break

    if not (date_ok and time_ok):
        raise ComposerError(
            f"Schedule dialog did not accept the values "
            f"(date_set={date_ok}, time_set={time_ok}); "
            f"wanted {date_str} {time_str}. LinkedIn may have changed the dialog."
        )

    await asyncio.sleep(0.5)
    confirmed = await _click_by_text(
        page, ["text:next", "aria:next", "text:done"], within_dialog=True
    )
    if not confirmed:
        raise ComposerError("Schedule dialog had no Next/Done control to confirm.")
    await asyncio.sleep(1.2)
    return {"date": date_str, "time": time_str}


# --------------------------------------------------------------------------
# The write
# --------------------------------------------------------------------------


async def create_post(
    page: Any,
    goto: Any,
    *,
    text: str,
    image_paths: list[str] | None = None,
    schedule_at: str | None = None,
) -> dict[str, Any]:
    """Publish or schedule a post on the founder's own feed.

    Every argument is validated before the browser is touched, so a bad
    request costs no page load and cannot leave a half-written draft behind.
    """
    body = (text or "").strip()
    if not body:
        raise ComposerError("Refusing to post empty text.")
    if len(body) > MAX_POST_CHARS:
        raise ComposerError(
            f"Post is {len(body)} characters; LinkedIn's limit is {MAX_POST_CHARS}."
        )
    images = validate_images(image_paths)
    when = parse_schedule_at(schedule_at)

    await goto(FEED_URL)
    await asyncio.sleep(1.5)

    trigger = await _click_by_text(
        page,
        [
            "aria:start a post",
            "aria:create a post",
            "text:start a post",
        ],
        within_dialog=False,
    )
    if not trigger:
        raise ComposerError("Could not find the 'Start a post' control on the feed.")
    if not await _wait_for_dialog(page):
        raise ComposerError("Composer did not open after clicking 'Start a post'.")

    if not await _focus_editor(page):
        await _dismiss_composer(page)
        raise ComposerError("Could not focus the composer text editor.")

    await asyncio.sleep(0.2)
    # Typed rather than pasted: LinkedIn's editor builds its internal model
    # from key events, and a human-plausible cadence costs us nothing here.
    await page.keyboard.type(body, delay=12)
    await asyncio.sleep(0.8)

    landed = await _editor_text(page)
    if body.split("\n", 1)[0][:40] not in landed:
        await _dismiss_composer(page)
        raise ComposerError(
            "Composer did not receive the text (editor content did not match)."
        )

    if images:
        await _attach_images(page, images)

    schedule_info: dict[str, Any] | None = None
    if when is not None:
        schedule_info = await _apply_schedule(page, when)

    submit = await _click_by_text(
        page,
        (
            ["text:schedule", "aria:schedule"]
            if when is not None
            else ["aria:post", "text:post"]
        ),
        within_dialog=True,
    )
    if not submit:
        await _dismiss_composer(page)
        raise ComposerError(
            "Could not find the "
            + ("Schedule" if when is not None else "Post")
            + " button; nothing was submitted."
        )

    await asyncio.sleep(3.0)
    still_open = await page.evaluate(
        """() => {
            const d = document.querySelector('div[role="dialog"]');
            return !!(d && (d.offsetWidth || d.offsetHeight
                            || d.getClientRects().length));
        }"""
    )

    return {
        "status": "scheduled" if when is not None else "posted",
        "submitted": True,
        "composer_closed": not still_open,
        "characters": len(body),
        "images_attached": len(images),
        "scheduled_for": when.isoformat(timespec="minutes") if when else None,
        "schedule_fields": schedule_info,
        "url": page.url,
    }
