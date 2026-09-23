"""Browser-DOM tests for answering an Easy Apply form and sending it.

The unit suite mocks ``page.evaluate``, so none of the programs behind
``JobPageReader.submit_easy_apply`` execute there and no answer ever reaches a
real control. These run the whole walk against a synthetic posting in headless
chromium, with the navigation stubbed. The dialog is rendered inside an open
shadow root, validates its required fields on Next, grows a card for an
uploaded resume, offers a typeahead that renders its suggestions in that same
shadow root and binds an entity only to a chosen one, and holds Submit beside
the follow checkbox in its footer, as LinkedIn's does; its markup carries the
data attributes LinkedIn's dialog was measured with and none of its classes, so
it is a claim about which signals are read and written, not about LinkedIn's
layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import json

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.core.exceptions import InvalidAnswerError
from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import EasyApplySubmitRead, JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB_URL = "https://www.linkedin.com/jobs/view/123/"

CONTACT: dict[str, Any] = {
    "heading": "Contact info",
    "progress": 0,
    "body": (
        '<div data-test-form-element><label for="first">First name</label>'
        '<input id="first" type="text" required value="Ada"></div>'
        '<div data-test-form-element><label for="email">Email address</label>'
        '<select id="email" required aria-required="true">'
        "<option>Select an option</option><option>me@example.com</option>"
        "</select></div>"
        '<div data-test-form-element><label for="phone">Mobile phone number</label>'
        '<input id="phone" type="text" required></div>'
    ),
}

QUESTIONS: dict[str, Any] = {
    "heading": "Additional questions",
    "progress": 50,
    "body": (
        '<div data-test-form-element><fieldset aria-required="true"><legend>'
        "Authorized to work in France?</legend>"
        '<input type="radio" id="yes" name="auth" required>'
        '<label for="yes">Yes</label>'
        '<input type="radio" id="no" name="auth" required><label for="no">No</label>'
        "</fieldset></div>"
        "<div data-test-form-element><fieldset><legend>Which apply?</legend>"
        '<input type="checkbox" id="py" name="skill"><label for="py">Python</label>'
        '<input type="checkbox" id="go" name="skill" checked>'
        '<label for="go">Go</label>'
        "</fieldset></div>"
        '<div data-test-form-element><label for="why">Why Acme?</label>'
        '<textarea id="why" maxlength="500"></textarea></div>'
    ),
}

# What the typeahead is offering to look up, and the entity behind each label.
# Two of them share a prefix, so an answer that stops at the prefix names none.
CITIES: dict[str, str] = {
    "Paris, \u00cele-de-France, France": "urn:li:geo:104994268",
    "Paris, Texas, United States": "urn:li:geo:101857102",
}


def with_city(city: str = "") -> dict[str, Any]:
    """The contact step with a city typeahead, optionally already chosen.

    Beside the other contact questions rather than alone, because what a failed
    pick must leave untouched is the rest of the step.

    The combobox carries a hidden sibling for the entity behind the text: what
    LinkedIn stores is the entity and the visible string is only its label. The
    hidden control is this dialog's own device -- LinkedIn publishes no control
    carrying the entity and keeps the binding in component state -- and it is
    here for the one behaviour both share: the step refuses on Next while
    nothing is bound, so typed-but-unpicked text fails here the way it fails
    live. Nothing under test reads it, because there is nothing to read.

    The label says its text twice, once for the screen and once aria-hidden, as
    LinkedIn's does. Read raw it would double, and the step read drops the copy.
    """
    return dict(
        CONTACT,
        body=CONTACT["body"] + '<div data-test-form-element><label for="city">'
        '<span aria-hidden="true">Location (city)</span>'
        '<span class="visually-hidden">Location (city)</span></label>'
        '<div id="city-ta">'
        '<input id="city" type="text" role="combobox"'
        f' aria-autocomplete="list" aria-expanded="false" required value="{city}">'
        "</div>"
        f'<input id="city-urn" type="hidden" value="{CITIES.get(city, "")}">'
        "</div>",
    )


# The resume step LinkedIn renders: the cards it already holds, and an upload
# button with the file input hidden behind it.
RESUME: dict[str, Any] = {
    "heading": "Resume",
    "progress": 100,
    "body": (
        '<div id="resume-list">'
        '<div><input id="card-1" type="radio" name="resume" checked>'
        '<label for="card-1">Select resume old.pdf</label></div>'
        "</div>"
        '<div><label for="upload"><span role="button">Upload resume</span></label>'
        '<input id="upload" type="file" style="display:none"></div>'
    ),
}

# A one-step form: Submit in the footer slot from the start.
SOLO: dict[str, Any] = {
    "heading": "Apply to Acme",
    "progress": None,
    "body": (
        '<div data-test-form-element><label for="first">First name</label>'
        '<input id="first" type="text" required value="Ada"></div>'
        '<div id="resume-list">'
        '<div><input id="card-1" type="radio" name="resume" checked>'
        '<label for="card-1">Select resume old.pdf</label></div>'
        "</div>"
        '<div><label for="upload"><span role="button">Upload resume</span></label>'
        '<input id="upload" type="file" style="display:none"></div>'
    ),
}

# The posting, its Easy Apply anchor, and a dialog that behaves like LinkedIn's:
# opened by the anchor, rendered in a shadow root, refusing Next while a
# required field is empty or a typeahead holds text nobody picked, growing a
# card for an uploaded resume, and asking to save the draft on Escape. Every
# click, keystroke and value that reaches it is logged on `window.log`.
DIALOG_SCRIPT = r"""
const STEPS = %s;
window.log = [];
const outlet = document.createElement('div');
outlet.id = 'interop-outlet';
document.body.appendChild(outlet);
const shadow = outlet.attachShadow({mode: 'open'});
const value = (el) => (el.type === 'checkbox' || el.type === 'radio')
    ? String(el.checked)
    : (el.type === 'file' ? (el.files[0] || {}).name : el.value);
