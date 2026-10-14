"""Browser-DOM tests for reading an Easy Apply form.

The unit suite mocks ``page.evaluate``, so the programs behind
``JobPageReader.read_easy_apply_form`` never execute there. These run the whole
read against a synthetic posting in headless chromium, with the navigation
stubbed. The dialog is rendered inside an open shadow root and validates its
required fields on Next, as LinkedIn's does; its markup carries the data
attributes LinkedIn's dialog was measured with and none of its classes, so it
is a claim about which signals are read, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import asyncio
import json

import anyio
import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import EasyApplyFormRead, JobPageReader
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
        '<div data-test-form-element><label for="first">'
        '<span aria-hidden="true">First name</span><span>First name</span></label>'
        '<input id="first" type="text" required value="Ada"></div>'
        '<div data-test-form-element><label for="email">Email address</label>'
        '<select id="email" required aria-required="true">'
        "<option>Select an option</option><option selected>me@example.com</option>"
        "</select></div>"
        '<div data-test-form-element><label for="city">Location (city)</label>'
        '<input id="city" type="text" role="combobox" required value="Paris"></div>'
    ),
}

QUESTIONS: dict[str, Any] = {
    "heading": "Additional questions",
    "progress": 50,
    "body": (
        '<div data-test-form-element><fieldset aria-required="true"><legend>'
        '<span aria-hidden="true">Authorized to work in France?</span>'
        "<span>Authorized to work in France?</span></legend>"
        '<input type="radio" id="yes" name="auth" required checked>'
        '<label for="yes">Yes</label>'
        '<input type="radio" id="no" name="auth" required><label for="no">No</label>'
        "</fieldset></div>"
        '<div data-test-form-element><label for="why">Why Acme?</label>'
        '<textarea id="why" maxlength="500">Because.</textarea></div>'
        '<div><label for="upload"><span role="button">Upload resume</span></label>'
        '<input id="upload" type="file" style="display:none"></div>'
    ),
}

RESUME: dict[str, Any] = {
    "heading": "Resume",
    "progress": None,
    "body": (
        '<input id="card" type="radio" checked>'
        '<label for="card"><span>Deselect resume cv.pdf</span></label>'
    ),
}

# The posting, its Easy Apply anchor, and a dialog that behaves like LinkedIn's:
# opened by the anchor, rendered in a shadow root, refusing Next while a
# required field is empty, and asking to save the draft on Escape. Every click
# and keystroke that reaches it is logged on `window.log`.
DIALOG_SCRIPT = """
const STEPS = %s;
window.log = [];
const outlet = document.createElement('div');
outlet.id = 'interop-outlet';
document.body.appendChild(outlet);
const shadow = outlet.attachShadow({mode: 'open'});
shadow.addEventListener('input', () => window.log.push('input'), true);
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
    if (submit) submit.addEventListener('click', () => window.log.push('submit'));
};
document.querySelector('main a[href*="/apply"]').addEventListener('click', (event) => {
    event.preventDefault();
    window.log.push('open');
    setTimeout(render, 100);
});
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !shadow.querySelector('[role="dialog"]')) return;
    window.log.push('escape');
    if (window.stuck) return;
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

EASY_APPLY = '<a href="https://www.linkedin.com/jobs/view/123/apply/?openSDUIApplyFlow=true">Easy Apply</a>'
EXTERNAL = (
    '<a target="_blank" href="https://www.linkedin.com/safety/go/'
    '?url=https%3A%2F%2Facme.example%2F">Apply</a>'
)


