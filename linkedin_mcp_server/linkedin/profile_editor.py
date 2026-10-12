"""Read and write the signed-in member's own profile through LinkedIn's edit forms.

Implements ``profile_edit.service.ProfileEditorPort``. Each write opens the
field's own edit form by URL, checks that the visible value is still the
expected "before" value, refuses if the form would notify the member's network,
changes only that control, saves, and waits for the dialog to close. It never
reports success itself: the service re-reads the field afterwards and compares.
Locators all come from ``profile_selectors``.
"""

from __future__ import annotations

from typing import Any, Literal

import asyncio
import logging
import re
import time

from patchright.async_api import Locator, Page

import linkedin_mcp_server.linkedin.profile_selectors as sel
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.profile_edit.changeset import skill_key
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    ENTRY_SPECS,
    EntrySummary,
    ExperienceForm,
    ExperienceSummary,
    Skill,
    TextField,
    normalize_text,
)

logger = logging.getLogger(__name__)

_DIALOG_TIMEOUT_MS = 15_000
_SAVE_TIMEOUT_MS = 15_000
_FIELD_TIMEOUT_MS = 10_000
_SETTLE_MS = 500
_SETTLE_READS = 8
_EMPTY_SETTLE_MS = 4_000
_VIEW_SETTLE_MS = 2_500
_MAX_VIEW_SCROLLS = 8
_SCROLL_SETTLE_MS = 1_500
_OPTION_TIMEOUT_MS = 6_000
# Hard limit per step of a write, and the duration after which a step is logged.
_PHASE_LIMIT_S = 45.0
_SLOW_PHASE_S = 15.0
_DATE_RANGE = re.compile(r"\b(19|20)\d{2}\b")