shadow.addEventListener('input', (e) => window.log.push(e.target.id + '=' + value(e.target)), true);
shadow.addEventListener('change', (e) => {
    const el = e.target;
    if (el.type !== 'file' || !el.files.length) return;
    window.log.push('upload:' + el.files[0].name);
    // LinkedIn tears the resume list down when the upload starts and rebuilds
    // it around the new file only once that file has arrived. Nothing about
    // the empty list in between says a file is coming.
    const list = shadow.getElementById('resume-list');
    if (!list) return;
    const name = el.files[0].name;
    list.innerHTML = '';
    setTimeout(() => {
        const live = shadow.getElementById('resume-list');
        if (!live) return;
        const card = document.createElement('div');
        card.innerHTML = '<input id="card-new" type="radio" name="resume" checked>'
            + `<label for="card-new">Select resume ${name}</label>`;
        live.appendChild(card);
        window.log.push('rebuilt');
    }, window.uploadDelay);
}, true);
// A typeahead that behaves like LinkedIn's: it searches only on keystrokes,
// it debounces, it names its open list through both `aria-owns` and
// `aria-controls`, and it binds an entity only when one of its suggestions is
// chosen. Typing again abandons whatever was bound, so a string that merely
// reads right is worth nothing.
const wireTypeahead = (input) => {
    const hidden = shadow.getElementById(input.id + '-urn');
    let box = null;
    let timer = null;
    const close = () => {
        if (box) box.remove();
        box = null;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-owns');
        input.removeAttribute('aria-controls');
    };
    const open = (typed) => {
        // What the component saw, not what the field holds by the time the
        // debounce fires. A value set in one shot arrives after the last
        // keystroke was seen, and this one searches for what it was given.
        const query = typed.replace(/\s+/g, ' ').trim().toLowerCase();
        close();
        if (!query) return;
        const hits = Object.keys(window.CITIES)
            .filter((city) => city.toLowerCase().startsWith(query));
        if (!hits.length) return;
        box = document.createElement('div');
        box.id = input.id + '-suggestions';
        box.setAttribute('role', 'listbox');
        for (const city of hits) {
            const option = document.createElement('div');
            option.setAttribute('role', 'option');
            // As markup renders it: wrapped and indented, so what the
            // node reads as is nothing like the string it shows. The
            // aria-hidden copy is the shape LinkedIn's labels carry, given to
            // a suggestion too so both go through one cleaning rule.
            option.innerHTML = `
                <span>${city}</span>
                <span aria-hidden="true">${city}</span>
            `;
            option.addEventListener('click', () => {
                window.log.push('clicked:' + city);
                // `ignore` is the suggestion LinkedIn offered and then did
                // nothing with: the text stays as it was typed and the list
                // stays open, which is the only thing that separates it from
                // a pick when the answer was spelled out in full.
                if (window.pickAnswer === 'ignore') return;
                input.value = city;
                hidden.value = window.CITIES[city];
                window.log.push('picked:' + city);
                close();
            });
            box.appendChild(option);
        }
        // Beside the input, inside the same shadow root, which is where
        // LinkedIn renders it. The document's own `querySelector` reaches
        // neither the field nor the list; a CSS locator reaches both.
        input.parentElement.appendChild(box);
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-owns', box.id);
        input.setAttribute('aria-controls', box.id);
        window.log.push('suggested:' + hits.length);
    };
    input.addEventListener('keydown', () => setTimeout(() => {
        hidden.value = '';
        const typed = input.value;
        clearTimeout(timer);
        timer = setTimeout(() => open(typed), window.suggestDelay);
    }, 0));
};
let index = 0;
const render = () => {
    const step = STEPS[index];
    const last = index === STEPS.length - 1;
    shadow.innerHTML = '<div role="dialog" data-test-modal>'
        + '<button aria-label="Dismiss" data-test-modal-close-btn></button>'
        + '<h2>Apply to Acme</h2>'
        + (step.progress === null ? '' : `<progress max="100" value="${step.progress}"></progress>`)
        + `<form><h3>${step.heading}</h3>${step.body}`
        + '<footer role="presentation">'
        + '<input id="follow" type="checkbox" checked style="' + window.followStyle + '">'
        + '<label for="follow">Follow Acme</label>'
        + (last
            ? '<button type="button" data-live-test-easy-apply-submit-button>Submit application</button>'
            : '<button type="button" data-easy-apply-next-button>Next</button>')
        + '</footer></form></div>';
    const next = shadow.querySelector('[data-easy-apply-next-button]');
    if (next) next.addEventListener('click', () => {
        window.log.push('next');
        let valid = true;
        for (const el of shadow.querySelectorAll('form [required]')) {
            // A combobox is answered by the entity behind it. Text that was
            // typed and never picked leaves that empty, and the step refuses.
            const entity = el.getAttribute('role') === 'combobox'
                && shadow.getElementById(el.id + '-urn');
            // What the step was judged on, kept where a later render cannot
            // take it away.
            if (entity) window.log.push('entity=' + entity.value);
            const empty = el.type === 'radio'
                ? !shadow.querySelector(`[name="${el.name}"]:checked`)
                : entity ? !entity.value : !el.value;
            if (!empty || shadow.getElementById(el.id + '-error')) continue;
            el.setAttribute('aria-describedby', el.id + '-error');
            const error = document.createElement('div');
            error.id = el.id + '-error';
            error.innerHTML = '<div role="alert">Please enter a valid answer</div>';
            el.after(error);
            valid = false;
        }
        if (valid) setTimeout(() => { index += 1; render(); }, 100);
    });
    const combo = shadow.querySelector('[role="combobox"]');
    if (combo) wireTypeahead(combo);
    const submit = shadow.querySelector('[data-live-test-easy-apply-submit-button]');
    if (submit) submit.addEventListener('click', () => {
        window.log.push('submit');
        window.log.push('follow=' + shadow.getElementById('follow').checked);
        shadow.innerHTML = '';
        // What LinkedIn does next is the thing that varies. `dialog` is the
        // modal it narrates the send with; `posting` is the case measured on
        // 2026-09-23, where nothing renders and the posting behind the form
        // grows its applied line a moment later; `silent` is neither.
        if (window.submitAnswer === 'dialog') {
            shadow.innerHTML = '<div role="dialog">'
                + '<h2>Your application was sent to Acme</h2></div>';
        } else if (window.submitAnswer === 'posting') {
            setTimeout(() => {
                const main = document.querySelector('main');
                const line = document.createElement('p');
                line.textContent = 'Application submitted';
                main.insertBefore(line, main.querySelector('h2'));
                window.log.push('applied');
            }, window.postingDelay);
        }
    });
};
document.querySelector('main a[href*="/apply"]').addEventListener('click', (event) => {
    event.preventDefault();
    window.log.push('open');
    setTimeout(render, 100);
});
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !shadow.querySelector('form')) return;
    window.log.push('escape');
    const confirm = document.createElement('div');
    confirm.setAttribute('role', 'alertdialog');
    confirm.innerHTML = '<h2>Save this application?</h2>'
        + '<button data-control-name="discard_application_confirm_btn">Discard</button>'
        + '<button data-control-name="save_application_btn">Save</button>';
    shadow.appendChild(confirm);
    confirm.querySelector('[data-control-name="discard_application_confirm_btn"]')
        .addEventListener('click', () => { window.log.push('discard'); shadow.innerHTML = ''; });
    confirm.querySelector('[data-control-name="save_application_btn"]')
        .addEventListener('click', () => window.log.push('save'));
});
"""

EASY_APPLY = (
    '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true">'
    "Easy Apply</a>"
)


def posting(
    steps: list[dict],
    delay: int = 400,
    follow_style: str = "",
    submit_answer: str = "dialog",
    posting_delay: int = 300,
    suggest_delay: int = 200,
    pick_answer: str = "commit",
) -> str:
    return (
        "<main>"
        f"<h1>AI Engineer</h1><p>Acme</p>{EASY_APPLY}"
        "<h2>About the job</h2><p>Build agents.</p>"
        "</main>"
        f"<script>window.uploadDelay = {delay};"
        f"window.followStyle = {json.dumps(follow_style)};"
        f"window.submitAnswer = {json.dumps(submit_answer)};"
        f"window.postingDelay = {posting_delay};"
        f"window.suggestDelay = {suggest_delay};"
        f"window.pickAnswer = {json.dumps(pick_answer)};"
        f"window.CITIES = {json.dumps(CITIES)};"
        f"{DIALOG_SCRIPT % json.dumps(steps)}</script>"
    )


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<p>ok</p>"
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(job_pages, "_APPLY_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_OPEN_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_STEP_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_DISCARD_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_SUBMIT_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_CONFIRMATION_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_UPLOAD_TIMEOUT", 4.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_SUGGEST_TIMEOUT", 3.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_PICK_TIMEOUT", 1.5)


@pytest.fixture
def resume(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.7\n")
    return str(path)


async def answer(
    page,
    steps: list[dict],
    answers: dict[str, Any],
    *,
    confirm_submit: bool = False,
    follow_company: bool = False,
    delay: int = 400,
    follow_style: str = "",
    submit_answer: str = "dialog",
    posting_delay: int = 300,
    suggest_delay: int = 200,
    pick_answer: str = "commit",
) -> EasyApplySubmitRead:
    """Answer the synthetic posting's form the way the workflow does."""
    await page.goto(JOB_URL)
    await page.set_content(
        posting(
            steps,
            delay,
            follow_style,
            submit_answer,
            posting_delay,
            suggest_delay,
            pick_answer,
        )
    )
    session = PageSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.submit_easy_apply(
            JOB_URL,
            "123",
            JOB_APPLY_EN_US,
            answers,
            confirm_submit=confirm_submit,
            follow_company=follow_company,
        )


