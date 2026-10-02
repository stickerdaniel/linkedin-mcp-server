"""Read and write the signed-in member's own profile through LinkedIn's edit forms.

Implements ``profile_edit.service.ProfileEditorPort``. Each write opens the
field's own edit form by URL, checks that the visible value is still the
expected "before" value, changes only that control, saves, and waits for the
dialog to close. It never reports success itself: the service re-reads the
field afterwards and compares. Locators all come from ``profile_selectors``.
"""

from __future__ import annotations

from typing import Any, Literal

import logging
import re

from patchright.async_api import Locator, Page

import linkedin_mcp_server.linkedin.profile_selectors as sel
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    ExperienceForm,
    ExperienceSummary,
    Skill,
    TextField,
    normalize_text,
)
from linkedin_mcp_server.profile_edit.changeset import skill_key

logger = logging.getLogger(__name__)

_DIALOG_TIMEOUT_MS = 10_000
_SAVE_TIMEOUT_MS = 15_000
_OPTION_TIMEOUT_MS = 6_000
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

    async def _open_form(self, urls: tuple[str, ...], spec: sel.FieldSpec) -> Locator:
        """Navigate to the first URL whose dialog contains *spec*; return the field."""
        last: ProfileEditError | None = None
        for url in urls:
            await self._goto(url)
            try:
                await self._page.locator(sel.DIALOG).first.wait_for(
                    state="visible", timeout=_DIALOG_TIMEOUT_MS
                )
            except Exception:
                last = await self._not_found(spec.name, url, "edit dialog did not open")
                continue
            try:
                return await self._field(spec, url)
            except ProfileEditError as e:
                last = e
        raise last or await self._not_found(
            spec.name, urls[-1], "no URL produced the form"
        )

    async def _field(self, spec: sel.FieldSpec, url: str) -> Locator:
        for css in spec.css:
            loc = self._page.locator(css)
            if await loc.count() == 1:
                return loc
        if spec.only_textarea:
            loc = self._page.locator(f"{sel.DIALOG} textarea")
            if await loc.count() == 1:
                return loc
        if spec.label_key and (text := self._labels.get(spec.label_key)):
            loc = self._page.locator(sel.DIALOG).get_by_label(text, exact=True)
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
    async def _read(self, loc: Locator) -> TextField:
        tag = await loc.evaluate("(el) => el.tagName.toLowerCase()")
        value = (
            await loc.input_value()
            if tag in {"input", "textarea"}
            else await loc.inner_text()
        )
        raw_max = await loc.get_attribute("maxlength")
        max_length = (
            int(raw_max)
            if raw_max and raw_max.isdigit()
            else await self._counter_limit()
        )
        return TextField(value=value, max_length=max_length)

    async def _counter_limit(self) -> int | None:
        try:
            text = await self._page.locator(sel.DIALOG).first.inner_text()
        except Exception:
            return None
        m = sel.COUNTER.search(text)
        if not m:
            return None
        digits = re.sub(r"\D", "", m.group(2))
        return int(digits) if digits else None

    async def _replace(
        self, loc: Locator, *, expected: str, value: str, field: str, url: str
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
        await loc.fill(value)
        typed = normalize_text((await self._read(loc)).value)
        if typed != normalize_text(value):
            raise ProfileEditError(
                ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                f"The {field} field did not accept the full value (it may enforce a shorter limit); nothing was saved.",
                field=field,
                typedLength=len(typed),
                wantedLength=len(value),
            )
        await self._save(field, url)

    async def _save(self, field: str, url: str) -> None:
        save = None
        for css in sel.SAVE_BUTTON:
            loc = self._page.locator(css)
            if await loc.count() == 1:
                save = loc
                break
        if save is None:
            loc = self._page.locator(sel.DIALOG).get_by_role(
                "button", name=self._labels["save"], exact=True
            )
            if await loc.count() == 1:
                save = loc
        if save is None:
            raise await self._not_found(
                f"{field} save button", url, "no unique save control"
            )
        await save.click()
        try:
            await self._page.locator(sel.DIALOG).first.wait_for(
                state="detached", timeout=_SAVE_TIMEOUT_MS
            )
        except Exception:
            errors: list[str] = []
            for css in sel.FORM_ERROR:
                for i in range(await self._page.locator(css).count()):
                    t = (await self._page.locator(css).nth(i).inner_text()).strip()
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

    # ── reads ───────────────────────────────────────────────────────────────
    async def read_identity(self) -> tuple[str, str | None, str | None]:
        vanity = await self._vanity_name()
        name = None
        h1 = self._page.locator("main h1")
        if await h1.count():
            name = (await h1.first.inner_text()).strip() or None
        return sel.profile_url(vanity), name, None

    async def read_headline(self) -> TextField:
        vanity = await self._vanity_name()
        loc = await self._open_form((sel.intro_form_url(vanity),), sel.HEADLINE)
        field = await self._read(loc)
        for css in sel.LOCATION.css:
            candidate = self._page.locator(css)
            if await candidate.count() == 1:
                self._location = normalize_text(await candidate.input_value()) or None
                break
        return field

    async def read_location(self) -> str | None:
        return self._location

    async def read_about(self) -> TextField:
        vanity = await self._vanity_name()
        return await self._read(
            await self._open_form(sel.about_form_urls(vanity), sel.ABOUT)
        )

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
        for css in sel.EXPERIENCE_COMPANY.css:
            candidate = self._page.locator(css)
            if await candidate.count() == 1:
                company = normalize_text(await candidate.input_value()) or None
                break
        return ExperienceForm(
            id=experience_id, title=title, description=description, company=company
        )

    async def list_skills(self) -> list[Skill]:
        vanity = await self._vanity_name()
        await self._goto(sel.skills_list_url(vanity))
        await self._session.scroll_body(pause_time=0.8, max_scrolls=10)
        items = await self._page.evaluate(
            sel.LIST_ITEMS_JS, sel.SKILL_EDIT_HREF.pattern
        )
        return [
            Skill(name=item["lines"][0], position=i + 1, ref=item["id"])
            for i, item in enumerate(items)
            if item["lines"]
        ]

    # ── writes ──────────────────────────────────────────────────────────────
    async def write_headline(self, *, expected: str, value: str) -> None:
        url = sel.intro_form_url(await self._vanity_name())
        await self._replace(
            await self._open_form((url,), sel.HEADLINE),
            expected=expected,
            value=value,
            field="headline",
            url=url,
        )

    async def write_about(self, *, expected: str, value: str) -> None:
        urls = sel.about_form_urls(await self._vanity_name())
        await self._replace(
            await self._open_form(urls, sel.ABOUT),
            expected=expected,
            value=value,
            field="about",
            url=self._page.url,
        )

    async def write_experience(
        self,
        experience_id: str,
        *,
        field: Literal["title", "description"],
        expected: str,
        value: str,
    ) -> None:
        url = sel.experience_form_url(await self._vanity_name(), experience_id)
        spec = sel.EXPERIENCE_TITLE if field == "title" else sel.EXPERIENCE_DESCRIPTION
        await self._replace(
            await self._open_form((url,), spec),
            expected=expected,
            value=value,
            field=f"experience {field}",
            url=url,
        )

    async def add_skill(self, name: str) -> str:
        url = sel.new_skill_form_url(await self._vanity_name())
        box = await self._open_form((url,), sel.SKILL_INPUT)
        await box.fill("")
        await box.press_sequentially(name, delay=60)
        options = self._page.locator(sel.TYPEAHEAD_OPTION)
        try:
            await options.first.wait_for(state="visible", timeout=_OPTION_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.SKILL_NOT_FOUND,
                f"LinkedIn offered no suggestions for '{name}'.",
                skill=name,
            ) from None
        offered: list[str] = []
        for i in range(await options.count()):
            text = normalize_text((await options.nth(i).inner_text()).split("\n")[0])
            offered.append(text)
            if skill_key(text) == skill_key(name):
                await options.nth(i).click()
                await self._save("skill", url)
                return text
        raise ProfileEditError(
            ProfileEditErrorCode.SKILL_NOT_FOUND,
            f"LinkedIn has no skill named exactly '{name}'. Nothing was added; choose one of the offered names.",
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
        box = await self._open_form((url,), sel.SKILL_INPUT)
        shown = normalize_text(await box.input_value())
        if shown and skill_key(shown) != skill_key(skill.name):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                "The skill form shows a different skill; nothing was deleted.",
                expected=skill.name,
                actual=shown,
            )
        delete = self._page.locator(sel.DIALOG).get_by_role(
            "button", name=self._labels["delete_skill"], exact=True
        )
        if await delete.count() != 1:
            raise await self._not_found(
                "delete skill button", url, "no unique delete control"
            )
        await delete.click()
        confirm = self._page.get_by_role("alertdialog").get_by_role(
            "button", name=self._labels["confirm_delete"], exact=True
        )
        if await confirm.count() != 1:
            confirm = self._page.locator(sel.DIALOG).last.get_by_role(
                "button", name=self._labels["confirm_delete"], exact=True
            )
        if await confirm.count() != 1:
            raise await self._not_found(
                "delete confirmation", url, "no unique confirm control"
            )
        await confirm.click()
        try:
            await self._page.locator(sel.DIALOG).first.wait_for(
                state="detached", timeout=_SAVE_TIMEOUT_MS
            )
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
    preview_from = (
        (rest.index(location) + 1)
        if location in rest
        else (rest.index(date_range) + 1 if date_range in rest else len(rest))
    )
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
