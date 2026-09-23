"""Browser-DOM tests for answering an Easy Apply form and sending it.

The unit suite mocks ``page.evaluate``, so none of the programs behind
``JobPageReader.submit_easy_apply`` execute there and no answer ever reaches a
real control. These run the whole walk against a synthetic posting in headless
chromium, with the navigation stubbed. The dialog is rendered inside an open
shadow root, validates its required fields on Next, grows a card for an
uploaded resume and holds Submit beside the follow checkbox in its footer, as
LinkedIn's does; its markup carries the data attributes LinkedIn's dialog was
measured with and none of its classes, so it is a claim about which signals are
read and written, not about LinkedIn's layout.

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
# required field is empty, growing a card for an uploaded resume, and asking to
# save the draft on Escape. Every click, keystroke and value that reaches it is
# logged on `window.log`.
DIALOG_SCRIPT = """
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
        + '<input id="follow" type="checkbox" checked><label for="follow">Follow Acme</label>'
        + (last
            ? '<button type="button" data-live-test-easy-apply-submit-button>Submit application</button>'
            : '<button type="button" data-easy-apply-next-button>Next</button>')
        + '</footer></form></div>';
    const next = shadow.querySelector('[data-easy-apply-next-button]');
    if (next) next.addEventListener('click', () => {
        window.log.push('next');
        let valid = true;
        for (const el of shadow.querySelectorAll('form [required]')) {
            const empty = el.type === 'radio'
                ? !shadow.querySelector(`[name="${el.name}"]:checked`)
                : !el.value;
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
    const submit = shadow.querySelector('[data-live-test-easy-apply-submit-button]');
    if (submit) submit.addEventListener('click', () => {
        window.log.push('submit');
        window.log.push('follow=' + shadow.getElementById('follow').checked);
        shadow.innerHTML = '<div role="dialog"><h2>Your application was sent to Acme</h2></div>';
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


def posting(steps: list[dict], delay: int = 400) -> str:
    return (
        "<main>"
        f"<h1>AI Engineer</h1><p>Acme</p>{EASY_APPLY}"
        "<h2>About the job</h2><p>Build agents.</p>"
        "</main>"
        f"<script>window.uploadDelay = {delay};"
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
    monkeypatch.setattr(job_pages, "_EASY_APPLY_UPLOAD_TIMEOUT", 4.0)


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
) -> EasyApplySubmitRead:
    """Answer the synthetic posting's form the way the workflow does."""
    await page.goto(JOB_URL)
    await page.set_content(posting(steps, delay))
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