class ProfileEditor:
    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        *,
        locale: str = sel.DEFAULT_LOCALE,
    ):
        self._session = session
        self._navigator = navigator
        self._labels = sel.LABELS.get(locale, sel.LABELS[sel.DEFAULT_LOCALE])
        self._vanity: str | None = None
        self._navigations = 0
        self._experience_forms: dict[str, str] = {}
        self._location: str | None = None

    @property
    def _page(self) -> Page:
        return self._session.page

    # ── navigation ──────────────────────────────────────────────────────────
    async def pause(self, seconds: float) -> None:
        await self._session.delay(seconds)

    async def _goto(self, url: str) -> None:
        if self._navigations:
            await self._session.delay(NAV_DELAY)
        self._navigations += 1
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()

    async def _vanity_name(self) -> str:
        if self._vanity is None:
            await self._goto(sel.OWN_PROFILE_URL)
            m = sel.VANITY_FROM_URL.search(self._page.url)
            if not m or m.group(1) == "me":
                raise ProfileEditError(
                    ProfileEditErrorCode.PROFILE_NOT_FOUND, url=self._page.url
                )
            self._vanity = m.group(1)
        return self._vanity

    def _dialog(self) -> Locator:
        """The visible edit dialog; hidden ad dialogs are ignored."""
        page = self._page
        return page.locator(sel.DIALOG).filter(has=page.locator(sel.DIALOG_HAS)).first

    async def _open_dialog(self, url: str, what: str) -> None:
        await self._goto(url)
        try:
            await self._dialog().wait_for(state="visible", timeout=_DIALOG_TIMEOUT_MS)
        except Exception:
            raise await self._not_found(what, url, "edit dialog did not open") from None
        await self._use_page_locale()

    async def _use_page_locale(self) -> None:
        """Pick the label table for the UI language LinkedIn rendered.

        An unknown language keeps English; exact labels then fail closed with
        SELECTOR_NOT_FOUND rather than matching something else.
        """
        try:
            lang = await self._page.evaluate("document.documentElement.lang") or ""
        except Exception:
            return
        table = sel.LABELS.get(lang.split("-")[0].lower())
        if table is not None:
            self._labels = table

    async def _leave_unsaved(self) -> None:
        """Abandon an open form without saving: navigate back to the profile."""
        await self._goto(sel.profile_url(await self._vanity_name()))

    async def _open_form(self, urls: tuple[str, ...], spec: sel.FieldSpec) -> Locator:
        """Navigate to the first URL whose dialog contains *spec*; return the field."""
        last: ProfileEditError | None = None
        for url in urls:
            try:
                await self._open_dialog(url, spec.name)
                return await self._field(spec, url)
            except ProfileEditError as e:
                if e.code is not ProfileEditErrorCode.SELECTOR_NOT_FOUND:
                    raise
                last = e
        assert last is not None
        raise last

    async def _field(self, spec: sel.FieldSpec, url: str) -> Locator:
        dialog = self._dialog()
        # Rich-text editors mount after the dialog itself appears.
        selector = ", ".join(spec.css) if spec.css else "input, select"
        try:
            await dialog.locator(selector).first.wait_for(
                state="visible", timeout=_FIELD_TIMEOUT_MS
            )
        except Exception:
            pass  # reported below with the dialog's actual controls
        for css in spec.css:
            loc = dialog.locator(css)
            if await loc.count() == 1:
                return loc
        for key in spec.label_keys:
            for text in self._labels.get(key, ()):
                loc = dialog.get_by_label(text, exact=True)
                if await loc.count() == 1:
                    return loc
        raise await self._not_found(spec.name, url, "no unique control matched")

    async def _not_found(self, what: str, url: str, reason: str) -> ProfileEditError:
        try:
            described = await self._page.evaluate(sel.DESCRIBE_DIALOG_JS)
        except Exception:
            described = None
        return ProfileEditError(
            ProfileEditErrorCode.SELECTOR_NOT_FOUND,
            f"Could not find the {what} control: {reason}.",
            control=what,
            url=url,
            currentUrl=self._page.url,
            dialog=described,
        )

    # ── field primitives ────────────────────────────────────────────────────
    @staticmethod
    async def _is_plain(loc: Locator) -> bool:
        return await loc.evaluate(
            "(el) => ['input', 'textarea'].includes(el.tagName.toLowerCase())"
        )

    async def _read(self, loc: Locator) -> TextField:
        if await self._is_plain(loc):
            value = await loc.input_value()
        else:
            # A rich-text editor fills itself after it mounts; accept a value only
            # once two reads agree, so a baseline is never taken mid-load.
            value = await loc.inner_text()
            for _ in range(_SETTLE_READS):
                await self._page.wait_for_timeout(_SETTLE_MS)
                again = await loc.inner_text()
                if again == value:
                    break
                value = again
            # Two empty reads agree before a slow editor has filled in, so an
            # empty value is accepted only once it has stayed empty for
            # _EMPTY_SETTLE_MS; a real value usually arrives well before that.
            waited = 0
            while not value.strip() and waited < _EMPTY_SETTLE_MS:
                await self._page.wait_for_timeout(_SETTLE_MS)
                waited += _SETTLE_MS
                value = await loc.inner_text()
        return TextField(value=value, max_length=await self._limit(loc))

    async def _limit(self, loc: Locator) -> int | None:
        """The field's own limit: its maxlength, or a maximum stated in its label.

        Never a counter found elsewhere in the dialog: a stray "1/7" in the intro
        form was once read as the headline's limit. Unknown means the default.
        """
        raw = await loc.get_attribute("maxlength")
        if raw and raw.isdigit():
            return int(raw)
        label = await loc.get_attribute("aria-label") or ""
        if m := sel.STATED_MAX.search(label):
            return int(re.sub(r"\D", "", m.group(1)))
        return None

    async def _type(self, loc: Locator, value: str) -> None:
        if await self._is_plain(loc):
            await loc.fill(value)
            return
        # A rich-text box: replace its content. LinkedIn separates paragraphs
        # with an empty paragraph, so a blank line is typed as two Enters; a
        # single line break is Shift+Enter.
        keyboard = self._page.keyboard
        await loc.click()
        await keyboard.press("ControlOrMeta+A")
        await keyboard.press("Delete")
        for i, paragraph in enumerate(value.split("\n\n")):
            if i:
                await keyboard.press("Enter")
                await keyboard.press("Enter")
            for j, line in enumerate(paragraph.split("\n")):
                if j:
                    await keyboard.press("Shift+Enter")
                if line:
                    await keyboard.insert_text(line)

    async def _refuse_if_notifying(self, field: str) -> None:
        """Never let an approved edit also broadcast an update to the network."""
        switches = self._dialog().locator(sel.NOTIFY_SWITCH)
        for i in range(await switches.count()):
            if await switches.nth(i).is_checked():
                raise ProfileEditError(
                    ProfileEditErrorCode.VALIDATION_ERROR,
                    f"The {field} form has LinkedIn's notify-your-network switch on. "
                    "Turn it off on linkedin.com first; this server neither changes "
                    "it nor saves with it on.",
                    field=field,
                )

    async def _replace(
        self,
        loc: Locator,
        *,
        expected: str,
        value: str,
        field: str,
        url: str,
        dry_run: bool = False,
    ) -> None:
        current = normalize_text((await self._read(loc)).value)
        if current != normalize_text(expected):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                f"The {field} on LinkedIn no longer matches the change set; nothing was typed.",
                field=field,
                expected=expected,
                actual=current,
            )
        await self._refuse_if_notifying(field)
        await self._type(loc, value)
        typed = normalize_text((await self._read(loc)).value)
        if typed != normalize_text(value):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The {field} field did not take the exact value (a shorter limit or "
                "reformatting); nothing was saved.",
                field=field,
                typedLength=len(typed),
                wantedLength=len(value),
            )
        if dry_run:
            await self._leave_unsaved()
            return
        await self._save(field, url)

    async def _button(self, key: str, scope: Locator | None = None) -> Locator | None:
        within = scope if scope is not None else self._dialog()
        for text in self._labels.get(key, ()):
            loc = within.get_by_role("button", name=text, exact=True)
            if await loc.count() == 1:
                return loc
        return None

    async def _save(self, field: str, url: str) -> None:
        dialog = self._dialog()
        save = None
        for css in sel.SAVE_BUTTON:
            loc = dialog.locator(css)
            if await loc.count() == 1:
                save = loc
                break
        save = save or await self._button("save")
        if save is None:
            raise await self._not_found(
                f"{field} save button", url, "no unique save control"
            )
        form_url = self._page.url
        await save.click()
        try:
            await self._dialog().wait_for(state="hidden", timeout=_SAVE_TIMEOUT_MS)
        except Exception:
            if await self._saved_into_follow_up(form_url):
                await self._session.check_rate_limit()
                return
            errors: list[str] = []
            for css in sel.FORM_ERROR:
                found = self._dialog().locator(css)
                for i in range(await found.count()):
                    t = (await found.nth(i).inner_text()).strip()
                    if t:
                        errors.append(t[:200])
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"LinkedIn did not close the {field} form after Save.",
                field=field,
                formErrors=errors,
                currentUrl=self._page.url,
            ) from None
        await self._session.check_rate_limit()

    async def _saved_into_follow_up(self, form_url: str) -> bool:
        """Whether Save succeeded and LinkedIn opened a follow-up dialog instead.

        Adding a position can replace the form with a "share this with your
        network" prompt at a ``next-action`` URL rather than closing it; the
        prompt is abandoned by navigating away, so nothing is shared. Adding a
        publication can instead return to the profile page. In both cases the
        form was saved, and verification still re-reads the section.
        """
        url = self._page.url
        if url.rstrip("/") == form_url.rstrip("/"):
            return False
        if sel.FOLLOW_UP_AFTER_SAVE.search(url):
            logger.info(
                "Save opened a follow-up prompt (%s); leaving it unanswered", url
            )
            await self._leave_unsaved()
            return True
        # LinkedIn also closes a form by navigating back to a page that is not
        # an edit form (the profile); another popup there can keep a dialog
        # visible. Leaving the form URL is LinkedIn's own sign the save went
        # through; the caller still verifies the result by reading it back.
        if "/edit/" not in url:
            logger.info("Save left the form for %s; treating it as saved", url)
            return True
        return False

    # ── reads ───────────────────────────────────────────────────────────────
    async def account(self) -> str:
        """The signed-in member's profile URL, resolved from /in/me/ per call."""
        return sel.profile_url(await self._vanity_name())

    async def read_identity(self) -> tuple[str, str | None, str | None]:
        vanity = await self._vanity_name()
        name = None
        h1 = self._page.locator("main h1")
        if await h1.count():
            name = (await h1.first.inner_text()).strip() or None
        if name is None:  # the page title is "<Name> | LinkedIn"
            name = (await self._page.title()).rsplit(" | ", 1)[0].strip() or None
        return sel.profile_url(vanity), name, None

    async def read_headline(self) -> TextField:
        url = sel.intro_form_url(await self._vanity_name())
        field = await self._read(await self._open_form((url,), sel.HEADLINE))
        try:
            location = await self._field(sel.LOCATION, url)
            self._location = normalize_text(await location.input_value()) or None
        except ProfileEditError:
            self._location = None  # display only; never blocks a read
        return field

    async def read_location(self) -> str | None:
        return self._location

    async def read_about(self) -> TextField:
        urls = sel.about_form_urls(await self._vanity_name())
        return await self._read(await self._open_form(urls, sel.ABOUT))

    async def describe_form(self, form: str) -> dict[str, Any]:
        """Open an edit form and describe its controls; nothing is typed or saved."""
        vanity = await self._vanity_name()
        if form == "connections":
            await self._goto(sel.CONNECTIONS_URL)
            await self._page.wait_for_timeout(_SETTLE_MS * 4)
            return {
                "form": form,
                **await self._page.evaluate(sel.DESCRIBE_CONNECTIONS_JS),
            }
        if form.startswith("list:") and form[5:] in sel.NEW_ENTRY_FORM_PATHS:
            await self._goto(sel.entry_list_url(vanity, form[5:]))
            await self._session.scroll_body(pause_time=0.8, max_scrolls=4)
            hrefs = await self._page.evaluate(
                '() => [...new Set([...document.querySelectorAll(\'main a[href*="/edit/"], main a[href*="/details/"]\')]'
                ".map((a) => a.getAttribute('href').split('?')[0]))].slice(0, 40)"
            )
            return {"form": form, "url": self._page.url, "hrefs": hrefs}
        if form == "intro":
            urls: tuple[str, ...] = (sel.intro_form_url(vanity),)
        elif form in sel.NEW_ENTRY_FORM_PATHS:
            urls = sel.new_entry_form_urls(vanity, form)
        else:
            raise ProfileEditError(ProfileEditErrorCode.UNSUPPORTED_FIELD, form=form)
        tried: list[dict[str, Any]] = []
        for url in urls:
            try:
                await self._open_dialog(url, form)
            except ProfileEditError as e:
                if e.code is not ProfileEditErrorCode.SELECTOR_NOT_FOUND:
                    raise
                tried.append({"url": url, "currentUrl": self._page.url})
                continue
            await self._page.wait_for_timeout(_SETTLE_MS * 2)
            return {
                "form": form,
                "tried": tried,
                **await self._page.evaluate(sel.DESCRIBE_FORM_JS),
            }
        return {"form": form, "dialog": False, "tried": tried}

    async def list_experiences(self) -> list[ExperienceSummary]:
        vanity = await self._vanity_name()
        await self._goto(sel.experience_list_url(vanity))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=8)
        items = await self._page.evaluate(
            sel.LIST_ITEMS_JS, sel.EXPERIENCE_EDIT_HREF.pattern
        )
        out: list[ExperienceSummary] = []
        for item in items:
            self._experience_forms[item["id"]] = item["href"]
            out.append(_summary(item))
        return out

    async def read_experience(self, experience_id: str) -> ExperienceForm:
        vanity = await self._vanity_name()
        if experience_id not in self._experience_forms:
            listed = await self.list_experiences()
            if experience_id not in {e.id for e in listed}:
                raise ProfileEditError(
                    ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                    experienceId=experience_id,
                )
        url = sel.experience_form_url(vanity, experience_id)
        title = await self._read(await self._open_form((url,), sel.EXPERIENCE_TITLE))
        description = await self._read(
            await self._field(sel.EXPERIENCE_DESCRIPTION, url)
        )
        company = None
        try:
            company_field = await self._field(sel.EXPERIENCE_COMPANY, url)
            company = normalize_text(await company_field.input_value()) or None
        except ProfileEditError:
            pass  # display only
        return ExperienceForm(
            id=experience_id, title=title, description=description, company=company
        )

    async def list_skills(self) -> list[Skill]:
        """Every skill, merged by LinkedIn's skill id across the page's views.

        The default view lists only some skills; the category views list the
        rest. Order is first appearance: the default view, then each category.
        """
        vanity = await self._vanity_name()
        await self._goto(sel.skills_list_url(vanity))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=10)
        found: dict[str, str] = {}

        async def collect() -> None:
            # Each view also loads more as it scrolls: read, scroll, and stop
            # once a scroll brings nothing new.
            for attempt in range(_MAX_VIEW_SCROLLS):
                before = len(found)
                items = await self._page.evaluate(
                    sel.LIST_ITEMS_JS, sel.SKILL_EDIT_HREF.pattern
                )
                for item in items:
                    if item["lines"] and item["id"] not in found:
                        found[item["id"]] = item["lines"][0]
                if len(found) == before and attempt > 0:
                    break
                # The list scrolls inside its own container, not the window:
                # bringing the last loaded item into view scrolls whichever
                # container holds it, which loads the next batch.
                await self._page.evaluate(
                    sel.SCROLL_LAST_ITEM_JS, sel.SKILL_EDIT_HREF.pattern
                )
                await self._page.wait_for_timeout(_SCROLL_SETTLE_MS)
            else:
                # Every scroll up to the cap still brought new skills, so the
                # view may hold more. A partial list must never be used to
                # decide what is a duplicate or to verify an edit.
                raise ProfileEditError(
                    ProfileEditErrorCode.INCOMPLETE_READ,
                    skillsRead=len(found),
                    scrolls=_MAX_VIEW_SCROLLS,
                )

        await collect()
        filters = self._page.locator(sel.SKILL_FILTER_BUTTONS)
        for i in range(await filters.count()):
            button = filters.nth(i)
            if await button.get_attribute("aria-current") == "true":
                continue
            await button.click()
            await self._page.wait_for_timeout(_VIEW_SETTLE_MS)
            await collect()
        return [
            Skill(name=name, position=n + 1, ref=ref)
            for n, (ref, name) in enumerate(found.items())
        ]

    # ── writes ──────────────────────────────────────────────────────────────
    async def write_headline(
        self, *, expected: str, value: str, dry_run: bool = False
    ) -> None:
        url = sel.intro_form_url(await self._vanity_name())
        loc = await self._open_form((url,), sel.HEADLINE)
        await self._replace(
            loc,
            expected=expected,
            value=value,
            field="headline",
            url=url,
            dry_run=dry_run,
        )

    async def write_about(
        self, *, expected: str, value: str, dry_run: bool = False
    ) -> None:
        urls = sel.about_form_urls(await self._vanity_name())
        loc = await self._open_form(urls, sel.ABOUT)
        await self._replace(
            loc,
            expected=expected,
            value=value,
            field="about",
            url=self._page.url,
            dry_run=dry_run,
        )

    async def write_experience(
        self,
        experience_id: str,
        *,
        field: Literal["title", "description"],
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        url = sel.experience_form_url(await self._vanity_name(), experience_id)
        spec = sel.EXPERIENCE_TITLE if field == "title" else sel.EXPERIENCE_DESCRIPTION
        loc = await self._open_form((url,), spec)
        await self._replace(
            loc,
            expected=expected,
            value=value,
            field=f"experience {field}",
            url=url,
            dry_run=dry_run,
        )

    # ── name ────────────────────────────────────────────────────────────────
    async def _name_inputs(self, url: str) -> tuple[Locator, Locator]:
        await self._open_dialog(url, "name")
        inputs = self._dialog().locator(sel.NAME_INPUTS)
        try:
            await inputs.first.wait_for(state="visible", timeout=_FIELD_TIMEOUT_MS)
        except Exception:
            pass  # reported below
        if await inputs.count() < 2:
            raise await self._not_found(
                "first/last name", url, "fewer than two required text inputs"
            )
        return inputs.nth(0), inputs.nth(1)

    async def read_name(self) -> tuple[TextField, TextField]:
        url = sel.intro_form_url(await self._vanity_name())
        first, last = await self._name_inputs(url)
        return await self._read(first), await self._read(last)

    async def write_name(
        self,
        which: Literal["first", "last"],
        *,
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        url = sel.intro_form_url(await self._vanity_name())
        first, last = await self._name_inputs(url)
        await self._replace(
            first if which == "first" else last,
            expected=expected,
            value=value,
            field=f"{which} name",
            url=url,
            dry_run=dry_run,
        )

    # ── new entries ─────────────────────────────────────────────────────────
    async def list_entries(self, kind: str) -> list[EntrySummary]:
        """Every entry of a section, identified by LinkedIn's own entity id."""
        if kind == "position":
            return [
                EntrySummary(
                    e.id, tuple(x for x in (e.title, e.company, e.date_range) if x)
                )
                for e in await self.list_experiences()
            ]
        vanity = await self._vanity_name()
        await self._goto(sel.entry_list_url(vanity, kind))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=8)
        items = await self._page.evaluate(
            sel.LIST_ITEMS_JS, sel.ENTRY_EDIT_HREF[kind].pattern
        )
        return [EntrySummary(i["id"], tuple(i["lines"])) for i in items]

    async def _labelled(self, kind: str, name: str, url: str) -> Locator:
        """The one control labelled exactly *label*, or "*label*, <limit note>".

        Some fields' accessible name is their label followed by a stated limit
        ("Description, maximum 1,000 characters"); the match stays anchored to
        the whole label so "Description" never matches another field.
        """
        dialog = self._dialog()
        for text in self._labels.get(f"{kind}.{name}", ()):
            for matcher in (text, re.compile(rf"^{re.escape(text)},")):
                loc = (
                    dialog.get_by_label(matcher, exact=True)
                    if isinstance(matcher, str)
                    else dialog.get_by_label(matcher)
                )
                if await loc.count() == 1:
                    return loc
        raise await self._not_found(f"{kind} {name}", url, "no unique labelled control")

    async def _set_switch(
        self, box: Locator, on: bool, what: str, handle: Locator | None = None
    ) -> None:
        """Set a checkbox/switch and confirm its state; refuse if it will not move.

        LinkedIn's switches render a visible ``div[role="switch"]`` over a
        hidden checkbox: the visible *handle* is clicked when given, and the
        checkbox's own state is what is confirmed either way.
        """
        attempts = [handle.click] if handle is not None else []
        attempts += [
            lambda: box.set_checked(on, force=True),
            lambda: box.evaluate("(el) => el.click()"),
        ]
        for attempt in attempts:
            if await box.is_checked() == on:
                break
            try:
                await attempt()
            except Exception:
                continue
            await self._page.wait_for_timeout(_SETTLE_MS)
        if await box.is_checked() != on:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"Could not set {what}; nothing was saved.",
                control=what,
                wanted=on,
            )

    async def _blur(self) -> None:
        """Close a suggestion list by clicking the dialog heading (Escape would close the dialog)."""
        heading = self._dialog().locator(sel.HEADINGS).first
        if await heading.count():
            await heading.click()

    async def _fill_typeahead(self, loc: Locator, value: str) -> str:
        """Type a value; pick a suggestion only when it is exactly that value."""
        await loc.fill(value[:-1])
        await loc.press_sequentially(value[-1], delay=120)
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await options.first.wait_for(state="visible", timeout=2_500)
        except Exception:
            await self._blur()
            return await loc.input_value()
        for i in range(await options.count()):
            text = normalize_text((await options.nth(i).inner_text()).split("\n")[0])
            if skill_key(text) == skill_key(value):
                await options.nth(i).click()
                await self._page.wait_for_timeout(_SETTLE_MS)
                return await loc.input_value()
        await self._blur()
        return await loc.input_value()

    async def _pick_suggestion(self, loc: Locator, value: str, what: str) -> str:
        """Type *value* and select LinkedIn's suggestion of exactly that name.

        Used where the point of the edit is the link to LinkedIn's own page
        (an organization), so free text is never accepted: without an exact
        suggestion nothing is selected and the offered names are reported.
        """
        await loc.fill(value[:-1])
        await loc.press_sequentially(value[-1], delay=120)
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await options.first.wait_for(state="visible", timeout=_OPTION_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                f"LinkedIn offered no suggestions for {what} '{value}'; nothing was saved.",
                field=what,
            ) from None
        offered: list[str] = []
        for i in range(await options.count()):
            text = normalize_text((await options.nth(i).inner_text()).split("\n")[0])
            offered.append(text)
            if skill_key(text) == skill_key(value):
                await options.nth(i).click()
                await self._page.wait_for_timeout(_SETTLE_MS)
                return await loc.input_value()
        raise ProfileEditError(
            ProfileEditErrorCode.VALIDATION_ERROR,
            f"LinkedIn has no {what} named exactly '{value}'; nothing was saved. "
            "Use one of the offered names.",
            field=what,
            offered=offered,
        )

    _ORG_FIELD = {"position": "company", "education": "school"}

    async def _organization_field(
        self, kind: str, entry_id: str
    ) -> tuple[Locator, str]:
        urls = sel.entry_form_urls(await self._vanity_name(), kind, entry_id)
        last: ProfileEditError | None = None
        for url in urls:
            try:
                await self._open_dialog(url, f"{kind} {entry_id}")
                await self._page.wait_for_timeout(_SETTLE_MS * 2)
                return await self._labelled(kind, self._ORG_FIELD[kind], url), url
            except ProfileEditError as e:
                if e.code is not ProfileEditErrorCode.SELECTOR_NOT_FOUND:
                    raise
                last = e
        assert last is not None
        raise last

    async def read_organization(self, kind: str, entry_id: str) -> TextField:
        loc, _ = await self._organization_field(kind, entry_id)
        return TextField(value=await loc.input_value())

    async def write_organization(
        self,
        kind: str,
        entry_id: str,
        *,
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        """Replace an entry's organization with LinkedIn's own page of that name."""
        loc, url = await self._organization_field(kind, entry_id)
        current = normalize_text(await loc.input_value())
        if current != normalize_text(expected):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                f"The {kind} organization no longer matches the change set; nothing was typed.",
                expected=expected,
                actual=current,
            )
        dialog = self._dialog()
        switches = dialog.locator(sel.NOTIFY_SWITCH)
        handles = dialog.locator(sel.NOTIFY_SWITCH_HANDLE)
        same = await handles.count() == await switches.count()
        for i in range(await switches.count()):
            await self._set_switch(
                switches.nth(i),
                False,
                "notify-your-network switch",
                handles.nth(i) if same else None,
            )
        got = await self._pick_suggestion(loc, value, f"{kind} organization")
        if skill_key(got) != skill_key(value):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The organization field did not keep the selected name; nothing was saved.",
                wanted=value,
                got=got,
            )
        if dry_run:
            await self._leave_unsaved()
            return
        await self._save(f"{kind} organization", url)

    async def _select(self, loc: Locator, wanted: str, what: str) -> str:
        """Choose an option by its value or exact visible text; report the text."""
        options = await loc.evaluate(
            "(s) => [...s.options].map((o) => [o.value, o.text.trim()])"
        )
        for value, text in options:
            if value and (value == wanted or skill_key(text) == skill_key(wanted)):
                await loc.select_option(value=value)
                return text
        raise ProfileEditError(
            ProfileEditErrorCode.VALIDATION_ERROR,
            f"'{wanted}' is not an option for {what}.",
            control=what,
            options=[t for v, t in options if v],
        )

    async def create_entry(
        self, kind: str, values: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Fill an add-entry form with exactly *values*, check it, then save.

        The notify-your-network switch, on by default on these forms, is turned
        off and confirmed off first: adding an approved entry must not also
        broadcast it. Every filled control is read back before Save; any
        difference stops with nothing saved. With *dry_run* the form is left
        unsaved after the read-back.
        """
        specs = {s.name: s for s in ENTRY_SPECS[kind]}
        urls = sel.new_entry_form_urls(await self._vanity_name(), kind)
        opened = None
        for url in urls:
            try:
                await self._open_dialog(url, kind)
                opened = url
                break
            except ProfileEditError as e:
                if e.code is not ProfileEditErrorCode.SELECTOR_NOT_FOUND:
                    raise
        if opened is None:
            raise await self._not_found(
                f"new {kind} form", urls[-1], "no add form opened"
            )
        url = opened
        await self._page.wait_for_timeout(_SETTLE_MS * 2)
        dialog = self._dialog()

        switches = dialog.locator(sel.NOTIFY_SWITCH)
        handles = dialog.locator(sel.NOTIFY_SWITCH_HANDLE)
        same = await handles.count() == await switches.count()
        for i in range(await switches.count()):
            await self._set_switch(
                switches.nth(i),
                False,
                "notify-your-network switch",
                handles.nth(i) if same else None,
            )

        filled: dict[str, Any] = {}
        if kind == "position":
            box = dialog.locator(sel.CURRENT_ROLE_CHECKBOX)
            if await box.count() != 1:
                raise await self._not_found(
                    "current-role checkbox", url, "not exactly one"
                )
            # The input is hidden; its own <label for=…> is the visible box
            # (measured 12 October 2026).
            box_id = await box.get_attribute("id")
            handle = dialog.locator(f'label[for="{box_id}"]') if box_id else None
            if handle is not None and await handle.count() != 1:
                handle = None
            await self._set_switch(
                box, bool(values.get("current")), "current-role checkbox", handle
            )
            filled["current"] = await box.is_checked()

        for name, value in values.items():
            spec = specs[name]
            if spec.control in {"flag", "month", "year"}:
                continue
            if spec.control == "rich":
                loc = dialog.locator(sel.RICH_TEXT)
                if await loc.count() != 1:
                    raise await self._not_found(
                        f"{kind} {name}", url, "no unique rich-text box"
                    )
                await self._type(loc, value)
                got = (await self._read(loc)).value
            elif spec.control == "select":
                loc = await self._labelled(kind, name, url)
                got = await self._select(loc, str(value), f"{kind} {name}")
                filled[name] = got
                continue
            else:
                loc = await self._labelled(kind, name, url)
                limit = await self._limit(loc)
                if limit and len(str(value)) > limit:
                    raise ProfileEditError(
                        ProfileEditErrorCode.VALIDATION_ERROR,
                        f"{kind} {name} is longer than the form allows.",
                        field=name,
                        allowedLength=limit,
                        proposedLength=len(str(value)),
                    )
                if spec.control == "typeahead":
                    got = await self._fill_typeahead(loc, str(value))
                else:
                    await loc.fill(str(value))
                    if spec.control == "date":
                        await self._blur()
                    got = await loc.input_value()
            if normalize_text(got) != normalize_text(str(value)) and not (
                spec.control == "typeahead" and skill_key(got) == skill_key(str(value))
            ):
                raise ProfileEditError(
                    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                    f"The {kind} {name} field did not keep the approved value; nothing was saved.",
                    field=name,
                    wanted=value,
                    got=got,
                )
            filled[name] = got

        selects = dialog.locator("select")
        for side, idx in (("start", 0), ("end", 1)):
            # Looked up again per side: the end dropdowns' options follow the
            # chosen start year.
            dates = await self._page.evaluate(sel.DATE_SELECTS_JS)
            for unit, key in (("Year", "years"), ("Month", "months")):
                name = f"{side}{unit}"
                if name not in values:
                    continue
                if idx >= len(dates[key]):
                    err = await self._not_found(
                        f"{kind} {name}", url, "date dropdown not present"
                    )
                    err.details["dateSelects"] = dates
                    err.details["selects"] = await self._page.evaluate(
                        sel.DESCRIBE_SELECTS_JS
                    )
                    raise err
                loc = selects.nth(dates[key][idx])
                await loc.select_option(value=str(values[name]))
                got = await loc.input_value()
                if got != str(values[name]):
                    raise ProfileEditError(
                        ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                        f"The {kind} {name} did not take the approved value; nothing was saved.",
                        field=name,
                        wanted=values[name],
                        got=got,
                    )
                filled[name] = int(got)
        # A month left over from LinkedIn's defaults (the position form starts
        # at the current month) is cleared when no month was approved.
        for side, idx in (("start", 0), ("end", 1)):
            if (
                f"{side}Year" in values
                and f"{side}Month" not in values
                and idx < len(dates["months"])
            ):
                loc = selects.nth(dates["months"][idx])
                if await loc.input_value():
                    await loc.select_option(value="")

        for i in range(await switches.count()):
            if await switches.nth(i).is_checked():
                raise ProfileEditError(
                    ProfileEditErrorCode.VALIDATION_ERROR,
                    "The notify-your-network switch turned itself back on; nothing was saved.",
                )
        if dry_run:
            await self._leave_unsaved()
            return filled
        await self._save(f"new {kind}", url)
        return filled

    async def _phase(
        self, step: str, awaitable: Any, *, limit: float = _PHASE_LIMIT_S
    ) -> Any:
        """Run one step of a write with a hard time limit, and log slow steps.

        A step that never finishes must not hang the tool: it stops with the
        step's name instead. Steps over _SLOW_PHASE_S are logged at WARNING,
        which the server writes at its default log level.
        """
        started = time.monotonic()
        try:
            return await asyncio.wait_for(awaitable, timeout=limit)
        except TimeoutError:
            logger.warning("Profile edit step %r exceeded %.0fs; stopped", step, limit)
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The step '{step}' took longer than {limit:.0f}s and was stopped."
                + (
                    " The save may or may not have gone through; check the profile."
                    if step == "save"
                    else " Nothing was saved."
                ),
                step=step,
            ) from None
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_PHASE_S:
                logger.warning("Profile edit step %r took %.1fs", step, elapsed)

    async def add_skill(self, name: str) -> str:
        url = sel.new_skill_form_url(await self._vanity_name())
        box = await self._phase(
            "open skill form", self._open_form((url,), sel.SKILL_INPUT)
        )
        # Typing a whole name key by key lost characters while the suggestion
        # list re-rendered ("Large LanguagModels"). Set all but the last
        # character at once, type the last to trigger suggestions, and only
        # continue when the box holds exactly the intended name.
        await self._phase("type skill", box.fill(name[:-1]))
        await self._phase("type skill", box.press_sequentially(name[-1], delay=120))
        typed = await self._phase("read skill box", box.input_value())
        if typed != name:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The skill box did not take the exact name; nothing was added.",
                skill=name,
                typed=typed,
            )
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await self._phase(
                "wait for suggestions",
                options.first.wait_for(state="visible", timeout=_OPTION_TIMEOUT_MS),
            )
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.SKILL_NOT_FOUND,
                f"LinkedIn offered no suggestions for '{name}'.",
                skill=name,
            ) from None

        async def offered_names() -> list[str]:
            return [
                normalize_text(t.split("\n")[0])
                for t in await options.all_inner_texts()
            ]

        offered = await self._phase("read suggestions", offered_names())
        for i, text in enumerate(offered):
            if skill_key(text) == skill_key(name):
                await self._phase("pick suggestion", options.nth(i).click())
                await self._phase("notify check", self._refuse_if_notifying("skill"))
                await self._phase("save", self._save("skill", url), limit=60)
                return text
        raise ProfileEditError(
            ProfileEditErrorCode.SKILL_NOT_FOUND,
            f"LinkedIn has no skill named exactly '{name}'. Nothing was added; "
            "choose one of the offered names.",
            skill=name,
            offered=offered,
        )

    async def remove_skill(self, skill: Skill) -> None:
        if not skill.ref:
            raise ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD,
                "This skill has no edit control.",
                skill=skill.name,
            )
        url = sel.skill_form_url(await self._vanity_name(), skill.ref)
        await self._open_dialog(url, "skill")
        dialog = self._dialog()
        # The dialog names its skill only in its heading ("Edit React.js").
        heading = normalize_text(await dialog.locator(sel.HEADINGS).first.inner_text())
        if not skill_key(heading).endswith(skill_key(skill.name)):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                "The skill form is for a different skill; nothing was deleted.",
                expected=skill.name,
                actual=heading,
            )
        await self._refuse_if_notifying("skill")
        delete = await self._button("delete_skill")
        if delete is None:
            raise await self._not_found(
                "delete skill button", url, "no unique delete control"
            )
        await delete.click()
        confirm = await self._button("confirm_delete", self._page.locator(sel.DIALOG))
        if confirm is None:
            raise await self._not_found(
                "delete confirmation", url, "no unique confirm control"
            )
        await confirm.click()
        try:
            await self._dialog().wait_for(state="hidden", timeout=_SAVE_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                "The skill dialog did not close after deleting.",
                skill=skill.name,
            ) from None


def _summary(item: dict[str, Any]) -> ExperienceSummary:
    """Best-effort display fields from a position's list text.

    Used to show and match experiences, never to identify one for a write (the
    position id does that) and never as a "before" value (the form does that).
    """
    lines: list[str] = item.get("lines") or []
    group: list[str] = item.get("groupLines") or []
    title = lines[0] if lines else ""
    company = employment = date_range = location = None
    rest = lines[1:]
    if group:  # a role grouped under its company: the group's first line is the company
        company = group[0]
    elif rest:
        company, _, employment = (s.strip() for s in rest.pop(0).partition("·"))
        employment = employment or None
    for i, line in enumerate(rest):
        if _DATE_RANGE.search(line):
            date_range = line
            if i + 1 < len(rest) and not _DATE_RANGE.search(rest[i + 1]):
                location = rest[i + 1]
            break
    if location in rest:
        preview_from = rest.index(location) + 1
    elif date_range in rest:
        preview_from = rest.index(date_range) + 1
    else:
        preview_from = len(rest)
    preview = " ".join(rest[preview_from:])[:280] or None
    return ExperienceSummary(
        id=item["id"],
        title=title,
        company=company,
        employment_type=employment,
        date_range=date_range,
        location=location,
        description_preview=preview,
        editable=True,
    )