async def log(page) -> list[str]:
    """What reached the dialog, read in the page's own world where it was logged."""
    return await page.evaluate("() => window.log", isolated_context=False)


ALL_ANSWERS: dict[str, Any] = {
    "First name": "Noor",
    "Email address": "me@example.com",
    "Mobile phone number": "+33600000000",
    "Authorized to work in France?": "Yes",
    "Which apply?": ["Python"],
    "Why Acme?": "Because agents.",
}


async def test_every_control_type_is_set_from_its_label(dom_page):
    result = await answer(dom_page, [CONTACT, QUESTIONS], ALL_ANSWERS)

    assert result.complete is True
    # The values are in the DOM, read back off the live dialog, and they got
    # there through the events a React form listens for.
    contact = {field["label"]: field for field in result.steps[0]["fields"]}
    assert contact["First name"]["value"] == "Noor"
    assert contact["Email address"]["value"] == "me@example.com"
    assert contact["Mobile phone number"]["value"] == "+33600000000"
    questions = {field["label"]: field for field in result.steps[1]["fields"]}
    assert questions["Authorized to work in France?"]["selected"] == ["Yes"]
    assert questions["Which apply?"]["selected"] == ["Python"]
    assert questions["Why Acme?"]["value"] == "Because agents."
    events = await log(dom_page)
    assert "first=Noor" in events and "yes=true" in events
    # Go was checked to begin with and the answer did not name it.
    assert "go=false" in events


