"""Browser-DOM tests for the own-profile editor.

The editor's locators, form reads, save handling and typeahead selection run
in headless Chromium against synthetic edit forms routed under
www.linkedin.com. No LinkedIn request is made. The markup is a claim about the
algorithm (dialog-scoped fields found by id fragment, label, or structure), not
a copy of LinkedIn's page; the live calibration is documented in the README.
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
  headline: 'Senior Software Developer', city: 'Edinburgh', about: 'I build web applications.',
  title103: 'Lead Developer', desc103: 'Routing platform.', skills: ['jQuery', 'Python'],
  failSave: false, noHeadlineField: false,
}, JSON.parse(localStorage.getItem('S') || '{}'));
const save = () => localStorage.setItem('S', JSON.stringify(S));
const closeDialog = () => document.querySelector('[role=dialog]').remove();
"""


def page_html(body: str, script: str = "") -> str:
    return f"<!doctype html><html><body><main>{body}</main><script>{STATE_JS}{script}</script></body></html>"


def dialog(fields: str, on_submit: str) -> str:
    return page_html(
        f'<div role="dialog"><form id="f">{fields}<div class="err"></div>'
        '<button type="submit">Save</button></form></div>',
        """
        document.getElementById('f').addEventListener('submit', (e) => {
          e.preventDefault();
          if (S.failSave) {
            document.querySelector('.err').innerHTML = '<p role="alert">Something went wrong. Please try again.</p>';
            return;
          }
        """
        + on_submit
        + "save(); closeDialog(); });",
    )


PAGES: dict[str, str] = {
    "/in/jane/": page_html("<h1>Jane Doe</h1>"),
    "/in/jane/edit/intro/": dialog(
        '<input id="pe-firstName" value="Jane"><span id="hl"></span>'
        '<input id="pe-geoLocation-city">',
        "S.headline = document.querySelector('[id$=-headline]').value;",
    ).replace(
        "</script>",
        """
        if (!S.noHeadlineField) document.getElementById('hl').outerHTML =
          '<label for="pe-headline">Headline</label><textarea id="pe-headline" maxlength="220"></textarea>';
        if (document.getElementById('pe-headline')) document.getElementById('pe-headline').value = S.headline;
        document.getElementById('pe-geoLocation-city').value = S.city;
        </script>""",
    ),
    "/in/jane/edit/about/": dialog(
        '<textarea id="pe-summary"></textarea><span>25/2,600</span>',
        "S.about = document.getElementById('pe-summary').value;",
    ).replace(
        "</script>", "document.getElementById('pe-summary').value = S.about;</script>"
    ),
    "/in/jane/details/experience/": page_html(
        "<ul>"
        "<li><div>IPG Automotive</div><div>Full-time · 3 yrs</div><ul>"
        '<li><a href="/in/jane/details/experience/edit/forms/101/">Edit</a><div>Senior Software Developer</div><div>Jan 2023 - Present · 2 yrs</div><div>Simulation tooling.</div></li>'
        '<li><a href="/in/jane/details/experience/edit/forms/102/">Edit</a><div>Software Developer</div><div>Mar 2021 - Dec 2022 · 2 yrs</div></li>'
        "</ul></li>"
        '<li><a href="/in/jane/details/experience/edit/forms/103/">Edit</a><div id="t103"></div><div>Liftango · Contract</div><div>2019 - 2021 · 2 yrs</div><div>Brisbane, Australia</div><div>Routing platform.</div></li>'
        "</ul>",
        "document.getElementById('t103').textContent = S.title103;",
    ),
    "/in/jane/details/experience/edit/forms/103/": dialog(
        '<input id="pe-title" maxlength="100"><input id="pe-companyName" value="Liftango">'
        '<textarea id="pe-description" maxlength="2000"></textarea>',
        "S.title103 = document.getElementById('pe-title').value; S.desc103 = document.getElementById('pe-description').value;",
    ).replace(
        "</script>",
        "document.getElementById('pe-title').value = S.title103; document.getElementById('pe-description').value = S.desc103;</script>",
    ),
    "/in/jane/details/skills/": page_html(
        '<ul id="skills"></ul>',
        """document.getElementById('skills').innerHTML = S.skills.map((s, i) =>
             `<li><a href="/in/jane/details/skills/edit/forms/${1000 + i}/">Edit</a><div>${s}</div><div>${s}</div></li>`).join('');""",
    ),
    "/in/jane/details/skills/edit/forms/new/": dialog(
        '<input id="pe-skill" role="combobox"><ul role="listbox" id="lb"></ul>',
        "S.skills.push(document.getElementById('pe-skill').value);",
    ).replace(
        "</script>",
        """
        const CAT = ['ReactJS', 'React Native', 'TypeScript', 'Node.js'];
        const box = document.getElementById('pe-skill');
        box.addEventListener('input', () => {
          const q = box.value.toLowerCase();
          document.getElementById('lb').innerHTML = q.length < 2 ? '' :
            CAT.filter((c) => c.toLowerCase().includes(q)).map((c) => `<li role="option">${c}</li>`).join('');
          for (const o of document.querySelectorAll('[role=option]')) o.addEventListener('click', () => { box.value = o.textContent; });
        });
        </script>""",
    ),
}