def posting(control: str, steps: list[dict], *, stuck: bool = False) -> str:
    return (
        "<main>"
        f"<h1>AI Engineer</h1><p>Acme</p>{control}"
        "<h2>About the job</h2><p>Build agents.</p>"
        "</main>"
        f"<script>{DIALOG_SCRIPT % json.dumps(steps)}"
        f"window.stuck = {'true' if stuck else 'false'};</script>"
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


async def read(page, html: str) -> EasyApplyFormRead:
    """Serve ``html`` as the posting and read its form the way the workflow does."""
    await page.goto(JOB_URL)
    await page.set_content(html)
    return await read_served(page)


async def read_served(page) -> EasyApplyFormRead:
    """Read the form of the posting ``page`` already holds."""
    session = PageSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.read_easy_apply_form(JOB_URL, "123", JOB_APPLY_EN_US)


async def dialog_open(page) -> bool:
    return await page.evaluate(job_pages.EASY_APPLY_OPEN_JS)


async def until_logged(page, entry: str) -> None:
    """Wait for ``entry`` to reach the dialog's log."""
    async with asyncio.timeout(5):
        while entry not in await log(page):
            await asyncio.sleep(0.02)


async def log(page) -> list[str]:
    """What reached the dialog, read in the page's own world where it was logged."""
    return await page.evaluate("() => window.log", isolated_context=False)


async def test_every_step_is_read_and_the_draft_discarded(dom_page):
    result = await read(dom_page, posting(EASY_APPLY, [CONTACT, QUESTIONS, RESUME]))

    assert result.type == "easy_apply"
    assert result.complete is True
    assert result.discarded is True
    assert [step.get("heading") for step in result.steps] == [
        "Contact info",
        "Additional questions",
        "Resume",
    ]
    assert result.steps[0] == {
        "heading": "Contact info",
        "progress": 0,
        "fields": [
            {
                "label": "First name",
                "type": "text",
                "required": True,
                "value": "Ada",
            },
            {
                "label": "Email address",
                "type": "select",
                "required": True,
                "options": ["Select an option", "me@example.com"],
                "value": "me@example.com",
            },
            {
                "label": "Location (city)",
                "type": "typeahead",
                "required": True,
                "value": "Paris",
            },
        ],
    }
    assert result.steps[1]["fields"] == [
        {
            "label": "Authorized to work in France?",
            "type": "radio",
            "required": True,
            "options": ["Yes", "No"],
            "selected": ["Yes"],
        },
        {
            "label": "Why Acme?",
            "type": "textarea",
            "required": False,
            "value": "Because.",
            "max_length": 500,
        },
        {"label": "Upload resume", "type": "file", "required": False},
    ]
    assert "progress" not in result.steps[2]
    assert await log(dom_page) == ["open", "next", "next", "escape", "discard"]


async def test_a_step_needing_input_ends_the_read_with_its_error(dom_page):
    blocked = dict(CONTACT, body=CONTACT["body"].replace(' value="Paris"', ""))

    result = await read(dom_page, posting(EASY_APPLY, [blocked, QUESTIONS, RESUME]))

    assert result.complete is False
    assert result.discarded is True
    assert len(result.steps) == 1
    city = result.steps[0]["fields"][2]
    assert city == {
        "label": "Location (city)",
        "type": "typeahead",
        "required": True,
        "error": "Please enter a valid answer",
    }
    assert await log(dom_page) == ["open", "next", "escape", "discard"]


async def test_submit_is_never_clicked_on_a_one_step_form(dom_page):
    result = await read(dom_page, posting(EASY_APPLY, [RESUME]))

    assert result.complete is True
    assert result.steps[0]["fields"] == [
        {
            "label": "Deselect resume cv.pdf",
            "type": "radio",
            "required": False,
            "options": ["Deselect resume cv.pdf"],
            "selected": ["Deselect resume cv.pdf"],
        }
    ]
    assert "submit" not in await log(dom_page)


async def test_long_option_lists_are_cut_with_their_total(dom_page, monkeypatch):
    monkeypatch.setattr(job_pages, "_EASY_APPLY_MAX_OPTIONS", 1)

    result = await read(dom_page, posting(EASY_APPLY, [CONTACT]))

    email = result.steps[0]["fields"][1]
    assert email["options"] == ["Select an option"]
    assert email["options_total"] == 2
    assert email["value"] == "me@example.com"


async def test_a_dialog_that_will_not_close_is_reported(dom_page):
    result = await read(dom_page, posting(EASY_APPLY, [RESUME], stuck=True))

    assert result.discarded is False
    assert "discard" not in await log(dom_page)


async def test_a_posting_without_easy_apply_is_not_opened(dom_page):
    result = await read(dom_page, posting(EXTERNAL, [CONTACT]))

    assert result == EasyApplyFormRead("external")
    assert await log(dom_page) == []


async def serve(page, html: str) -> None:
    await page.goto(JOB_URL)
    await page.set_content(html)


async def test_a_cancelled_read_still_discards_the_draft(dom_page):
    await serve(dom_page, posting(EASY_APPLY, [CONTACT, QUESTIONS, RESUME]))
    task = asyncio.ensure_future(read_served(dom_page))
    await until_logged(dom_page, "next")
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await dialog_open(dom_page)
    assert (await log(dom_page))[-2:] == ["escape", "discard"]


async def test_a_read_cancelled_while_the_dialog_opens_discards_it(dom_page):
    # The dialog renders 100ms after the click, so this cancel lands before it.
    await serve(dom_page, posting(EASY_APPLY, [CONTACT]))
    task = asyncio.ensure_future(read_served(dom_page))
    await until_logged(dom_page, "open")
    assert not await dialog_open(dom_page)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await dialog_open(dom_page)
    assert await log(dom_page) == ["open", "escape", "discard"]


async def test_a_read_past_the_tool_deadline_still_discards_the_draft(dom_page):
    # A tool timeout is an AnyIO scope, which cancels the task again on every
    # pass of the loop: an unshielded discard ends at its first await. Three
    # steps take well over a second to read.
    await serve(dom_page, posting(EASY_APPLY, [CONTACT, QUESTIONS, RESUME]))

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.4):
            await read_served(dom_page)

    assert not await dialog_open(dom_page)
    assert "open" in await log(dom_page)
    assert (await log(dom_page))[-2:] == ["escape", "discard"]