async def test_the_uploaded_file_lands_and_its_new_card_is_not_a_refusal(
    dom_page, resume
):
    result = await answer(
        dom_page,
        [RESUME],
        {"Upload resume": resume},
    )

    assert "upload:resume.pdf" in await log(dom_page)
    assert result.complete is True
    labels = [field["label"] for field in result.steps[0]["fields"]]
    assert "Select resume resume.pdf" in labels
    card = next(
        field
        for field in result.steps[0]["fields"]
        if field["label"] == "Select resume resume.pdf"
    )
    assert card["selected"] == ["Select resume resume.pdf"]


async def test_a_resume_list_torn_down_by_the_upload_is_waited_out(dom_page, resume):
    # The live failure: the list empties the moment the upload starts and
    # holds still until the file lands. Reads agreeing on the empty list is
    # not the file having arrived.
    result = await answer(dom_page, [RESUME], {"Upload resume": resume}, delay=1500)

    events = await log(dom_page)
    assert events.index("upload:resume.pdf") < events.index("rebuilt")
    labels = [field["label"] for field in result.steps[0]["fields"]]
    assert "Select resume resume.pdf" in labels


async def test_a_file_the_form_never_shows_back_is_not_submitted(dom_page, resume):
    with pytest.raises(InvalidAnswerError, match="did not show 'resume.pdf'"):
        await answer(
            dom_page,
            [RESUME],
            {"Upload resume": resume},
            confirm_submit=True,
            delay=100_000,
        )

    events = await log(dom_page)
    assert "submit" not in events
    assert "discard" in events