def skill_form(i: int) -> str:
    return page_html(
        '<div role="dialog"><input id="pe-skill" role="combobox" readonly><button type="button" id="del">Delete skill</button></div>',
        f"""
        document.getElementById('pe-skill').value = S.skills[{i}];
        document.getElementById('del').addEventListener('click', () => {{
          const c = document.createElement('div'); c.setAttribute('role', 'alertdialog');
          c.innerHTML = '<button type="button">Delete</button><button type="button">Cancel</button>';
          document.body.appendChild(c);
          c.querySelector('button').addEventListener('click', () => {{ S.skills.splice({i}, 1); save(); closeDialog(); c.remove(); }});
        }});""",
    )


async def _route(route: Route) -> None:
    path = route.request.url.removeprefix(BASE).split("?")[0]
    if not route.request.url.startswith(BASE):
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
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=skill_form(int(path.split("/")[-2]) - 1000),
        )
    else:
        await route.fulfill(
            status=404,
            content_type="text/html; charset=utf-8",
            body=page_html("<h1>Not found</h1>"),
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
    async def test_identity_headline_location_and_limits_come_from_the_forms(
        self, page
    ):
        ed = editor(page)
        assert await ed.read_identity() == (f"{BASE}/in/jane/", "Jane Doe", None)
        h = await ed.read_headline()
        assert (h.value, h.max_length) == ("Senior Software Developer", 220)
        assert await ed.read_location() == "Edinburgh"

    async def test_a_limit_without_maxlength_is_read_from_the_counter(self, page):
        about = await editor(page).read_about()
        assert (about.value, about.max_length) == ("I build web applications.", 2600)

    async def test_experiences_carry_linkedins_position_ids_and_grouped_companies(
        self, page
    ):
        listed = {e.id: e for e in await editor(page).list_experiences()}
        assert set(listed) == {"101", "102", "103"}
        assert (
            listed["102"].title,
            listed["102"].company,
            listed["102"].date_range,
        ) == ("Software Developer", "IPG Automotive", "Mar 2021 - Dec 2022 · 2 yrs")
        e = listed["103"]
        assert (e.company, e.employment_type, e.location, e.description_preview) == (
            "Liftango",
            "Contract",
            "Brisbane, Australia",
            "Routing platform.",
        )

    async def test_skills_are_listed_in_order_with_screen_reader_duplicates_removed(
        self, page
    ):
        skills = await editor(page).list_skills()
        assert [(s.name, s.position, s.ref) for s in skills] == [
            ("jQuery", 1, "1000"),
            ("Python", 2, "1001"),
        ]


class TestWrites:
    async def test_a_headline_write_saves_only_after_checking_the_visible_value(
        self, page
    ):
        ed = editor(page)
        await ed.write_headline(
            expected="Senior Software Developer", value="Senior Product Engineer"
        )
        assert (await stored(page))["headline"] == "Senior Product Engineer"
        assert (await editor(page).read_headline()).value == "Senior Product Engineer"

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
            await editor(page).write_headline(
                expected="Senior Software Developer", value="x" * 230
            )
        assert e.value.code is ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
        assert e.value.details["typedLength"] == 220
        assert (await stored(page)).get("headline") in (
            None,
            "Senior Software Developer",
        )

    async def test_a_missing_field_fails_with_diagnostics_and_clicks_nothing(
        self, page
    ):
        await set_state(page, noHeadlineField=True)
        with pytest.raises(ProfileEditError) as e:
            await editor(page).write_headline(
                expected="Senior Software Developer", value="New"
            )
        assert e.value.code is ProfileEditErrorCode.SELECTOR_NOT_FOUND
        ids = {c["id"] for c in e.value.details["dialog"]["controls"]}
        assert "pe-geoLocation-city" in ids
        assert "headline" not in await stored(page)

    async def test_an_experience_description_is_edited_by_position_id(self, page):
        ed = editor(page)
        await ed.write_experience(
            "103",
            field="description",
            expected="Routing platform.",
            value="Led the routing platform.\n\nTypeScript, Node.js.",
        )
        form = await editor(page).read_experience("103")
        assert (
            form.description.value
            == "Led the routing platform.\n\nTypeScript, Node.js."
        )
        assert (form.title.value, form.company, form.title.max_length) == (
            "Lead Developer",
            "Liftango",
            100,
        )

    async def test_a_skill_is_added_only_on_an_exact_typeahead_match(self, page):
        assert await editor(page).add_skill("reactjs") == "ReactJS"
        assert (await stored(page))["skills"] == ["jQuery", "Python", "ReactJS"]
        with pytest.raises(ProfileEditError) as e:
            await editor(page).add_skill("React")
        assert e.value.code is ProfileEditErrorCode.SKILL_NOT_FOUND
        assert set(e.value.details["offered"]) == {"ReactJS", "React Native"}
        assert (await stored(page))["skills"] == ["jQuery", "Python", "ReactJS"]

    async def test_a_skill_is_removed_through_its_own_form(self, page):
        ed = editor(page)
        [jquery, _] = await ed.list_skills()
        await ed.remove_skill(jquery)
        assert [s.name for s in await editor(page).list_skills()] == ["Python"]


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
                about="New about.",
                skills_add=["TypeScript"],
            )
        )
        assert (await stored(page)).get("headline") is None, "proposing writes nothing"
        refused = svc(writes=False)
        with pytest.raises(ProfileEditError):
            await refused.apply(cs["changeSetId"], confirm=True)
        out = await svc().apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED" and all(r["verified"] for r in out["results"])
        s = await stored(page)
        assert (s["headline"], s["about"], s["skills"][-1]) == (
            "Senior Product Engineer | React, TypeScript",
            "New about.",
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
