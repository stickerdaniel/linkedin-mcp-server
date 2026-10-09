"""Browser-DOM tests for the own-profile editor.

The editor's locators, form reads, save handling and typeahead selection run
in headless Chromium against synthetic pages routed under www.linkedin.com. No
request leaves the test browser. The markup reproduces the structure measured
on LinkedIn's own edit forms on 2 October 2026 (see ``profile_selectors``):
native ``<dialog>`` elements beside hidden ad dialogs, rich-text boxes,
inputs labelled through ``aria-labelledby``, a text-only Save button, a
notify-your-network switch, and list items with a content link and a pencil
link to the same edit form.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from patchright.async_api import Page, Route, async_playwright

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.profile_editor import ProfileEditor
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import normalize_text
from linkedin_mcp_server.profile_edit.service import (
    ExperienceEdit,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

BASE = "https://www.linkedin.com"

STATE_JS = """
const S = Object.assign({
  headline: 'Senior Software Developer', country: 'United Kingdom',
  about: 'I build web applications.\\n\\nMostly React.',
  title103: 'Lead Developer', desc103: '', notify: false,
  skills: ['jQuery', 'Python'], failSave: false, noHeadlineField: false,
  fillDelay: 150, endless: false,
}, JSON.parse(localStorage.getItem('S') || '{}'));
const save = () => localStorage.setItem('S', JSON.stringify(S));
const closeAll = () => document.querySelectorAll('dialog[open]').forEach((d) => d.close());
const rich = (el) => el.innerText.replace(/\\n$/, '');
"""

# An ad menu LinkedIn keeps in the page as a closed <dialog> with its own form.
AD_DIALOG = '<dialog class="ad"><form><input type="radio" name="r"><button type="submit">Submit</button></form></dialog>'


def page_html(body: str, script: str = "") -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>Jane Doe | LinkedIn</title></head>'
        f"<body><main>{body}</main>{AD_DIALOG}<script>{STATE_JS}{script}</script></body></html>"
    )


def form(heading: str, fields: str, on_save: str, extra: str = "") -> str:
    return page_html(
        f'<dialog id="d"><h2>{heading}</h2>{fields}<div class="err"></div>'
        '<button type="button" id="save">Save</button></dialog>',
        extra
        + """
        document.getElementById('d').show();
        document.getElementById('save').addEventListener('click', () => {
          if (S.failSave) {
            document.querySelector('.err').innerHTML = '<p role="alert">Something went wrong. Please try again.</p>';
            return;
          }
        """
        + on_save
        + " save(); closeAll(); });",
    )


INTRO = form(
    "Edit intro",
    '<span id="hl"></span><input aria-label="Country/Region*" id="cr">',
    "S.headline = rich(document.querySelector('[role=textbox]'));",
    """
    if (!S.noHeadlineField) document.getElementById('hl').outerHTML =
      '<div role="textbox" contenteditable="true"></div>';
    const hb = document.querySelector('[role=textbox]');
    if (hb) setTimeout(() => { hb.innerText = S.headline; }, S.fillDelay);  // fills after mounting
    document.getElementById('cr').value = S.country;
    """,
)
ABOUT = form(
    "Edit about",
    '<div role="textbox" contenteditable="true" aria-label="About"></div>',
    "S.about = rich(document.querySelector('[role=textbox]'));",
    "document.querySelector('[role=textbox]').innerText = S.about;",
)
POSITION_103 = form(
    "Edit experience",
    '<input type="checkbox" role="switch" id="notify">'
    '<span id="lt">Title*</span><input id="t" aria-labelledby="lt" maxlength="100">'
    '<span id="lc">Company or organization*</span><input id="c" aria-labelledby="lc" value="Liftango">'
    '<div role="textbox" contenteditable="true" aria-label="Description, maximum 2,000 characters"></div>',
    "S.title103 = document.getElementById('t').value; S.desc103 = rich(document.querySelector('[role=textbox]'));",
    """
    document.getElementById('notify').checked = S.notify;
    document.getElementById('t').value = S.title103;
    document.querySelector('[role=textbox]').innerText = S.desc103;
    """,
)
EXPERIENCE_LIST = page_html(
    '<div id="list"></div>',
    """
    const item = (id, lines) => `<div><a href="/in/jane/details/experience/edit/forms/${id}/">`
      + lines.map((l) => `<div>${l}</div>`).join('')
      + `</a><a href="/in/jane/details/experience/edit/forms/${id}/" aria-label="Edit ${lines[0]}"><span>Edit ${lines[0]}</span></a></div>`;
    document.getElementById('list').innerHTML = '<h2>Experience</h2>'
      + item(103, [S.title103, 'Liftango · Full-time', 'Jul 2023 - Jun 2024 · 1 yr', 'Remote'])
      + item(104, ['Senior Frontend Developer', 'Equator · Full-time', 'May 2022 - Jul 2023 · 1 yr 3 mos', 'Glasgow City, Scotland, United Kingdom', 'React.js'])
      + '<ul><li><div>IPG Automotive</div><div>Full-time · 3 yrs</div><ul>'
      + '<li><a href="/in/jane/details/experience/edit/forms/101/"><div>Senior Developer</div><div>Aug 2024 - Mar 2025 · 8 mos</div></a></li>'
      + '<li><a href="/in/jane/details/experience/edit/forms/102/"><div>Developer</div><div>Mar 2021 - Dec 2022 · 2 yrs</div></a></li>'
      + '</ul></li></ul>';
    """,
)
SKILLS_LIST = page_html(
    '<ul id="filters"><li><button aria-current="true" data-v="all">All</button></li>'
    '<li><button data-v="tools">Tools &amp; Technologies</button></li></ul><div id="skills"></div>',
    """
    // Like LinkedIn: the default view shows a bounded subset; a category view
    // shows skills the default view leaves out.
    const TOOLS = ['React.js'];
    const show = (view) => {
      const list = view === 'all' ? S.skills.slice(0, 2) : TOOLS.concat(S.skills.slice(2));
      document.getElementById('skills').innerHTML = list.map((s) => {
        const i = view === 'all' || !TOOLS.includes(s) ? S.skills.indexOf(s) : 900;
        return `<div><div>${s}</div><a href="/in/jane/details/skills/edit/forms/${1000 + i}/" aria-label="Edit ${s}"><span>Edit ${s}</span></a></div>`;
      }).join('');
      for (const b of document.querySelectorAll('#filters button')) b.toggleAttribute('aria-current', b.dataset.v === view);
    };
    for (const b of document.querySelectorAll('#filters button')) b.addEventListener('click', () => show(b.dataset.v));
    show('all');
    if (S.endless) {
      // A view that never stops loading: every scroll appends another batch.
      let n = 0;
      const more = () => {
        const html = Array.from({length: 5}, () => { n += 1;
          return `<div style="height:400px"><div>Skill ${n}</div><a href="/in/jane/details/skills/edit/forms/${5000 + n}/" aria-label="Edit"><span>Edit</span></a></div>`; }).join('');
        document.getElementById('skills').insertAdjacentHTML('beforeend', html);
      };
      more();
      window.addEventListener('scroll', more);
    }
    """,
)
NEW_SKILL = form(
    "Add skill",
    '<input type="checkbox" role="switch" id="notify">'
    '<input aria-label="Skill*" placeholder="Skill (ex: Project Management)" id="sk">'
    '<div role="listbox" id="lb"></div><input type="checkbox" aria-label="Senior Developer at IPG">',
    "S.skills.push(document.getElementById('sk').value);",
    """
    document.getElementById('notify').checked = S.notify;
    const CAT = ['ReactJS', 'React Native', 'TypeScript', 'Node.js'];
    const box = document.getElementById('sk');
    box.addEventListener('input', () => {
      const q = box.value.toLowerCase();
      document.getElementById('lb').innerHTML = q.length < 2 ? '' :
        CAT.filter((c) => c.toLowerCase().includes(q)).map((c) => `<div role="option">${c}</div>`).join('');
      for (const o of document.querySelectorAll('[role=option]')) o.addEventListener('click', () => { box.value = o.textContent; });
    });
    """,
)

PAGES: dict[str, str] = {
    "/in/jane/": page_html("<h2>Profile</h2>"),
    "/in/jane/edit/intro/": INTRO,
    "/in/jane/edit/forms/summary/new/": ABOUT,
    "/in/jane/details/experience/": EXPERIENCE_LIST,
    "/in/jane/details/experience/edit/forms/103/": POSITION_103,
    "/in/jane/details/skills/": SKILLS_LIST,
    "/in/jane/skills/edit/forms/new/": NEW_SKILL,
}


def skill_form(i: int) -> str:
    return page_html(
        '<dialog id="d"><h2 id="h"></h2><input type="checkbox" aria-label="Engineer at Arm">'
        '<button type="button" id="del">Delete skill</button><button type="button">Save</button></dialog>'
        '<dialog id="confirm"><p>Delete skill?</p><button type="button" id="yes">Delete</button>'
        '<button type="button">Cancel</button></dialog>',
        f"""
        document.getElementById('h').textContent = 'Edit ' + S.skills[{i}];
        document.getElementById('d').show();
        document.getElementById('del').addEventListener('click', () => document.getElementById('confirm').show());
        document.getElementById('yes').addEventListener('click', () => {{ S.skills.splice({i}, 1); save(); closeAll(); }});""",
    )


async def _route(route: Route) -> None:
    url = route.request.url
    path = url.removeprefix(BASE).split("?")[0]
    if not url.startswith(BASE):
        await route.abort()  # hermetic: nothing leaves the test browser
    elif path == "/in/me/":
        # A client-side redirect: Playwright does not route the request that
        # follows an HTTP redirect, so a 302 here would reach the real site.
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body="<script>location.replace('/in/jane/')</script>",
        )
    elif path in PAGES:
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=PAGES[path]
        )
    elif path.startswith("/in/jane/details/skills/edit/forms/1"):
        i = int(path.split("/")[-2]) - 1000
        await route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=skill_form(i)
        )
    else:
        await route.fulfill(
            status=404,
            content_type="text/html; charset=utf-8",
            body=page_html("<h2>Not found</h2>"),
        )


class _Navigator(PageNavigator):
    """Plain navigation: the auth-barrier checks are covered by navigation's own tests."""

    async def _navigate_to_page(self, url: str) -> None:
        page = self._session.page
        await page.goto(url, wait_until="domcontentloaded")
        if url.endswith("/in/me/"):
            await page.wait_for_url(f"{BASE}/in/jane/")


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        context = await browser.new_context()
        await context.route("**/*", _route)
        p = await context.new_page()
        yield p
        await browser.close()


async def set_state(page: Page, **values: object) -> None:
    await page.goto(f"{BASE}/in/jane/")
    await page.evaluate(
        "(v) => localStorage.setItem('S', JSON.stringify(Object.assign(JSON.parse(localStorage.getItem('S') || '{}'), v)))",
        values,
    )


async def stored(page: Page) -> dict:
    return json.loads(await page.evaluate("localStorage.getItem('S') || '{}'"))


class _FastSession(PageSession):
    """No navigation pacing, so the suite stays quick; pacing is a unit-test concern."""

    async def delay(self, seconds: float) -> None:
        return None


def editor(page: Page) -> ProfileEditor:
    session = _FastSession(page)
    return ProfileEditor(session, _Navigator(session))


class TestReads:
    async def test_identity_headline_and_location_ignore_the_hidden_ad_dialog(
        self, page
    ):
        ed = editor(page)
        assert await ed.read_identity() == (f"{BASE}/in/jane/", "Jane Doe", None)
        h = await ed.read_headline()
        assert h.value == "Senior Software Developer", (
            "read after the editor filled itself"
        )
        assert (h.max_length, h.limit("headline")) == (None, 220)
        assert await ed.read_location() == "United Kingdom"

    async def test_about_is_read_from_its_rich_text_box(self, page):
        about = await editor(page).read_about()
        assert about.value == "I build web applications.\n\nMostly React."

    async def test_a_limit_stated_in_the_label_is_used(self, page):
        form = await editor(page).read_experience("103")
        assert (form.description.max_length, form.title.max_length) == (2000, 100)
        assert (form.title.value, form.company) == ("Lead Developer", "Liftango")

    async def test_experiences_keep_their_text_and_linkedins_position_ids(self, page):
        listed = {e.id: e for e in await editor(page).list_experiences()}
        assert set(listed) == {"101", "102", "103", "104"}
        e = listed["104"]
        assert (e.title, e.company, e.employment_type, e.date_range, e.location) == (
            "Senior Frontend Developer",
            "Equator",
            "Full-time",
            "May 2022 - Jul 2023 · 1 yr 3 mos",
            "Glasgow City, Scotland, United Kingdom",
        )
        assert (listed["102"].title, listed["102"].company) == (
            "Developer",
            "IPG Automotive",
        )

    async def test_skills_are_listed_in_order_without_the_pencil_labels(self, page):
        skills = await editor(page).list_skills()
        assert [(s.name, s.position, s.ref) for s in skills] == [
            ("jQuery", 1, "1000"),
            ("Python", 2, "1001"),
            ("React.js", 3, "1900"),
        ], "skills only a category view shows are included, each once"


class TestReviewFindings:
    """Regression tests for the findings in the first review of this feature."""

    async def test_a_slow_editor_is_not_read_as_empty(self, page):
        await set_state(page, fillDelay=2500)
        assert (await editor(page).read_headline()).value == "Senior Software Developer"

    async def test_an_empty_field_is_still_read_as_empty(self, page):
        form = await editor(page).read_experience("103")
        assert form.description.value.strip() == ""

    async def test_a_skills_view_that_never_ends_is_an_incomplete_read(self, page):
        await set_state(page, endless=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).list_skills()
        assert e.value.code is ProfileEditErrorCode.INCOMPLETE_READ

    async def test_adding_a_skill_never_notifies_the_network(self, page):
        await set_state(page, notify=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).add_skill("reactjs")
        assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
        assert (await stored(page)).get("skills", ["jQuery", "Python"]) == [
            "jQuery",
            "Python",
        ]

    async def test_the_account_is_the_signed_in_profile(self, page):
        assert await editor(page).account() == f"{BASE}/in/jane/"


class TestWrites:
    async def test_a_headline_write_saves_only_after_checking_the_visible_value(
        self, page
    ):
        await editor(page).write_headline(
            expected="Senior Software Developer", value="Senior Product Engineer"
        )
        assert (await stored(page))["headline"] == "Senior Product Engineer"
        assert (await editor(page).read_headline()).value == "Senior Product Engineer"

    async def test_paragraphs_survive_a_rich_text_write(self, page):
        text = "First paragraph.\n\nSecond paragraph.\nWith a line break."
        await editor(page).write_about(
            expected="I build web applications.\n\nMostly React.", value=text
        )
        assert normalize_text((await editor(page).read_about()).value) == text

    async def test_a_changed_visible_value_stops_before_typing(self, page):
        await set_state(page, headline="Edited by hand")
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_headline(
                expected="Senior Software Developer", value="New"
            )
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        assert (await stored(page))["headline"] == "Edited by hand"

    async def test_a_refused_save_reports_linkedins_message(self, page):
        await set_state(page, failSave=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_headline(
                expected="Senior Software Developer", value="New"
            )
        assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
        assert e.value.details["formErrors"] == [
            "Something went wrong. Please try again."
        ]

    async def test_a_field_that_enforces_a_shorter_limit_is_never_saved_truncated(
        self, page
    ):
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_experience(
                "103", field="title", expected="Lead Developer", value="x" * 120
            )
        assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
        assert e.value.details["typedLength"] == 100
        assert "title103" not in await stored(page)

    async def test_a_form_that_would_notify_the_network_is_never_saved(self, page):
        await set_state(page, notify=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_experience(
                "103", field="description", expected="", value="New"
            )
        assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
        assert "desc103" not in await stored(page)

    async def test_a_missing_field_fails_with_diagnostics_and_clicks_nothing(
        self, page
    ):
        await set_state(page, noHeadlineField=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_headline(
                expected="Senior Software Developer", value="New"
            )
        assert e.value.code is ProfileEditErrorCode.SELECTOR_NOT_FOUND
        dialog = e.value.details["dialog"]
        assert dialog["heading"] == "Edit intro"
        assert "Country/Region*" in {c["ariaLabel"] for c in dialog["controls"]}
        assert "headline" not in await stored(page)

    async def test_an_experience_description_is_edited_by_position_id(self, page):
        await editor(page).write_experience(
            "103",
            field="description",
            expected="",
            value="Led the routing platform.\n\nTypeScript, Node.js.",
        )
        form = await editor(page).read_experience("103")
        assert (
            normalize_text(form.description.value)
            == "Led the routing platform.\n\nTypeScript, Node.js."
        )

    async def test_a_skill_is_added_only_on_an_exact_typeahead_match(self, page):
        assert await editor(page).add_skill("reactjs") == "ReactJS"
        assert (await stored(page))["skills"] == ["jQuery", "Python", "ReactJS"]
        with pytest.raises(ProfileEditError) as e:
            await editor(page).add_skill("React")
        assert e.value.code is ProfileEditErrorCode.SKILL_NOT_FOUND
        assert set(e.value.details["offered"]) == {"ReactJS", "React Native"}
        assert (await stored(page))["skills"] == ["jQuery", "Python", "ReactJS"]

    async def test_a_skill_is_removed_only_from_its_own_form(self, page):
        ed = editor(page)
        [jquery, python, *_] = await ed.list_skills()
        with pytest.raises(ProfileEditError) as e:  # the form at this ref is Python's
            await editor(page).remove_skill(
                type(jquery)(name="jQuery", position=1, ref=python.ref)
            )
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        await editor(page).remove_skill(jquery)
        assert [s.name for s in await editor(page).list_skills()] == [
            "Python",
            "React.js",
        ]


class TestEndToEnd:
    async def test_propose_apply_verify_then_stale_on_a_manual_edit(
        self, page, tmp_path: Path
    ):
        store = ProfileEditStore(tmp_path)

        def svc(writes: bool = True) -> ProfileEditService:
            return ProfileEditService(
                editor(page), store, writes_enabled=lambda: writes, pacing_seconds=0
            )

        cs = await svc().propose(
            Proposal(
                headline="Senior Product Engineer | React, TypeScript",
                about="New about.\n\nSecond paragraph.",
                skills_add=["TypeScript"],
            )
        )
        assert (await stored(page)).get("headline") is None, "proposing writes nothing"
        with pytest.raises(ProfileEditError):
            await svc(writes=False).apply(cs["changeSetId"], confirm=True)
        out = await svc().apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED" and all(r["verified"] for r in out["results"])
        s = await stored(page)
        assert (s["headline"], s["skills"][-1]) == (
            "Senior Product Engineer | React, TypeScript",
            "TypeScript",
        )

        second = await svc().propose(
            Proposal(
                experiences=[ExperienceEdit(experience_id="103", title="Lead Engineer")]
            )
        )
        await set_state(page, title103="Changed by hand")
        with pytest.raises(ProfileEditError) as e:
            await svc().apply(second["changeSetId"], confirm=True)
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        assert (await stored(page))["title103"] == "Changed by hand"