async def test_without_the_flag_the_form_is_filled_and_the_draft_discarded(dom_page):
    result = await answer(dom_page, [CONTACT, QUESTIONS, RESUME], ALL_ANSWERS)

    assert result.submitted is False
    assert result.complete is True
    assert result.discarded is True
    events = await log(dom_page)
    assert "submit" not in events
    assert events[-2:] == ["escape", "discard"]


async def test_a_required_field_nothing_answers_refuses_before_any_click(dom_page):
    with pytest.raises(InvalidAnswerError, match="Mobile phone number"):
        await answer(
            dom_page,
            [CONTACT, QUESTIONS],
            {"First name": "Noor", "Email address": "me@example.com"},
            confirm_submit=True,
        )

    events = await log(dom_page)
    assert "next" not in events and "submit" not in events
    assert "discard" in events


async def test_with_every_answer_the_flag_clicks_submit_once(dom_page, resume):
    result = await answer(
        dom_page,
        [CONTACT, QUESTIONS, RESUME],
        {**ALL_ANSWERS, "Upload resume": resume},
        confirm_submit=True,
    )

    events = await log(dom_page)
    assert events.count("submit") == 1
    assert "discard" not in events
    assert result.submitted is True
    assert result.confirmation == "Your application was sent to Acme"


async def test_the_follow_checkbox_is_unchecked_on_submit_unless_asked_for(dom_page):
    result = await answer(
        dom_page, [CONTACT, QUESTIONS], ALL_ANSWERS, confirm_submit=True
    )

    assert "follow=false" in await log(dom_page)
    assert result.follow is False


async def test_the_follow_checkbox_is_left_checked_when_it_is_asked_for(dom_page):
    result = await answer(
        dom_page,
        [CONTACT, QUESTIONS],
        ALL_ANSWERS,
        confirm_submit=True,
        follow_company=True,
    )

    assert "follow=true" in await log(dom_page)
    assert result.follow is True


async def test_a_follow_checkbox_hidden_behind_its_label_is_still_unchecked(dom_page):
    # How LinkedIn renders it: the input has nothing to click, so `uncheck`
    # will not act on it and only the label a person clicks does.
    result = await answer(
        dom_page,
        [CONTACT, QUESTIONS],
        ALL_ANSWERS,
        confirm_submit=True,
        follow_style="display:none",
    )

    assert "follow=false" in await log(dom_page)
    assert result.follow is False


async def test_a_one_step_form_submits_from_its_own_footer(dom_page, resume):
    result = await answer(
        dom_page, [SOLO], {"Upload resume": resume}, confirm_submit=True
    )

    events = await log(dom_page)
    assert events.count("submit") == 1
    assert "next" not in events
    assert result.submitted is True
    assert result.complete is True
    assert result.confirmation == "Your application was sent to Acme"


async def test_a_submit_with_no_dialog_is_confirmed_by_the_posting(dom_page, resume):
    """The one-step form as it was measured: no modal, ever.

    The form closes and nothing narrates it. What renders instead, and only
    after a moment, is the posting's own applied line, which the read has to
    still be looking for by then.
    """
    result = await answer(
        dom_page,
        [SOLO],
        {"Upload resume": resume},
        confirm_submit=True,
        submit_answer="posting",
    )

    assert result.submitted is True
    assert result.confirmation == "Application submitted"
    assert "applied" in await log(dom_page)


async def test_a_line_in_the_description_is_not_the_postings_state(dom_page, resume):
    """The applied line is read above the description heading and nowhere else.

    A posting whose own text uses the words is not a posting that has been
    applied to, and reading one as the other would report a confirmation for
    an application LinkedIn said nothing about.
    """
    await dom_page.goto(JOB_URL)
    body = posting([SOLO], submit_answer="silent")
    await dom_page.set_content(
        body.replace(
            "<p>Build agents.</p>", "<p>Application submitted</p><p>Build agents.</p>"
        )
    )
    session = PageSession(dom_page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        result = await reader.submit_easy_apply(
            JOB_URL,
            "123",
            JOB_APPLY_EN_US,
            {"Upload resume": resume},
            confirm_submit=True,
        )

    assert result.submitted is True
    assert result.confirmation == ""


PARIS = "Paris, Île-de-France, France"


def judged_on(events: list[str]) -> list[str]:
    """The entity the city held each time LinkedIn judged the step.

    Read from the log rather than from the DOM: the hidden control goes with
    the step that carried it, and what matters is what it held at the moment
    Next was pressed.
    """
    return [
        event.removeprefix("entity=") for event in events if event.startswith("entity=")
    ]


async def test_a_typeahead_is_answered_by_picking_what_linkedin_offers(dom_page):
    result = await answer(
        dom_page, [with_city(), QUESTIONS], {**ALL_ANSWERS, "Location (city)": PARIS}
    )

    city = {field["label"]: field for field in result.steps[0]["fields"]}
    assert city["Location (city)"]["value"] == PARIS
    events = await log(dom_page)
    # The entity is what LinkedIn stores, and only a chosen suggestion sets it.
    assert judged_on(events) == [CITIES[PARIS]]
    assert f"picked:{PARIS}" in events
    # Typed a character at a time: a value set in one shot never reaches a
    # search that listens for keystrokes.
    assert len([e for e in events if e.startswith("city=")]) > 1
    # And the step was accepted, so its entity was there when Next was pressed.
    assert result.complete is True


async def test_an_answer_no_suggestion_wears_refuses_and_says_what_was_offered(
    dom_page,
):
    with pytest.raises(InvalidAnswerError) as refusal:
        await answer(
            dom_page,
            [with_city(), QUESTIONS],
            {"Location (city)": "Paris", "First name": "Noor"},
        )

    message = str(refusal.value)
    assert "No suggestion is labelled 'Paris'" in message
    # Both spellings LinkedIn offered, so the retry is one call away.
    assert PARIS in message and "Paris, Texas, United States" in message
    assert "Location (city) (typeahead)" in message


async def test_a_refused_pick_leaves_the_step_as_linkedin_prefilled_it(dom_page):
    with pytest.raises(InvalidAnswerError):
        await answer(
            dom_page,
            [with_city(), QUESTIONS],
            # The city named after the other answer, so the order the edits
            # run in is the planner's doing rather than the dict's.
            {"First name": "Noor", "Location (city)": "Nowhere"},
        )

    events = await log(dom_page)
    # The pick ran before every other edit, so nothing else was written, and
    # the fragment it typed was taken back out.
    assert not any(event.startswith("first=") for event in events)
    assert "city=" in events


async def test_a_prefilled_typeahead_is_replaced_when_an_answer_names_it(dom_page):
    texas = "Paris, Texas, United States"
    result = await answer(
        dom_page,
        [with_city(texas), QUESTIONS],
        {**ALL_ANSWERS, "Location (city)": PARIS},
    )

    city = {field["label"]: field for field in result.steps[0]["fields"]}
    assert city["Location (city)"]["value"] == PARIS
    assert judged_on(await log(dom_page)) == [CITIES[PARIS]]


async def test_a_prefill_that_already_is_the_answer_is_never_retyped(dom_page):
    result = await answer(
        dom_page,
        [with_city(PARIS), QUESTIONS],
        {**ALL_ANSWERS, "Location (city)": PARIS},
    )

    city = {field["label"]: field for field in result.steps[0]["fields"]}
    assert city["Location (city)"]["value"] == PARIS
    events = await log(dom_page)
    # Untouched: LinkedIn's own binding of that text to an entity survived.
    assert judged_on(events) == [CITIES[PARIS]]
    assert not any(event.startswith("city=") for event in events)


async def test_an_empty_typeahead_no_answer_names_still_refuses(dom_page):
    with pytest.raises(InvalidAnswerError, match="needs an answer naming it"):
        await answer(dom_page, [with_city(), QUESTIONS], ALL_ANSWERS)


async def test_typing_the_label_and_not_picking_does_not_answer_the_step(dom_page):
    """What the tests above would look like if the pick never committed.

    The dialog is built to refuse that, because without it every test here
    would pass for an implementation that types the right string, picks
    nothing, and sends an application LinkedIn has no entity for.
    """
    await dom_page.goto(JOB_URL)
    await dom_page.set_content(posting([with_city(), QUESTIONS]))
    await dom_page.locator('main a[href*="/apply"]').click()
    city = dom_page.locator('[role="dialog"] [id="city"]')
    await city.wait_for()
    await city.press_sequentially(PARIS, delay=5)
    await dom_page.wait_for_timeout(400)

    assert await city.input_value() == PARIS
    await dom_page.locator(job_pages.EASY_APPLY_NEXT_SELECTOR).click()
    await dom_page.wait_for_timeout(300)

    assert judged_on(await log(dom_page)) == [""]
    assert await dom_page.locator('[role="dialog"] [id="city-error"]').count() == 1


async def test_a_suggestion_linkedin_does_not_take_refuses(dom_page):
    """The answer spelled out in full, clicked, and quietly dropped.

    The field reads back as the answer either way, because that is what was
    typed. Only the list staying open says nothing was chosen.
    """
    with pytest.raises(InvalidAnswerError) as refusal:
        await answer(
            dom_page,
            [with_city(), QUESTIONS],
            {**ALL_ANSWERS, "Location (city)": PARIS},
            pick_answer="ignore",
        )

    message = str(refusal.value)
    assert "LinkedIn did not take it" in message
    events = await log(dom_page)
    assert f"clicked:{PARIS}" in events and f"picked:{PARIS}" not in events
    # And the string that was typed is not left behind as the answer.
    assert "city=" in events


async def test_a_suggestion_is_matched_by_what_it_shows(dom_page):
    """Not by what its markup holds.

    LinkedIn wraps a suggestion over several indented lines and repeats its
    text in an aria-hidden copy. Read raw, the label comes back doubled and
    padded, and an answer wearing the visible text matches nothing.
    """
    spaced = "  Paris,   \u00cele-de-France,\n France "
    assert spaced != PARIS

    result = await answer(
        dom_page,
        [with_city(), QUESTIONS],
        {**ALL_ANSWERS, "Location (city)": spaced},
    )

    city = {field["label"]: field for field in result.steps[0]["fields"]}
    assert city["Location (city)"]["value"] == PARIS
    assert judged_on(await log(dom_page)) == [CITIES[PARIS]]
