"""Page-level reads behind the job search and saved-job list workflows.

A lower-level service than the workflows in `jobs.py` rather than a peer of
them: it navigates, scrolls, extracts and counts one page at a time, and
answers with a `JobPageCapture`. The pagination policy, the budgets and the
diagnostics that decide what a page *means* stay with the workflow.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import asyncio
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import anyio
from patchright.async_api import Locator
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    InvalidAnswerError,
    LinkedInOperationError,
)
from linkedin_mcp_server.core.utils import (
    _JOB_CARD_SELECTOR,
    _RAIL_PICK_JS,
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.linkedin.capture import RATE_LIMIT_RETRY_DELAY
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.identifiers import job_view_url
from linkedin_mcp_server.linkedin.job_policy import (
    EASY_APPLY_BUTTON_UNREAD,
    EASY_APPLY_DRAFT_LEFT,
    EASY_APPLY_NO_CONTROL,
    EASY_APPLY_SUBMIT_UNCONFIRMED,
    EASY_APPLY_TYPEAHEAD_REFUSED,
    SAFETY_REDIRECT_PATH,
    SCROLL_DEADLINE_MAX,
    ApplyType,
    EasyApplyAnswer,
    easy_apply_answer_refused,
    easy_apply_duplicate_label_error,
    easy_apply_no_suggestion_error,
    easy_apply_pick_unconfirmed_error,
    easy_apply_required_unanswered_error,
    easy_apply_upload_unconfirmed_error,
    easy_apply_unknown_answers_error,
    employer_apply_url,
    parse_posting_title,
    route,
    same_job_search,
)
from linkedin_mcp_server.linkedin.link_metadata import JOB_PATH_RE, build_references
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import (
    JobApplyTextTable,
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# What the rate-limit retry has to have left after its backoff before it is
# worth starting. The retry is a whole page again, and a search page measured
# 6.5s on average (ten navigations of a Paris developer search, 83s). One
# admitted with less times out in navigation, which trades the rate-limit
# answer the first attempt already had for a timeout that explains nothing.
_RETRY_PAGE_ALLOWANCE = 6.5


# The id is the trailing run of digits, and LinkedIn serves the same job under
# both `/jobs/view/1967281839/` and `/jobs/view/<title>-at-<company>-1967281839/`.
# Anchoring the digits to the front of the segment loses the slugged form
# entirely, and reads `2026` out of a title that opens with a year.
#
# The slug is anything but a separator, not `[\w-]`: JS `\w` is ASCII, and a
# localized title reaches this as `d%C3%A9veloppeur-web-at-koul-3510216552`,
# where the `%` ends the match and the id is lost. Measured on the guest
# search API for `developpeur`, where 6 of 10 hrefs were percent-encoded.
# The authenticated pages this server visits serve bare ids today (measured
# across job search, collections and a French search: 0 slugs in 27 anchors),
# so this branch is defensive on both counts.
# `scoped` runs the sidebar's own rule again and reads only the container it
# names, because everything outside it is not a search result: the detail pane
# holds its own permalink and, once opened, a similar-jobs module, and counting
# those as rendered results advances the offset past results the rail never
# showed. Re-run rather than remembered, so a rail replaced between the scroll
# and this call is followed instead of silently widening the scope back to the
# document. `get_saved_jobs` reads the document, having no sidebar to scroll
# and no second list to be confused with.
JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, scoped} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const picked = scoped ? pickRail() : null;
    const scope = picked || document;
    const cards = scope.querySelectorAll(selector);
    const seen = new Set();
    const ids = [];
    for (const card of cards) {
        // `idOf` from the rail rule above, rather than a second copy of the
        // pattern: the rail is picked by counting ids, so a card shape one
        // side understands and the other does not would have extraction read
        // a container the pick never considered.
        const id = idOf(card);
        if (id && !seen.has(id)) {
            seen.add(id);
            ids.push(id);
        }
    }
    return {ids: ids, scoped: Boolean(picked)};
}"""
)

# The same scope as `JOB_IDS_JS`, since a pane job is not a result either. A
# card is the largest element around a job link that holds no other job, found
# by counting ids and not by class, so the classic `<li>` and the redesigned
# card are both found. Only the label is text, and it comes from the locale
# table.
PROMOTED_JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, label} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const seen = new Set();
    const promoted = [];
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const lines = (cardOf(node).innerText || '').split('\n')
            .map((line) => line.trim());
        if (lines.includes(label)) promoted.push(id);
    }
    return promoted;
}"""
)

# How many unpromoted results sit above the notice the redesigned search puts
# where its matches end, or null when nothing on the page reads as the notice.
# Null rather than zero, because zero is what calls the search a no-match. The
# notice is looked for in the whole document, the cards only in the rail,
# each read as `PROMOTED_JOB_IDS_JS` reads it.
#
# The notice is the smallest element whose text is the whole sentence, reached
# from a text node that opens it, so a sentence LinkedIn splits across inline
# elements is still found. Its text only matched when it was one text node,
# and a page whose notice was split then kept its unrelated postings.
ORGANIC_BEFORE_NOTICE_JS = (
    r"""(opts) => {
    const {selector, label, notice} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const isPromoted = (node) => (cardOf(node).innerText || '').split('\n')
        .map((line) => line.trim()).includes(label);
    const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let mark = null;
    while (!mark && walker.nextNode()) {
        const piece = norm(walker.currentNode.data);
        if (!piece || !notice.startsWith(piece)) continue;
        let node = walker.currentNode;
        while (node && norm(node.textContent).length < notice.length) {
            node = node.parentNode;
        }
        if (node && norm(node.textContent) === notice) mark = node;
    }
    if (!mark) return null;
    const seen = new Set();
    let organic = 0;
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const above = mark.compareDocumentPosition(node)
            & Node.DOCUMENT_POSITION_PRECEDING;
        if (above && !isPromoted(node)) organic += 1;
    }
    return organic;
}"""
)

# The posting's own lines: everything above its description heading, trimmed.
# One fragment rather than one per reader, because the boundary is what keeps a
# word in the description or on a "More jobs" card from passing for this
# posting's state, and two copies of it are two chances to put it elsewhere.
# Needs `main` and `descriptionHeadings` already in scope.
_TOP_LINES_JS = r"""    const lines = (main.innerText || '').split('\n').map((line) => line.trim());
    const end = lines.findIndex((line) => descriptionHeadings.includes(line));
    const top = end === -1 ? [] : lines.slice(0, end);
"""

# This posting's apply control and state, once they render. Easy Apply comes in
# two shapes. The anchor into the posting's own `/apply/` route
# (`<a href=".../jobs/view/<id>/apply/?openSDUIApplyFlow=true">`, measured on
# 2026-09-14) is found by its URL, which the "More jobs" cards, each linking its
# own posting, cannot match. The `<button>` with no href that replaced it
# (measured on 2026-09-25) is found by its text, above the description only; the
# cards below show the same words, as text inside their links.
#
# The external control is an `<a target="_blank">` into the off-site
# interstitial, which names the employer's page in its href (measured on
# 2026-09-19), so it is answered without a click. It and the state lines are
# read above the description heading only: below it sit the "More jobs" cards,
# each with an Apply of its own. Without the heading nothing is proven to be
# this posting's, and the read answers `unknown`.
#
# On 2026-09-14 the external control was a `<button>` with no href, whose click
# opened a "Share your profile?" dialog or a tab. LinkedIn counts that click as
# an apply on the posting, and no posting has shown the button since, so it is
# not read: a posting carrying it answers `unknown` and is never clicked.
#
# The heading is found by walking text nodes rather than asking every element
# for its text. Both find it; the walk visits fewer nodes and copies none of
# them, and `textContent` on every element of a posting copies that posting
# once per level of nesting. That is worth the difference because the readiness
# poll runs this program on every frame for up to ten seconds.
APPLY_SIGNALS_JS = (
    r"""(opts) => {
    const {
        applyPath, redirectPath, easyApplyLabel, externalLabel, descriptionHeadings,
        closedLines, appliedPattern,
    } = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const pathOf = (anchor) => {
        try {
            return new URL(anchor.href).pathname.replace(/\/+$/, '');
        } catch (error) {
            return '';
        }
    };
    const walk = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
    let heading = null;
    while (walk.nextNode()) {
        if (descriptionHeadings.includes((walk.currentNode.nodeValue || '').trim())) {
            heading = walk.currentNode;
            break;
        }
    }
    const above = (el) => Boolean(
        heading && (heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)
    );
"""
    + _TOP_LINES_JS
    + r"""    const applied = new RegExp(appliedPattern);
    const anchors = [...main.querySelectorAll('a[href]')];
    const link = anchors.find((anchor) => above(anchor)
        && (anchor.innerText || '').trim() === externalLabel
        && pathOf(anchor) === redirectPath);
    return {
        bounded: Boolean(heading),
        easy_apply: anchors.some((anchor) => pathOf(anchor) === applyPath)
            || [...main.querySelectorAll('button')].some((el) => above(el)
                && (el.innerText || '').trim() === easyApplyLabel),
        external_link: link ? link.href : null,
        applied: top.some((line) => applied.test(line)),
        closed: top.some((line) => closedLines.includes(line)),
    };
}"""
)

# Ready once the description heading and a signal are both in. The Easy Apply
# anchor is found without the heading (the button is not), but the applied and
# closed lines are not, so a read that settled on the anchor alone could call
# such a posting open.
APPLY_READY_JS = (
    "(opts) => {\n    const signals = (" + APPLY_SIGNALS_JS + ")(opts);\n"
    "    return Boolean(signals && signals.bounded && (signals.easy_apply\n"
    "        || signals.external_link || signals.applied || signals.closed));\n}"
)

# How long the apply control gets to render.
_APPLY_READY_TIMEOUT = 10.0
_APPLY_POLL = 0.25

# The Easy Apply dialog renders inside an open shadow root, which
# `querySelectorAll` from the document never enters, so the programs below walk
# the open roots first. Playwright locators pierce them on their own. The dialog
# is told apart from others by an Easy Apply control inside it, and every button
# by a LinkedIn data attribute rather than by its localized label.
_SHADOW_ROOTS_JS = r"""
    const roots = [document];
    for (let i = 0; i < roots.length; i++) {
        for (const el of roots[i].querySelectorAll('*')) {
            if (el.shadowRoot) roots.push(el.shadowRoot);
        }
    }
"""

_EASY_APPLY_DIALOG_JS = (
    _SHADOW_ROOTS_JS
    + r"""
    const marker = '[data-easy-apply-next-button], '
        + '[data-live-test-easy-apply-submit-button], [id*="easyApplyFormElement"]';
    let dialog = null;
    for (const root of roots) {
        dialog = [...root.querySelectorAll('[role="dialog"]')]
            .find((candidate) => candidate.querySelector(marker));
        if (dialog) break;
    }
"""
)

EASY_APPLY_OPEN_JS = (
    "() => {" + _EASY_APPLY_DIALOG_JS + "    return Boolean(dialog);\n}"
)
EASY_APPLY_CLOSED_JS = "() => !(" + EASY_APPLY_OPEN_JS + ")()"

# One step of the open dialog. Radios and checkboxes are grouped into one field
# per question; every other control is its own field. Footer controls (follow
# the company) are not questions, and a file input is read although hidden
# behind its upload button. `signature` changes when the step does.
#
# `ids` carries the control behind each field so an answer can reach it: one
# entry for a text, select or file input, and one per option, in the options'
# order, for a radio or checkbox group. `_public_step` drops it again, because
# a control id is the walk's own handle and means nothing to a caller.
EASY_APPLY_STEP_JS = (
    "(maxOptions) => {"
    + _EASY_APPLY_DIALOG_JS
    + r"""
    if (!dialog) return null;
    const form = dialog.querySelector('form') || dialog;
    const footer = form.querySelector('footer');
    const shown = (el) => Boolean(
        el.offsetWidth || el.offsetHeight || el.getClientRects().length
    );
    // Labels repeat their text in an aria-hidden copy.
    const text = (el) => {
        if (!el) return '';
        const copy = el.cloneNode(true);
        copy.querySelectorAll('[aria-hidden="true"]').forEach((node) => node.remove());
        return (copy.textContent || '').replace(/\s+/g, ' ').trim();
    };
    const byId = (id) => form.querySelector(`[id="${CSS.escape(id)}"]`);
    const labelOf = (el) =>
        (el.id && text(form.querySelector(`label[for="${CSS.escape(el.id)}"]`)))
        || text(el.closest('label'))
        || (el.getAttribute('aria-label') || '').trim();
    const isChoice = (el) => el.type === 'radio' || el.type === 'checkbox';
    const controls = [...form.querySelectorAll('input, select, textarea')].filter(
        (el) => !(footer && footer.contains(el))
            && el.type !== 'hidden'
            && (el.type === 'file' || shown(el))
    );
    const groups = new Map();
    for (const el of controls) {
        const key = isChoice(el)
            ? (el.closest('fieldset, [data-test-form-element]') || el)
            : el;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(el);
    }
    const fields = [];
    for (const [key, group] of groups) {
        const first = group[0];
        const fieldset = first.closest('fieldset');
        const field = {
            label: text(fieldset && fieldset.querySelector('legend'))
                || (group.length === 1 ? labelOf(first) : '')
                || (key === first ? '' : text(key).slice(0, 300)),
            type: first.tagName === 'SELECT' ? 'select'
                : first.tagName === 'TEXTAREA' ? 'textarea'
                : first.getAttribute('role') === 'combobox' ? 'typeahead'
                : first.type || 'text',
            required: group.some(
                (el) => el.required || el.getAttribute('aria-required') === 'true'
            ) || Boolean(fieldset && fieldset.getAttribute('aria-required') === 'true'),
        };
        if (first.tagName === 'SELECT') {
            const options = [...first.options].map((option) => option.text.trim());
            field.options = options.slice(0, maxOptions);
            if (options.length > maxOptions) field.options_total = options.length;
            const picked = first.options[first.selectedIndex];
            if (picked) field.value = picked.text.trim();
        } else if (isChoice(first)) {
            const options = group.map(labelOf);
            field.options = options.slice(0, maxOptions);
            if (options.length > maxOptions) field.options_total = options.length;
            field.selected = group.filter((el) => el.checked).map(labelOf);
        } else if (first.type !== 'file' && first.value) {
            field.value = first.value;
        }
        field.ids = group.map((el) => el.id || '');
        if (first.maxLength > 0) field.max_length = first.maxLength;
        // Hints are described too. Which text is an error is the walk's to
        // say, by what a refused Next added (`_refused_step`).
        const errors = group
            .flatMap((el) => (el.getAttribute('aria-describedby') || '').split(/\s+/))
            .filter(Boolean)
            .map((id) => text(byId(id)))
            .filter(Boolean);
        if (errors.length) field.error = [...new Set(errors)].join(' ');
        fields.push(field);
    }
    const heading = dialog.querySelector('h3');
    const progress = dialog.querySelector('progress');
    const value = progress && progress.getAttribute('value');
    return {
        heading: text(heading),
        progress: value === null ? null : Number(value),
        fields,
        next: Boolean(dialog.querySelector('[data-easy-apply-next-button]')),
        signature: [text(heading), value, ...controls.map((el) => el.id || el.type)]
            .join('\n'),
    };
}"""
)

# The follow-the-company checkbox, which LinkedIn ships checked and puts in the
# footer the step program skips: it is not one of the form's questions, and
# nobody answering a job application means to follow the company by omission.
EASY_APPLY_FOLLOW_JS = (
    "() => {"
    + _EASY_APPLY_DIALOG_JS
    + r"""
    if (!dialog) return null;
    const form = dialog.querySelector('form') || dialog;
    const footer = form.querySelector('footer');
    const box = footer && footer.querySelector('input[type="checkbox"]');
    if (!box) return null;
    // Whether the control can be clicked at all. LinkedIn styles the box with
    // a label and may leave the input itself with nothing to click, which a
    // locator refuses to act on rather than clicking something else.
    const shown = Boolean(
        box.offsetWidth || box.offsetHeight || box.getClientRects().length
    );
    const labelled = Boolean(
        box.id && form.querySelector(`label[for="${CSS.escape(box.id)}"]`)
    );
    return {id: box.id || '', checked: box.checked, shown, labelled};
}"""
)

# What LinkedIn shows once the form dialog is gone. Any dialog will do, because
# the point is evidence that something answered the Submit click rather than the
# dialog closing on its own, and the caller is the one who reads it.
EASY_APPLY_CONFIRMATION_JS = (
    "() => {"
    + _SHADOW_ROOTS_JS
    + r"""
    for (const root of roots) {
        for (const box of root.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
            const text = (box.textContent || '').replace(/\s+/g, ' ').trim();
            if (text) return text.slice(0, 400);
        }
    }
    return '';
}"""
)

# The posting's own account of the application, for the submission LinkedIn
# answers without a modal. The lines `APPLY_SIGNALS_JS` reads for the applied
# state, returning the one that matched rather than whether one did: where no
# dialog renders, this line is the only thing the page says about what
# happened, and the posting is the surface that says it.
APPLIED_LINE_JS = (
    r"""(opts) => {
    const {descriptionHeadings, appliedPattern} = opts;
    const main = document.querySelector('main');
    if (!main) return '';
"""
    + _TOP_LINES_JS
    + r"""    const applied = new RegExp(appliedPattern);
    return top.find((line) => applied.test(line)) || '';
}"""
)

EASY_APPLY_NEXT_SELECTOR = "[data-easy-apply-next-button]"
EASY_APPLY_SUBMIT_SELECTOR = "[data-live-test-easy-apply-submit-button]"
EASY_APPLY_DISCARD_SELECTOR = '[data-control-name="discard_application_confirm_btn"]'

# The open dialog, named the same way the programs above name it. A control is
# addressed through it rather than by its id alone, because the posting behind
# the dialog is still in the document and an id is only unique within one tree.
_EASY_APPLY_DIALOG_PARTS = (
    '[role="dialog"]:has([data-easy-apply-next-button])',
    '[role="dialog"]:has([data-live-test-easy-apply-submit-button])',
    '[role="dialog"]:has([id*="easyApplyFormElement"])',
)


def easy_apply_control_selector(control_id: str) -> str:
    """A selector for one control of the open dialog, by its id.

    CSS locators pierce open shadow roots on their own, which is how this
    reaches a dialog the document's own `querySelector` never sees.
    """
    inside = f"[id={json.dumps(control_id)}]"
    return ", ".join(f"{part} {inside}" for part in _EASY_APPLY_DIALOG_PARTS)


# The footer checkbox addressed without an id, for a control LinkedIn left
# anonymous. Its own selector rather than the control one, which an id keys.
EASY_APPLY_FOLLOW_SELECTOR = ", ".join(
    f'{part} footer input[type="checkbox"]' for part in _EASY_APPLY_DIALOG_PARTS
)


def easy_apply_label_selector(control_id: str) -> str:
    """A selector for the label that stands in for one control of the dialog."""
    inside = f"label[for={json.dumps(control_id)}]"
    return ", ".join(f"{part} {inside}" for part in _EASY_APPLY_DIALOG_PARTS)


def easy_apply_option_selector(list_id: str | None) -> str:
    """The suggestions a typeahead is offering, by its list's id or by role.

    LinkedIn renders the list beside the input, inside the open shadow root
    that carries the whole dialog: the document's own `querySelector` finds no
    listbox and no option anywhere, while a CSS locator pierces the root and
    finds both. Not scoped to the dialog even so, which is the one selector
    here that is not, so a list rendered somewhere else is still reached.
    `aria-owns` narrows it to this field's own list, which LinkedIn sets while
    the list is open beside an `aria-controls` carrying the same id. Visible
    only, so a collapsed list left in the document is not mistaken for an open
    one.
    """
    if list_id:
        return f'[id={json.dumps(list_id)}] [role="option"]:visible'
    return '[role="listbox"] [role="option"]:visible'


# What one suggestion reads as, cleaned the way the step read cleans a label,
# so an answer that matches the label it was written against also matches the
# suggestion wearing it. LinkedIn repeats text in an aria-hidden copy.
EASY_APPLY_OPTION_TEXT_JS = r"""(nodes) => nodes.map((node) => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('[aria-hidden="true"]').forEach((el) => el.remove());
    return (copy.textContent || '').replace(/\s+/g, ' ').trim();
})"""


_EASY_APPLY_OPEN_TIMEOUT = 10.0
_EASY_APPLY_STEP_TIMEOUT = 8.0
_EASY_APPLY_SET_TIMEOUT = 5.0
_EASY_APPLY_SUBMIT_TIMEOUT = 15.0
# How long LinkedIn's account of a sent application gets to render. Short,
# because the submission is already sound without one: the dialog is gone.
_EASY_APPLY_CONFIRMATION_TIMEOUT = 5.0
# An upload is a round trip to LinkedIn and a rebuilt step afterwards.
_EASY_APPLY_UPLOAD_TIMEOUT = 30.0
_EASY_APPLY_FOLLOW_TRIES = 3
_EASY_APPLY_DISCARD_TIMEOUT = 5.0
# How long closing the dialog may hold the page once the walk has ended, a
# cancelled walk included: the wait for a dialog still opening, Escape, the
# Discard click and the wait for the dialog to go, with room for the reads
# between them.
_EASY_APPLY_CLOSE_GRACE = 4 * _EASY_APPLY_DISCARD_TIMEOUT
_EASY_APPLY_MAX_STEPS = 8
# How long a typeahead's list gets to arrive. It is a round trip to LinkedIn
# behind a debounce, so longer than setting a control and shorter than a step.
_EASY_APPLY_SUGGEST_TIMEOUT = 8.0
# How long a clicked suggestion gets to reach the field and close the list.
_EASY_APPLY_PICK_TIMEOUT = 3.0
# Between keystrokes. A typeahead that listens for keys rather than for the
# input event sees nothing of a value set in one shot, and one that debounces
# needs the gaps to be gaps.
_EASY_APPLY_TYPE_DELAY = 40
# A country list runs to 250 options; the prefilled value is still reported.
# A radio or checkbox group is cut the same way, and keeps every option checked.
_EASY_APPLY_MAX_OPTIONS = 50


@dataclass(frozen=True, slots=True)
class JobApplyRead:
    """How one posting takes applications, and where the employer's form is."""

    type: ApplyType
    url: str | None = None


# A search result card and its dismiss control, in both of LinkedIn's job
# searches, measured on signed-in en-US accounts. The job named only by
# `currentJobId` sits in the detail pane, which has no card and no dismiss
# control.
#
# - Classic `/jobs/search/` (2026-09-28): each result is an
#   `li[data-occludable-job-id]` holding the job id. The dismiss control is
#   the card's `button` around an icon with `data-test-icon="close-small"`
#   (some cards also carry a "More options" button around
#   `overflow-web-ios-small`).
# - Redesigned `/jobs/search-results/` (2026-10-14): each result is an element
#   whose `componentkey` is `job-card-component-ref-<job id>`, with a nested
#   element repeating that key, so only the outermost counts. The dismiss
#   control is its `button` around `svg#close-small`.
#
# In both, clicking it keeps the card and swaps the button for one around
# `undo-small`, which is the only undo there is: a reload drops the job from
# that search and every later one. Applied jobs carry the same control. The
# button's `aria-label` names the job in the account's language and is never
# read. More than one card for the id, or more than one dismiss button in it,
# is refused rather than guessed at.
SEARCH_CARD_JS = r"""(opts) => {
    const {jobId, mark} = opts;
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const refKey = '[componentkey^="job-card-component-ref-"]';
    const classic = [...document.querySelectorAll('li[data-occludable-job-id]')];
    const redesigned = [...document.querySelectorAll(refKey)].filter(
        (card) => !card.parentElement || !card.parentElement.closest(refKey)
    );
    const idOf = (card) => card.getAttribute('data-occludable-job-id')
        ?? card.getAttribute('componentkey').slice('job-card-component-ref-'.length);
    const cards = [...classic, ...redesigned];
    const own = cards.filter((card) => idOf(card) === jobId);
    const result = {cards: cards.length, matches: own.length, dismiss: 0, undo: 0};
    if (own.length !== 1) return result;
    const card = own[0];
    // Occluded cards render their content only near the viewport.
    card.scrollIntoView({block: 'center'});
    const around = (icon) => [...card.querySelectorAll('button')].filter(
        (button) => button.querySelector(
            '[data-test-icon="' + icon + '"], svg#' + icon
        )
    );
    const dismiss = around('close-small');
    result.dismiss = dismiss.length;
    result.undo = around('undo-small').length;
    if (dismiss.length === 1) dismiss[0].setAttribute(mark, '');
    return result;
}"""

_SEARCH_CARD_SELECTOR = (
    'li[data-occludable-job-id], [componentkey^="job-card-component-ref-"]'
)
# The job search parameters a landed search has to keep by value: what the
# search is about and where. The worldwide `geoId` is ours, never a location
# string LinkedIn turns into one, so its value is comparable.
_SEARCH_VALUE_PARAMS = frozenset({"keywords", "geoId"})
_DISMISS_MARK = "data-mcp-dismiss"

# How long a search gets to render its cards, a found card its buttons, and a
# clicked card its undo state.
_SEARCH_READY_TIMEOUT = 10.0
_SEARCH_CARD_RENDER_TIMEOUT = 5.0
_DISMISS_TIMEOUT = 5.0
# How long a posting gets to set its "<title> | <company> | LinkedIn" title.
_POSTING_TITLE_TIMEOUT = 10.0
_SEARCH_POLL = 0.25

SearchCardOutcome = Literal[
    # The dismiss button was clicked and the card turned to its undo state.
    "dismissed",
    # The card is already in its undo state.
    "already_dismissed",
    # No card for the job in the search's results, scrolled to the end.
    "absent",
    # The card is there but no single dismiss button could be told to be its.
    "no_button",
    # The button was clicked and the card did not change.
    "not_dismissed",
]


@dataclass(frozen=True, slots=True)
class EasyApplyFormRead:
    """A posting's apply type and, for Easy Apply, the form steps read.

    `complete` is False when steps after the last one were not read, and
    `discarded` is False when the dialog could not be closed, which may leave a
    saved draft on LinkedIn.
    """

    type: ApplyType
    steps: tuple[dict[str, Any], ...] = ()
    complete: bool = False
    discarded: bool = True


class EasyApplyDraftLeftError(RuntimeError):
    """A walk failed and its dialog could not be closed after it.

    Raised in place of the failure, which it chains, because the draft it may
    leave on LinkedIn is what the caller has to act on, and an error of its own
    type keeps that warning's `error_type` instead of the failure's.
    """


def _added_errors(before: dict[str, Any], after: dict[str, Any]) -> list[str | None]:
    """Per field of ``after``, the error text a click on ``before`` added.

    Both reads are of one step, so their fields line up. The text a field
    describes itself with is LinkedIn's error only when the click put it
    there: `aria-describedby` also names hints, and a hint is there before
    any click. Nothing in the markup tells the two apart in every locale; the
    click does.
    """
    return [
        field.get("error") if field.get("error") != prior.get("error") else None
        for field, prior in zip(after["fields"], before["fields"], strict=True)
    ]


def _refused_step(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """``after``, the step LinkedIn kept after Next, with only its new errors."""
    fields = []
    for field, error in zip(after["fields"], _added_errors(before, after), strict=True):
        field = {key: value for key, value in field.items() if key != "error"}
        if error:
            field["error"] = error
        fields.append(field)
    return {**after, "fields": fields}


def _unrefused_step(step: dict[str, Any]) -> dict[str, Any]:
    """``step`` without errors, for a step no click has been refused on."""
    return _refused_step(step, step)


@dataclass(frozen=True, slots=True)
class EasyApplySubmitRead:
    """What one answered Easy Apply run did, and what it left behind.

    `complete` is False when a step was refused or the form ran past the step
    cap, which is also why `submitted` can be False on a run that asked to
    submit. `follow` is the state the follow-the-company checkbox ended in, or
    None where the dialog had none, and `confirmation` is what LinkedIn said
    about the sent application, its own modal or the posting's applied line,
    empty where neither surface spoke before the deadline.
    """

    type: ApplyType
    steps: tuple[dict[str, Any], ...] = ()
    complete: bool = False
    submitted: bool = False
    follow: bool | None = None
    discarded: bool = True
    confirmation: str | None = None


@dataclass(frozen=True, slots=True)
class _WalkOutcome:
    """What the answering walk did, before the dialog is closed."""

    complete: bool
    submitted: bool = False
    follow: bool | None = None
    confirmation: str | None = None


def _step_identity(step: dict[str, Any]) -> tuple[str, Any]:
    """Which step of the form this is.

    Its heading and progress, not its `signature`. The signature carries the
    controls, and answering a file field re-renders the step around a new
    resume card: same step, new signature. The read walk can compare signatures
    because it types nothing; a walk that answers cannot.
    """
    return step["heading"], step["progress"]


def _step_errors(step: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields LinkedIn is complaining about."""
    return [field for field in step["fields"] if field.get("error")]


def _unanswered_required(
    step: dict[str, Any], answered: set[str]
) -> list[dict[str, Any]]:
    """The required fields of ``step`` holding nothing.

    Judged on the step as read back, so an answer that cleared a field, an
    empty text or a lone checkbox answered no, leaves it as missing as one
    nothing answered. The exception is a file answered on this step: a file
    input never reports the file it was given, and the upload is waited for
    until the form names it back instead.
    """
    missing = []
    for field in step["fields"]:
        if not field.get("required"):
            continue
        if field["type"] == "file" and field["label"] in answered:
            continue
        held = (
            field.get("selected")
            if field["type"] in ("radio", "checkbox")
            else field.get("value")
        )
        if not held:
            missing.append(field)
    return missing


def _public_step(step: dict[str, Any]) -> dict[str, Any]:
    """One read step as returned to the caller, without the walk's bookkeeping."""
    public: dict[str, Any] = {}
    if step["heading"]:
        public["heading"] = step["heading"]
    if step["progress"] is not None:
        public["progress"] = step["progress"]
    public["fields"] = [
        {key: value for key, value in field.items() if key != "ids"}
        for field in step["fields"]
    ]
    return public


@dataclass(frozen=True, slots=True)
class EasyApplyEdit:
    """One control of the open dialog, and what to do to it.

    Every answer is resolved into these before the first one is carried out,
    so an answer the form cannot take refuses while the dialog is still
    untouched. That matters more here than it reads: LinkedIn saves Easy Apply
    answers to the account and prefills them into the next application, so a
    half-filled step is not undone by discarding the draft.
    """

    control_id: str
    action: Literal["fill", "select", "check", "uncheck", "upload", "pick"]
    value: str


def normalise_easy_apply_text(value: str) -> str:
    """``value`` as it is compared against what LinkedIn shows.

    Composed to NFC and with runs of whitespace collapsed, so an answer written
    with a decomposed accent still matches the suggestion it names. Nothing
    beyond that: case folding and substring matching would let the server
    settle for a near miss, which is the one thing it must never do when the
    result is an entity the caller cannot see.
    """
    return unicodedata.normalize("NFC", " ".join(value.split()))


def _one_control(field: dict[str, Any]) -> str:
    """The id of the single control behind ``field``."""
    ids = field.get("ids") or []
    if len(ids) != 1 or not ids[0]:
        raise InvalidAnswerError(
            easy_apply_answer_refused(field, EASY_APPLY_NO_CONTROL)
        )
    return ids[0]


def _plan_choice(field: dict[str, Any], answer: EasyApplyAnswer) -> list[EasyApplyEdit]:
    """The edits that leave a radio or checkbox group holding ``answer``.

    A radio takes one option and checking it clears the rest, so only the
    wanted option is ever touched. A checkbox group is set to exactly what the
    answer names, because answering the question at all claims its whole value.
    """
    options: list[str] = field.get("options") or []
    selected: set[str] = set(field.get("selected") or [])
    ids: list[str] = field.get("ids") or []
    radio = field["type"] == "radio"

    total = field.get("options_total")
    if total:
        # Unlike a select's, a choice is clicked by the control its label
        # stands for, and the labels past the cut were never read.
        raise InvalidAnswerError(
            easy_apply_answer_refused(
                field,
                f"It lists {total} options and only the first {len(options)} "
                "were read, so none can be clicked by name.",
            )
        )

    if isinstance(answer, bool):
        if len(options) != 1:
            raise InvalidAnswerError(
                easy_apply_answer_refused(
                    field, "Yes or no answers only one option. Name the option instead."
                )
            )
        wanted = set(options) if answer else set()
    else:
        wanted = {answer} if isinstance(answer, str) else set(answer)

    if radio and len(wanted) != 1:
        raise InvalidAnswerError(
            easy_apply_answer_refused(field, "Exactly one option answers this field.")
        )
    missing = sorted(wanted - set(options))
    if missing:
        raise InvalidAnswerError(
            easy_apply_answer_refused(
                field,
                "No option is labelled "
                + ", ".join(repr(name) for name in missing)
                + ".",
            )
        )
    if len(set(options)) != len(options) or len(ids) != len(options):
        raise InvalidAnswerError(
            easy_apply_answer_refused(
                field, "Its options do not each carry one distinct label and control."
            )
        )

    edits: list[EasyApplyEdit] = []
    for option, control_id in zip(options, ids, strict=True):
        if option in wanted and option not in selected:
            action: Literal["check", "uncheck"] = "check"
        elif not radio and option not in wanted and option in selected:
            action = "uncheck"
        else:
            continue
        if not control_id:
            raise InvalidAnswerError(
                easy_apply_answer_refused(field, EASY_APPLY_NO_CONTROL)
            )
        edits.append(EasyApplyEdit(control_id, action, option))
    return edits


def plan_easy_apply_answer(
    field: dict[str, Any], answer: EasyApplyAnswer
) -> list[EasyApplyEdit]:
    """The edits that leave ``field`` holding ``answer``, or a refusal.

    Nothing is inferred and nothing is adjusted to fit: a value the field
    cannot take refuses, naming the field and what it accepts, rather than
    going in as the nearest thing that would.
    """
    kind = field["type"]
    if kind == "typeahead":
        control_id = _one_control(field)
        if not isinstance(answer, str):
            raise InvalidAnswerError(
                easy_apply_answer_refused(field, "It takes one value, written out.")
            )
        # A prefill that already is the answer is left where it is. Retyping it
        # would throw away LinkedIn's own binding of that text to an entity for
        # the chance of picking the same one back.
        held = field.get("value") or ""
        if held and normalise_easy_apply_text(held) == normalise_easy_apply_text(
            answer
        ):
            return []
        return [EasyApplyEdit(control_id, "pick", answer)]
    if kind in ("radio", "checkbox"):
        return _plan_choice(field, answer)

    control_id = _one_control(field)
    if not isinstance(answer, str):
        raise InvalidAnswerError(
            easy_apply_answer_refused(field, "It takes one value, written out.")
        )
    if kind == "select":
        # A country list runs past the read's cap and comes back cut, with its
        # real length beside it. The options that were kept cannot say the
        # answer is wrong, so a cut list is left for the browser to match and
        # refuse; only a whole one is checked here.
        options = field.get("options") or []
        if answer not in options and not field.get("options_total"):
            raise InvalidAnswerError(
                easy_apply_answer_refused(field, f"No option is labelled {answer!r}.")
            )
        return [EasyApplyEdit(control_id, "select", answer)]
    if kind == "file":
        path = Path(answer).expanduser()
        if not path.is_file():
            raise InvalidAnswerError(
                easy_apply_answer_refused(field, f"There is no file at {answer!r}.")
            )
        return [EasyApplyEdit(control_id, "upload", str(path))]

    limit = field.get("max_length")
    if limit and len(answer) > limit:
        raise InvalidAnswerError(
            easy_apply_answer_refused(
                field,
                f"The answer is {len(answer)} characters and the field holds "
                f"{limit}, so LinkedIn would cut it.",
            )
        )
    return [EasyApplyEdit(control_id, "fill", answer)]


def plan_easy_apply_step(
    step: dict[str, Any], answers: Mapping[str, EasyApplyAnswer]
) -> tuple[list[EasyApplyEdit], set[str]]:
    """The edits one step takes from ``answers``, and the labels they used.

    A field no answer names keeps whatever LinkedIn prefilled. A label this
    step does not carry is left for another step to claim; what becomes of one
    no step claims is the walk's to decide, once every step has been seen.

    An empty typeahead no answer names refuses here rather than at the end of
    the step, because no prefill means there is nothing to keep and LinkedIn
    will not take the step without it. Naming it is what makes it answerable.

    Picks come back first. Every other answer is resolved before the first one
    is carried out, so a refusal cannot leave a half-filled step; a pick only
    learns whether it matches after its value has been typed into the page,
    which breaks that. Running the picks first is what restores the intent — a
    pick that finds no suggestion leaves the rest of the dialog as LinkedIn
    prefilled it.
    """
    for field in step["fields"]:
        if (
            field["type"] == "typeahead"
            and not field.get("value")
            and field["label"] not in answers
        ):
            raise InvalidAnswerError(
                easy_apply_answer_refused(field, EASY_APPLY_TYPEAHEAD_REFUSED)
            )

    by_label: dict[str, dict[str, Any]] = {}
    duplicated: set[str] = set()
    for field in step["fields"]:
        if field["label"] in by_label:
            duplicated.add(field["label"])
        by_label[field["label"]] = field

    edits: list[EasyApplyEdit] = []
    used: set[str] = set()
    for label, answer in answers.items():
        field = by_label.get(label)
        if field is None:
            continue
        if label in duplicated:
            raise InvalidAnswerError(easy_apply_duplicate_label_error(label))
        edits.extend(plan_easy_apply_answer(field, answer))
        used.add(label)
    edits.sort(key=lambda edit: edit.action != "pick")
    return edits, used


def refuse_unused_easy_apply_answers(
    answers: Mapping[str, EasyApplyAnswer], used: set[str]
) -> None:
    """Refuse when the form asked nothing for some of ``answers``."""
    unused = [label for label in answers if label not in used]
    if unused:
        raise InvalidAnswerError(easy_apply_unknown_answers_error(unused))


@dataclass(frozen=True, slots=True)
class JobPageCapture:
    """One job-list page read, plus what the reader alone can still answer.

    Everything a page attempt used to leave behind on the extractor travels
    here instead. That was one field, `_scroll_seconds`, and it was the only
    state crossing the page-attempt boundary without being a parameter or a
    return value; the budgets, the offset, the seen ids and the warnings were
    always the workflow's own locals and stay there.

    `landed_url` is where the browser actually is when the attempt ends, taken
    inside the reader because that is the same read the caller would make:
    nothing between this value and the caller's first look at the address
    awaits anything. The saved-jobs offset check is the one place that may not
    use it, because a page-count read runs first and can move the address; see
    `current_url`.
    """

    section: ExtractedSection
    landed_url: str
    scroll_seconds: float


@dataclass(slots=True)
class _ScrollCharge:
    """What the sidebar scroll has spent on one page, across both attempts.

    Mutable and private, unlike the capture built from it: the scroll books
    its own time in a `finally`, so an attempt that raises after scrolling
    still has to be charged, and a return value cannot carry that. It never
    leaves this module.
    """

    seconds: float = 0.0


class JobPageReader:
    """Read one job-search or saved-job page at a time.

    A page service under the job workflows rather than a peer of them. It
    consumes `PageNavigator` for navigation watching, document-origin checks
    and settling, and answers with a `JobPageCapture`; which page to ask for
    next, and what an answer means, belong to `jobs.JobReader`.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

    @property
    def current_url(self) -> str:
        """Where the browser is right now, with nothing awaited to find out.

        The saved-jobs offset check needs the address as it stands *after* the
        page-count read, which the capture's `landed_url` predates. Neither an
        await nor a document-identity check on purpose: adding either would be
        a new observation on a path that has none today.
        """
        return self._session.page.url

    def _captured(
        self, section: ExtractedSection, scroll_seconds: float = 0.0
    ) -> JobPageCapture:
        """Seal one page attempt together with where it ended."""
        return JobPageCapture(
            section=section,
            landed_url=self._session.page.url,
            scroll_seconds=scroll_seconds,
        )

    async def read_apply_link(
        self, url: str, job_id: str, text: JobApplyTextTable
    ) -> JobApplyRead:
        """Read how a posting takes applications, and an external one's address.

        Nothing is clicked. An external Apply is a link whose href names the
        employer's address; a posting whose Apply is anything else answers
        `unknown`.

        The employer's address is answered as LinkedIn gives it and never
        loaded. It comes from whoever posted the job, and loading it would let
        a stranger's link, or any redirect behind it, send a request from this
        host to whatever the host can reach.
        """
        kind, link = await self._read_apply_type(url, job_id, text)
        if kind == "external" and link is not None:
            return JobApplyRead("external", employer_apply_url(link))
        return JobApplyRead(kind)

    async def read_easy_apply_form(
        self, url: str, job_id: str, text: JobApplyTextTable
    ) -> EasyApplyFormRead:
        """Read an Easy Apply form step by step without answering it.

        Any posting that is not Easy Apply answers with its type alone. Next is
        clicked while LinkedIn accepts a step on its prefilled values, and the
        first step it refuses ends the read. Nothing is typed, because LinkedIn
        saves Easy Apply answers and prefills them into later applications.
        The dialog is then closed and the draft it offers to save discarded.
        """
        kind, _ = await self._read_apply_type(url, job_id, text)
        if kind != "easy_apply":
            return EasyApplyFormRead(kind)

        steps: list[dict[str, Any]] = []
        try:
            complete = await self._walk_easy_apply(job_id, steps)
        except asyncio.CancelledError:
            # A cancel can land between the click on Easy Apply and the dialog
            # rendering, and the click goes on without the task that sent it.
            await self._close_easy_apply(opening=True)
            raise
        except Exception as e:
            if not await self._close_easy_apply():
                raise EasyApplyDraftLeftError(EASY_APPLY_DRAFT_LEFT) from e
            raise
        discarded = await self._close_easy_apply()
        return EasyApplyFormRead("easy_apply", tuple(steps), complete, discarded)

    async def _walk_easy_apply(self, job_id: str, steps: list[dict[str, Any]]) -> bool:
        """Open the dialog and read its steps into ``steps``; True when all were read."""
        page = self._session.page
        route = page.locator(f'main a[href*="/jobs/view/{job_id}/apply"]')
        if not await route.count():
            raise RuntimeError(EASY_APPLY_BUTTON_UNREAD)
        await route.first.click(timeout=5000)
        try:
            await page.wait_for_function(
                EASY_APPLY_OPEN_JS, timeout=_EASY_APPLY_OPEN_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            raise RuntimeError("The Easy Apply dialog did not open") from None

        step = await self._read_easy_apply_step(None)
        while True:
            # Nothing has been asked of this step yet, so nothing on it is an
            # answer to a click.
            steps.append(_public_step(_unrefused_step(step)))
            if not step["next"]:
                return True
            if len(steps) >= _EASY_APPLY_MAX_STEPS:
                return False
            # The only control ever clicked in the dialog. Found by its
            # attribute: a one-step form holds Submit in the same footer slot.
            await page.locator(EASY_APPLY_NEXT_SELECTOR).click(timeout=5000)
            after = await self._read_easy_apply_step(step)
            if after["signature"] == step["signature"]:
                # Refused: the same step, now carrying LinkedIn's errors.
                steps[-1] = _public_step(_refused_step(step, after))
                return False
            step = after

    async def submit_easy_apply(
        self,
        url: str,
        job_id: str,
        text: JobApplyTextTable,
        answers: Mapping[str, EasyApplyAnswer],
        *,
        confirm_submit: bool,
        follow_company: bool = False,
    ) -> EasyApplySubmitRead:
        """Answer a posting's Easy Apply form, and send it when asked to.

        Every step is filled from ``answers`` alone, by the label the form read
        returned, and a field no answer names keeps LinkedIn's prefill. Submit
        is clicked only when ``confirm_submit`` is set and only once every step was
        reached with its required fields holding something; without it the form
        is filled, read back and the draft discarded, which is the rehearsal
        for the real thing.

        Both are writes. LinkedIn saves Easy Apply answers to the account and
        prefills them into the next application, and an uploaded resume becomes
        the preselected one, so a run that submits nothing still changes what
        the next form starts from.

        An uploaded file is waited for until the form names it back, because
        that is the only account LinkedIn gives of what it is holding.
        """
        kind, _ = await self._read_apply_type(url, job_id, text)
        if kind != "easy_apply":
            return EasyApplySubmitRead(kind)

        steps: list[dict[str, Any]] = []
        try:
            outcome = await self._walk_easy_apply_answers(
                job_id,
                text,
                answers,
                steps,
                confirm_submit=confirm_submit,
                follow_company=follow_company,
            )
        except asyncio.CancelledError:
            # As in `read_easy_apply_form`. Answers already typed stay saved to
            # the account either way; the draft holding them does not.
            await self._close_easy_apply(opening=True)
            raise
        except Exception as e:
            if not await self._close_easy_apply():
                raise EasyApplyDraftLeftError(EASY_APPLY_DRAFT_LEFT) from e
            raise
        return EasyApplySubmitRead(
            "easy_apply",
            tuple(steps),
            complete=outcome.complete,
            submitted=outcome.submitted,
            follow=outcome.follow,
            # A sent application leaves no dialog and no draft to discard.
            discarded=True if outcome.submitted else await self._close_easy_apply(),
            confirmation=outcome.confirmation,
        )

    async def _walk_easy_apply_answers(
        self,
        job_id: str,
        text: JobApplyTextTable,
        answers: Mapping[str, EasyApplyAnswer],
        steps: list[dict[str, Any]],
        *,
        confirm_submit: bool,
        follow_company: bool,
    ) -> _WalkOutcome:
        """Open the dialog, answer it step by step, and end on the last one."""
        page = self._session.page
        route = page.locator(f'main a[href*="/jobs/view/{job_id}/apply"]')
        if not await route.count():
            raise RuntimeError(EASY_APPLY_BUTTON_UNREAD)
        await route.first.click(timeout=5000)
        try:
            await page.wait_for_function(
                EASY_APPLY_OPEN_JS, timeout=_EASY_APPLY_OPEN_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            raise RuntimeError("The Easy Apply dialog did not open") from None

        used: set[str] = set()
        step = await self._read_easy_apply_step(None)
        while True:
            answered, uploaded = await self._answer_easy_apply_step(step, answers)
            if answered:
                # Read again rather than reason about what was typed.
                step = (
                    await self._settle_easy_apply_upload(uploaded)
                    if uploaded
                    else await self._read_easy_apply_step(None)
                )
                used |= answered
            missing = _unanswered_required(step, answered)
            if missing:
                raise InvalidAnswerError(easy_apply_required_unanswered_error(missing))
            steps.append(_public_step(step))
            if not step["next"]:
                break
            if len(steps) >= _EASY_APPLY_MAX_STEPS:
                return _WalkOutcome(complete=False)
            await page.locator(EASY_APPLY_NEXT_SELECTOR).click(timeout=5000)
            after = await self._read_easy_apply_step(step)
            if _step_errors(after) or _step_identity(after) == _step_identity(step):
                # Refused, or standing still and saying nothing about why.
                steps[-1] = _public_step(after)
                return _WalkOutcome(complete=False)
            step = after

        # Now that every step has been seen, an answer none of them asked for
        # is a stale answers file, and this is the last moment to say so: the
        # draft is still discardable and nothing has been sent.
        refuse_unused_easy_apply_answers(answers, used)

        follow = await self._set_follow_company(follow_company)
        if not confirm_submit:
            return _WalkOutcome(complete=True, follow=follow)
        confirmation = await self._click_easy_apply_submit(text)
        return _WalkOutcome(
            complete=True, submitted=True, follow=follow, confirmation=confirmation
        )

    async def _settle_easy_apply_upload(self, names: set[str]) -> dict[str, Any]:
        """The step once LinkedIn has named every file in ``names`` back.

        Waiting for the step to change is not enough, and reading it once it
        stops changing is not either. An upload tears the resume list down
        before the file arrives, and the torn-down list holds still: two reads
        agree on it, and what comes back is a form with no resume on it at all.
        Measured on a live application, which is how this is known.

        So the file itself is the signal, and nothing weaker stands in for it.
        LinkedIn names an uploaded resume on the card it renders for it, which
        is the only account this side gets of what it is holding. Refusing a
        file it never names costs a run that changed nothing; accepting one
        costs an application sent with the resume it was meant to replace.
        """
        deadline = self._session.monotonic() + _EASY_APPLY_UPLOAD_TIMEOUT
        while True:
            step = await self._read_easy_apply_step(None)
            missing = sorted(
                name
                for name in names
                if not any(name in field["label"] for field in step["fields"])
            )
            if not missing:
                return step
            if self._session.monotonic() >= deadline:
                raise InvalidAnswerError(easy_apply_upload_unconfirmed_error(missing))
            await self._session.delay(_APPLY_POLL)

    async def _set_follow_company(self, follow: bool) -> bool | None:
        """Leave the follow checkbox as ``follow`` asks; answers its real state.

        Tried more than one way, because the box measured live took none of
        them: LinkedIn styles it with a label, and the input behind it is not
        something a locator will click. Reported rather than assumed either
        way, and never worth failing an application over: a checkbox that
        cannot be reached costs the caller a company in their feed, which is
        not the irreversible half of this tool.
        """
        page = self._session.page
        box = await page.evaluate(EASY_APPLY_FOLLOW_JS)
        if box is None:
            return None
        for attempt in range(_EASY_APPLY_FOLLOW_TRIES):
            if box["checked"] == follow:
                break
            try:
                await self._toggle_follow(box, attempt)
            except Exception as e:
                logger.warning("Could not set the follow checkbox (%s)", e)
            await self._session.delay(_APPLY_POLL)
            box = await page.evaluate(EASY_APPLY_FOLLOW_JS)
            if box is None:
                return None
        if box["checked"] != follow:
            logger.warning(
                "The follow checkbox stayed %s",
                "checked" if box["checked"] else "clear",
            )
        return bool(box["checked"])

    async def _toggle_follow(self, box: dict[str, Any], attempt: int) -> None:
        """One go at the follow checkbox, from the most honest way down.

        `check` is what a checkbox is for and it goes first, on the id where
        LinkedIn gave the control one and on the footer's own checkbox where it
        did not. It refuses a control with nothing to click, and the way past
        that is the label: clicking it is what a person does, and the browser
        toggles the box itself.
        """
        page = self._session.page
        timeout = _EASY_APPLY_SET_TIMEOUT * 1000
        label = page.locator(easy_apply_label_selector(box["id"])).first
        if attempt and box["labelled"]:
            await label.click(timeout=timeout)
            return
        if not box["shown"]:
            if not box["labelled"]:
                raise RuntimeError("The follow checkbox has nothing to click")
            await label.click(timeout=timeout)
            return
        selector = (
            easy_apply_control_selector(box["id"])
            if box["id"]
            else EASY_APPLY_FOLLOW_SELECTOR
        )
        control = page.locator(selector).first
        if box["checked"]:
            await control.uncheck(timeout=timeout)
        else:
            await control.check(timeout=timeout)

    async def _click_easy_apply_submit(self, text: JobApplyTextTable) -> str:
        """Send the application; answers with what LinkedIn showed afterwards.

        The one click that cannot be taken back. A dialog still holding the
        form after it is a submission that did not happen, and it is raised
        rather than reported, so that nothing downstream records an application
        on the strength of a click alone.
        """
        page = self._session.page
        await page.locator(EASY_APPLY_SUBMIT_SELECTOR).first.click(
            timeout=_EASY_APPLY_SET_TIMEOUT * 1000
        )
        try:
            await page.wait_for_function(
                EASY_APPLY_CLOSED_JS, timeout=_EASY_APPLY_SUBMIT_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(EASY_APPLY_SUBMIT_UNCONFIRMED) from None
        return await self._read_easy_apply_confirmation(text)

    async def _read_easy_apply_confirmation(self, text: JobApplyTextTable) -> str:
        """What LinkedIn says about the application once the form is gone.

        The closing is the evidence that the click landed; this is the account
        of it, and there are two surfaces that give one. LinkedIn's own modal
        is the direct one and it does not always come: measured on 2026-09-23,
        an application that landed closed the form and rendered no dialog at
        all, which left this empty on a submission the posting itself reported
        as sent. So the posting's applied line stands in, the same line
        `read_apply_link` reads, and reaching it is not a fallback on weaker
        evidence: the modal is LinkedIn narrating the click, while the line is
        the posting's state having changed. Both are polled, the modal first
        within a round because it is the one that renders immediately. Neither
        is waited for past a few seconds, and an empty answer means only that
        neither spoke, never that nothing was sent.
        """
        page = self._session.page
        opts = {
            "descriptionHeadings": list(text.description_headings),
            "appliedPattern": text.applied_pattern.pattern,
        }
        deadline = self._session.monotonic() + _EASY_APPLY_CONFIRMATION_TIMEOUT
        while True:
            confirmation = await page.evaluate(EASY_APPLY_CONFIRMATION_JS)
            if confirmation:
                return confirmation
            # The posting could not have been showing this before the click:
            # a posting that reads as applied never reaches the form at all.
            applied = await page.evaluate(APPLIED_LINE_JS, opts)
            if applied:
                return applied
            if self._session.monotonic() >= deadline:
                logger.info("A sent application showed no confirmation of its own")
                return ""
            await self._session.delay(_APPLY_POLL)

    async def _answer_easy_apply_step(
        self, step: dict[str, Any], answers: Mapping[str, EasyApplyAnswer]
    ) -> tuple[set[str], set[str]]:
        """Fill ``step`` from ``answers``; answers the labels it took, and the
        names of any files it uploaded.

        A file is the one answer that travels: LinkedIn takes the upload, tears
        the step's resume list down and rebuilds it around the new file. The
        names come back because the rebuilt list showing them is the only
        evidence that the upload landed.

        The whole step is resolved before anything is touched, so a refusal
        leaves the dialog holding what LinkedIn prefilled. Every control is
        reached through a Playwright locator rather than through a script: the
        dialog is React, and a value written straight onto an element is not an
        answer it has seen.
        """
        edits, used = plan_easy_apply_step(step, answers)
        page = self._session.page
        by_control = {
            control_id: field
            for field in step["fields"]
            for control_id in field.get("ids") or []
        }
        for edit in edits:
            control = page.locator(easy_apply_control_selector(edit.control_id)).first
            timeout = _EASY_APPLY_SET_TIMEOUT * 1000
            if edit.action == "fill":
                await control.fill(edit.value, timeout=timeout)
            elif edit.action == "pick":
                await self._pick_easy_apply_suggestion(
                    by_control.get(edit.control_id, {}), control, edit.value
                )
            elif edit.action == "select":
                await control.select_option(label=edit.value, timeout=timeout)
            elif edit.action == "check":
                await control.check(timeout=timeout)
            elif edit.action == "uncheck":
                await control.uncheck(timeout=timeout)
            else:
                # The one control set while invisible: LinkedIn hides the file
                # input behind an upload button, and an upload is the one thing
                # a locator does without the element being shown.
                await control.set_input_files(edit.value, timeout=timeout)
        return used, {
            Path(edit.value).name for edit in edits if edit.action == "upload"
        }

    async def _pick_easy_apply_suggestion(
        self, field: dict[str, Any], control: Locator, answer: str
    ) -> None:
        """Leave the typeahead behind ``control`` holding the suggestion
        LinkedIn offers for ``answer``.

        Typed one character at a time, because a value set in one shot reaches
        a component listening for React's change event and nothing at all of
        one listening for keystrokes, and a debounce needs the gaps to be gaps.
        What lands in the field is then a suggestion LinkedIn itself offered,
        clicked by the text it wears: the server never composes the value, and
        it never settles for the nearest suggestion to one that is missing.

        Keyboard selection is deliberately not used. It picks by position
        rather than by what the option says, and its `Enter` reaches the form
        the field sits in whenever no list is open, which in this dialog is a
        step advanced on an answer nobody checked. LinkedIn leaves
        `aria-activedescendant` empty while the list stands open, so there is
        not even a reading of which option a keystroke would take.

        A pick that does not land clears the field before refusing, so the
        typed fragment is not left behind as the answer.
        """
        wanted = normalise_easy_apply_text(answer)
        await self._clear_easy_apply_control(control)
        await control.press_sequentially(
            answer,
            delay=_EASY_APPLY_TYPE_DELAY,
            timeout=_EASY_APPLY_SET_TIMEOUT * 1000,
        )

        offered: list[str] = []
        deadline = self._session.monotonic() + _EASY_APPLY_SUGGEST_TIMEOUT
        while True:
            options, offered = await self._easy_apply_suggestions(control)
            match = next(
                (
                    index
                    for index, text in enumerate(offered)
                    if normalise_easy_apply_text(text) == wanted
                ),
                None,
            )
            if match is not None:
                break
            if self._session.monotonic() >= deadline:
                await self._clear_easy_apply_control(control)
                raise InvalidAnswerError(
                    easy_apply_no_suggestion_error(field, answer, offered)
                )
            await self._session.delay(_APPLY_POLL)

        await options.nth(match).click(timeout=_EASY_APPLY_SET_TIMEOUT * 1000)

        # The click landed on a suggestion; whether LinkedIn took it is its own
        # answer. The field holding the text is not enough on its own, because
        # an answer spelled out in full holds it before anything is picked. The
        # list closing is what says a choice was made.
        deadline = self._session.monotonic() + _EASY_APPLY_PICK_TIMEOUT
        while True:
            held = await control.input_value(timeout=_EASY_APPLY_SET_TIMEOUT * 1000)
            _, still_offered = await self._easy_apply_suggestions(control)
            if normalise_easy_apply_text(held) == wanted and not still_offered:
                return
            if self._session.monotonic() >= deadline:
                await self._clear_easy_apply_control(control)
                raise InvalidAnswerError(
                    easy_apply_pick_unconfirmed_error(field, answer, held)
                )
            await self._session.delay(_APPLY_POLL)

    async def _easy_apply_suggestions(
        self, control: Locator
    ) -> tuple[Locator, list[str]]:
        """The suggestions open under ``control``, and what each one reads as.

        Addressed through the list the field names in `aria-owns` or
        `aria-controls` when it names one, and by role when it does not or when
        the named list holds nothing. Both are attribute presence and ARIA
        roles, so neither depends on the language the page is served in.
        LinkedIn sets both attributes to the same id, and only while the list
        is open; the role fallback is what carries a page that names nothing.
        """
        page = self._session.page
        named = (await control.get_attribute("aria-owns")) or (
            await control.get_attribute("aria-controls")
        )
        ids = named.split() if named else []
        list_id = ids[0] if ids else None
        options = page.locator(easy_apply_option_selector(list_id))
        texts = await options.evaluate_all(EASY_APPLY_OPTION_TEXT_JS)
        if not texts and list_id:
            options = page.locator(easy_apply_option_selector(None))
            texts = await options.evaluate_all(EASY_APPLY_OPTION_TEXT_JS)
        return options, texts

    @staticmethod
    async def _clear_easy_apply_control(control: Locator) -> None:
        """Empty ``control`` through the keyboard.

        A programmatic clear has the same blind spot as a programmatic fill: it
        never reaches a component that listens for keystrokes, which would then
        keep offering suggestions for a query the field no longer holds.
        """
        timeout = _EASY_APPLY_SET_TIMEOUT * 1000
        await control.click(timeout=timeout)
        await control.press("ControlOrMeta+a", timeout=timeout)
        await control.press("Delete", timeout=timeout)

    async def _read_easy_apply_step(
        self, previous: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The step the dialog settles on after ``previous``.

        Settled means two reads in a row agree, and they differ from
        ``previous`` or carry an error ``previous`` did not. A step that does
        neither by the deadline is returned as it stands. Text a field already
        described itself with is a hint, and settling on it would take a step
        that Next is still moving off for a refused one.
        """
        page = self._session.page
        deadline = self._session.monotonic() + _EASY_APPLY_STEP_TIMEOUT
        last: dict[str, Any] | None = None
        while True:
            step = await page.evaluate(EASY_APPLY_STEP_JS, _EASY_APPLY_MAX_OPTIONS)
            if step is None:
                raise RuntimeError("The Easy Apply dialog closed while it was read")
            moved = previous is None or step["signature"] != previous["signature"]
            refused = not moved and any(_added_errors(previous, step))
            if (
                last is not None
                and last["signature"] == step["signature"]
                and (moved or refused)
            ):
                return step
            if self._session.monotonic() >= deadline:
                return step
            last = step
            await self._session.delay(_APPLY_POLL)

    async def _close_easy_apply(self, *, opening: bool = False) -> bool:
        """Discard the dialog however the walk ended; True once it is gone.

        Shielded from AnyIO as well as from asyncio, for the reason
        `_let_the_scroll_land` is: a tool timeout cancels the task again on
        every pass of the event loop, and would end the discard at its first
        await with the dialog still open on the shared page and a draft that
        LinkedIn resumes the next time Easy Apply is opened. Bounded by
        `_EASY_APPLY_CLOSE_GRACE`, so a page that never answers cannot hold
        the page lock. A cancel that arrives meanwhile is raised once the
        discard has ended.

        ``opening`` gives a dialog that is not open yet a moment to open first,
        for a walk cancelled after its click on Easy Apply was sent.
        """
        grace = _EASY_APPLY_DISCARD_TIMEOUT if opening else 0.0
        close = asyncio.ensure_future(self._discard_easy_apply(grace))
        cancel: asyncio.CancelledError | None = None
        until = time.monotonic() + _EASY_APPLY_CLOSE_GRACE
        with anyio.CancelScope(shield=True):
            while not close.done() and time.monotonic() < until:
                try:
                    await asyncio.wait({close}, timeout=until - time.monotonic())
                except asyncio.CancelledError as e:
                    cancel = e
        if close.done():
            discarded = close.result()
        else:
            logger.warning(
                "The Easy Apply dialog was not closed within %.0fs",
                _EASY_APPLY_CLOSE_GRACE,
            )
            close.cancel()
            discarded = False
        if cancel is not None:
            raise cancel
        # A deadline that expired inside the shield is delivered here, so a
        # walk that ends past it does not report a result.
        await anyio.lowlevel.checkpoint()
        return discarded

    async def _discard_easy_apply(self, opening_grace: float = 0.0) -> bool:
        """Close the Easy Apply dialog and discard its draft; True once it is gone.

        Escape rather than the dialog's close button, which other dialogs carry
        too. LinkedIn answers with "Save this application?", and only its
        Discard, found by attribute, is clicked. A dialog that is not open is
        waited for up to ``opening_grace`` seconds before it counts as gone.
        """
        page = self._session.page
        try:
            if not await page.evaluate(EASY_APPLY_OPEN_JS):
                if not opening_grace:
                    return True
                try:
                    await page.wait_for_function(
                        EASY_APPLY_OPEN_JS, timeout=opening_grace * 1000
                    )
                except PlaywrightTimeoutError:
                    return True
            await page.keyboard.press("Escape")
            with suppress(PlaywrightTimeoutError):
                await page.locator(EASY_APPLY_DISCARD_SELECTOR).first.click(
                    timeout=_EASY_APPLY_DISCARD_TIMEOUT * 1000
                )
            await page.wait_for_function(
                EASY_APPLY_CLOSED_JS, timeout=_EASY_APPLY_DISCARD_TIMEOUT * 1000
            )
            return True
        except Exception as e:
            logger.warning("Could not discard the Easy Apply dialog (%s)", e)
            return False

    async def _read_apply_type(
        self, url: str, job_id: str, text: JobApplyTextTable
    ) -> tuple[ApplyType, str | None]:
        """Open the posting and read its apply type, without clicking anything.

        An external posting comes with its Apply link's href.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        opts = {
            "applyPath": f"/jobs/view/{job_id}/apply",
            "redirectPath": SAFETY_REDIRECT_PATH,
            "easyApplyLabel": text.easy_apply_label,
            "externalLabel": text.external_apply_label,
            "descriptionHeadings": list(text.description_headings),
            "closedLines": list(text.closed_lines),
            "appliedPattern": text.applied_pattern.pattern,
        }
        try:
            await page.wait_for_function(
                APPLY_READY_JS, arg=opts, timeout=_APPLY_READY_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            logger.debug("No apply control or posting state rendered on %s", url)

        signals = await self._session.run_on_linkedin(APPLY_SIGNALS_JS, opts)
        # Both states before any control, so a posting in either never reads
        # as open.
        if signals and signals["applied"]:
            return "applied", None
        if signals and signals["closed"]:
            return "closed", None
        if signals and signals["easy_apply"]:
            return "easy_apply", None
        if signals and signals["external_link"]:
            return "external", signals["external_link"]
        # A barrier served in place of the posting renders none of the above
        # either, and it needs the relogin path rather than a type.
        await self._navigator._raise_if_auth_barrier(url)
        return "unknown", None

    async def _extract_job_ids(self, *, scoped: bool = False) -> list[str]:
        """Extract unique job IDs from job card links on the current page.

        Finds all `a[href*="/jobs/view/"]` links and extracts the numeric
        job ID from each href. Returns deduplicated IDs in DOM order.

        Args:
            scoped: Read only the results rail, chosen by the same rule the
                sidebar scroll uses. Off for lists that have no rail.
        """
        result = await self._session.run_on_linkedin(
            JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "scoped": scoped}
        )
        if scoped and not result["scoped"]:
            # The whole document, because a page with nothing scrollable
            # rendered everything it has and returning no ids at all would
            # lose the results along with the detail pane's links. Said out
            # loud, because it is the one path where the offset can count
            # something the rail never showed, and it has not been observed:
            # live a search page has two scrollable candidates.
            logger.warning(
                "No results rail on %s, reading job ids from the whole document",
                self._session.page.url,
            )
        return result["ids"]

    async def _extract_promoted_job_ids(self, label: str) -> list[str]:
        """Ids of the results rail's cards that carry ``label`` as a line.

        Raises when the page answers with anything but a list, so a caller
        treating this as best effort cannot mistake a failed read for a page
        without promoted jobs.
        """
        result = await self._session.run_on_linkedin(
            PROMOTED_JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "label": label}
        )
        if not isinstance(result, list):
            raise TypeError(f"Promoted job ids read returned {type(result).__name__}")
        return [job_id for job_id in result if isinstance(job_id, str)]

    async def _count_organic_before_notice(self, notice: str, label: str) -> int | None:
        """Unpromoted results above ``notice``, or None without the notice.

        Raises when the page answers with anything else, so a caller treating
        this as best effort cannot read a failed read as zero, which would
        discard the page as a no-match.
        """
        result = await self._session.run_on_linkedin(
            ORGANIC_BEFORE_NOTICE_JS,
            {"selector": _JOB_CARD_SELECTOR, "label": label, "notice": notice},
        )
        if result is None:
            return None
        if isinstance(result, bool) or not isinstance(result, int):
            raise TypeError(f"Organic results read returned {type(result).__name__}")
        return result

    async def _extract_search_page(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        page_deadline: float | None = None,
    ) -> JobPageCapture:
        """Extract innerText from a job search page with soft rate-limit retry.

        Mirrors the noise-only detection and single-retry behavior of
        ``SectionCapture`` so that callers get a ``RATE_LIMITED_SECTION_TEXT``
        sentinel instead of silent empty results.

        One charge for both attempts, and it survives an attempt that raises:
        the scroll below books what it spent in a ``finally``, and the error
        path here still answers with a capture the caller charges its budget
        from. Losing that let a page whose extraction failed after a full
        twelve-second scroll cost the search nothing.

        ``page_deadline`` is where the search's budget ends, on the monotonic
        clock. Both attempts navigate and scroll against it, and the retry is
        not started when its backoff and a page will not fit before it.
        """
        charge = _ScrollCharge()
        try:
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return self._captured(result, charge.seconds)

            if page_deadline is not None:
                left = page_deadline - time.monotonic()
                if left < RATE_LIMIT_RETRY_DELAY + _RETRY_PAGE_ALLOWANCE:
                    logger.info(
                        "Not retrying search page %s: %.1fs of the budget left",
                        url,
                        left,
                    )
                    return self._captured(result, charge.seconds)

            logger.info(
                "Retrying search page %s after %.0fs backoff",
                url,
                RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline / 2,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text == RATE_LIMITED_SECTION_TEXT:
                logger.warning("Search page %s still rate-limited after retry", url)
            return self._captured(result, charge.seconds)

        except LinkedInOperationError:
            raise
        except Exception as e:
            logger.warning("Failed to extract search page %s: %s", url, e)
            return self._captured(
                ExtractedSection(
                    text="",
                    references=[],
                    error=build_issue_diagnostics(
                        e,
                        context="extract_search_page",
                        target_url=url,
                        section_name=section_name,
                    ),
                ),
                charge.seconds,
            )

    async def _extract_search_page_once(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        *,
        charge: _ScrollCharge,
        page_deadline: float | None = None,
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll sidebar, and extract innerText."""
        await self._navigator._navigate_to_page(url, deadline=page_deadline)
        await detect_rate_limit(self._session.page)
        # Above the selector wait and the modal close, so the window this
        # opens covers everything read from here on. Taken between them, a
        # reload committing during either one became the baseline itself, and
        # `main_found` then described a document that no longer existed.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)

        # `scroll_job_sidebar` swallows whatever its evaluate raises, so that a
        # rail replaced mid-flight does not cost the caller the page it is
        # about to read. A navigation destroys that context the same way and is
        # not the same thing: what waits to be read is then an authwall or a
        # checkpoint, and extracting it returns login text under
        # `search_results` with nothing beside it to say so.
        #
        # The route is compared as well as watched, because a redirect can
        # finish before the listener is registered. Host and path, and not the
        # whole URL, because LinkedIn appends `currentJobId` to the query of a
        # search page by itself. Measured across three live searches: the path
        # never moved, and neither did the query. The host has to come along,
        # or a redirect that keeps the path reads as no redirect at all.
        #
        # Against the URL that was asked for, and not the one the page held
        # after navigating, or a redirect finishing before the scroll becomes
        # its own baseline and passes. Outside the `main_found` branch for the
        # same reason: a landing page with no `<main>` extracts to nothing, and
        # an empty section is what an exhausted search looks like.
        before = route(url)
        moved = False
        navigated = False
        with self._navigator._watching_navigations() as hops:
            if main_found:
                scroll_started = time.monotonic()
                if page_deadline is not None:
                    # The scroll deadline was worked out before navigating.
                    # A slow `goto`, the `<main>` wait and the modal close all
                    # spend time it does not know about, and the scroll's own
                    # clock only starts when it is called.
                    scroll_deadline = min(
                        scroll_deadline, max(0.0, page_deadline - scroll_started)
                    )
                try:
                    moved = await scroll_job_sidebar(
                        self._session.page, deadline=scroll_deadline
                    )
                finally:
                    # Only what the scroll spent. Charging the whole page
                    # charged navigation and extraction to a budget that
                    # exists to bound scrolling, so five slow navigations that
                    # scrolled instantly still left the pages behind them with
                    # nothing. Accumulated, because a retry scrolls a second
                    # time.
                    charge.seconds += time.monotonic() - scroll_started
            # `hops` is read and not waited on, so a healthy page pays
            # nothing for it. It is what a scroll that finished cleanly leaves
            # behind when the document was replaced anyway: the scroll never
            # raised, so it reports no movement, and a reload moves no route,
            # so neither of the other two says anything happened.
            if moved or hops or before != route(self._session.page.url):
                navigated = await self._navigator._settle_navigation(hops, origin)

        after = route(self._session.page.url)
        if navigated or moved or not main_found or before != after:
            # Any of the three is enough, and none implies the others. A reload
            # keeps the address, so an account picker served in place of the
            # search page changes nothing the comparison below can see; a
            # redirect that completed during the navigation moves the route
            # without the scroll ever raising; and a barrier page carries no
            # `<main>`, so the scroll it would have raised from never ran.
            # That third one is the shape this check exists for and the one it
            # missed: an exhausted search renders no `<main>` either, which is
            # why the check has to decide it rather than the absence alone.
            await self._navigator._raise_if_auth_barrier(url)
        if before != after and not same_job_search(before, after):
            # An expired session lands here as often as a layout change does,
            # and the two need different answers. A plain error is caught by
            # the generic handler above and returned as a section diagnostic,
            # so the browser stays registered and no re-login is offered; the
            # caller then repeats the search against the same barrier.
            raise RuntimeError(
                f"Page navigated to {self._session.page.url} while scrolling {url}"
            )

        raw_result = await self._content._extract_root_content(["main"])

        # The watcher covers the scroll and nothing else, and the read sits
        # outside it at both ends: a reload committing after the listener came
        # off, or during the extraction itself, moves no route and raises
        # nothing. The document says what neither the address nor the listener
        # can, and it is asked about the text that was actually read.
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The search document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(url)

        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, sidebar scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Search page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(
                raw_result["references"], section_name, apply_cap=False
            ),
        )

    async def _get_total_search_pages(self) -> int | None:
        """Read total page count from LinkedIn's pagination state element.

        Parses the "Page X of Y" text from ``.jobs-search-pagination__page-state``.
        Returns ``None`` when the element is absent or unparseable.

        NOTE: This is a deliberate DOM exception. The element has ``display: none``
        (screen-reader only), so the text never appears in ``innerText``. A class-based
        selector is the only reliable way to read it. Gracefully returns ``None`` if
        LinkedIn renames the class — pagination just falls back to ``max_pages``.
        """
        text = await self._session.run_on_linkedin(
            """() => {
                const el = document.querySelector(
                    '.jobs-search-pagination__page-state'
                );
                return el ? el.textContent.trim() : null;
            }"""
        )
        if not text:
            return None
        match = re.search(r"of\s+(\d+)", text)
        return int(match.group(1)) if match else None

    async def _extract_saved_jobs_page(
        self,
        url: str,
        section_name: str,
    ) -> JobPageCapture:
        """Extract innerText from a saved-jobs page with soft rate-limit retry."""
        with self._navigator._watching_navigations() as hops:
            try:
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text != RATE_LIMITED_SECTION_TEXT:
                    return self._captured(result)

                logger.info(
                    "Retrying saved jobs page %s after %.0fs backoff",
                    url,
                    RATE_LIMIT_RETRY_DELAY,
                )
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text == RATE_LIMITED_SECTION_TEXT:
                    logger.warning(
                        "Saved jobs page %s still rate-limited after retry", url
                    )
                return self._captured(result)

            except LinkedInOperationError:
                raise
            except Exception as e:
                logger.warning("Failed to extract saved jobs page %s: %s", url, e)
                # A navigation destroys the scroll's execution context, and
                # what waits behind it is a checkpoint as often as a layout
                # change. Turning that into a section diagnostic hands the
                # caller an empty list, leaves the browser registered and
                # offers no relogin, so the next call meets the same barrier.
                #
                # Whether one happened is the listener's answer and not the
                # address's: this list reaches `/jobs-tracker/` by a redirect
                # LinkedIn makes on purpose, so comparing against the URL that
                # was asked for finds a difference on every ordinary failure
                # and waits out a chain that is not running.
                #
                # No document baseline, so every hop counts. One is taken
                # before the search scroll, where the page is already loaded
                # and the only navigation to expect is one going wrong. Here
                # the block opens before this page's own navigation, so a
                # reading from the top belongs to the document that was left
                # and would call every ordinary failure a replacement. `None`
                # says so, and settling costs a moment on a path that has
                # already failed.
                try:
                    await self._navigator._settle_navigation(hops, None)
                except Exception:
                    logger.debug(
                        "Could not settle the route after a saved-jobs failure",
                        exc_info=True,
                    )
                await self._navigator._raise_if_auth_barrier(
                    self._session.page.url, navigation_error=e
                )
                return self._captured(
                    ExtractedSection(
                        text="",
                        references=[],
                        error=build_issue_diagnostics(
                            e,
                            context="extract_saved_jobs_page",
                            target_url=url,
                            section_name=section_name,
                        ),
                    )
                )

    async def _extract_saved_jobs_page_once(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Single attempt: navigate, scroll list, and extract innerText."""
        await self._navigator._navigate_to_page(url)
        await detect_rate_limit(self._session.page)
        # Taken after this page's own navigation, so it belongs to the
        # document about to be read rather than to the one that was left.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)
        if main_found:
            await scroll_to_bottom(self._session.page, pause_time=0.5, max_scrolls=5)
        else:
            # A picker served in place of the list keeps the list's address
            # and its title, so the route guard below sees an allowed page and
            # the body fallback returns the picker under `saved_jobs`. Missing
            # `<main>` is what is left, and an emptied list has none either,
            # which is why the check decides it rather than the absence.
            await self._navigator._raise_if_auth_barrier(self._session.page.url)

        # A picker served by a reload keeps this page's address and this
        # page's title, so the route guard reads it as the list. Nothing else
        # notices either: the scroll pauses half a second between rounds, and
        # a document replaced in that gap leaves no evaluation to raise, so
        # the extraction succeeds against the replacement and returns it under
        # `saved_jobs` with the browser left on a barrier.
        #
        # Asked after the read rather than before it, or the gap between the
        # two is a window of its own and the text that came back is not the
        # text the check judged.
        raw_result = await self._content._extract_root_content(["main"])
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The saved-jobs document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(self._session.page.url)
        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Saved jobs page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name),
        )

    async def _get_total_list_pages(self) -> int | None:
        """Read last page number from artdeco pagination buttons.

        Parses numeric page labels from ``ul.artdeco-pagination__pages``.
        Returns ``None`` when pagination is absent or unparseable.

        NOTE: This is a deliberate DOM exception, mirroring
        ``_get_total_search_pages``. The my-items pager exposes no page count
        in ``innerText`` and no stable attribute to count, so a design-system
        class is the only reachable signal. The labels are numerals rather
        than words, so no locale table is needed. A renamed class, or a locale
        serving non-ASCII numerals that ``parseInt`` cannot read, both yield
        ``None`` — pagination then falls back to ``max_pages`` and the
        no-new-ids early stop.
        """
        value = await self._session.run_on_linkedin(
            """() => {
                const buttons = document.querySelectorAll(
                    'ul.artdeco-pagination__pages li button'
                );
                if (!buttons.length) return null;
                const nums = [...buttons]
                    .map((b) => parseInt(b.textContent.trim(), 10))
                    .filter((n) => !Number.isNaN(n));
                return nums.length ? Math.max(...nums) : null;
            }"""
        )
        return int(value) if value is not None else None

    async def read_posting_title(self, job_id: str) -> tuple[str, str]:
        """Open ``job_id``'s posting and read its card title and company.

        Both come from the document title, which LinkedIn sets once the
        posting renders. Raises when the navigation ends anywhere but that
        posting, after offering the page to the auth-barrier check, or when
        the title never takes that shape.
        """
        url = job_view_url(job_id, "/")
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        landed = JOB_PATH_RE.match(route(page.url)[1])
        if landed is None or landed.group(1) != job_id:
            await self._navigator._raise_if_auth_barrier(page.url)
            raise LinkedInOperationError(
                f"The navigation to job {job_id}'s posting ended on {page.url}"
            )
        deadline = self._session.monotonic() + _POSTING_TITLE_TIMEOUT
        while True:
            title = await page.title()
            parsed = parse_posting_title(title)
            if parsed is not None:
                return parsed
            if self._session.monotonic() >= deadline:
                raise LinkedInOperationError(
                    f"Job {job_id}'s posting never named its title and company: "
                    f"its page title read {title!r}"
                )
            await self._session.delay(_SEARCH_POLL)

    async def _open_job_search(self, url: str) -> None:
        """Navigate to a job search and wait for its result cards to render.

        Raises when the navigation ends anywhere but a job search, after
        offering the page to the auth-barrier check, or on a search that lost
        part of its query: a redirect to the bare search page keeps the route
        and drops the query whole, and its results are an answer to another
        question. The keywords and `geoId` are compared by value, since a
        search with other words or in another place is not the one asked
        for, and the other parameters by presence, as `search_jobs` does,
        since LinkedIn re-encodes some of them. `currentJobId`, which LinkedIn
        appends by itself, was never asked for and is not compared. A search with no results
        renders no card, so that wait ends in silence.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        if not same_job_search(route(url), route(page.url)):
            await self._navigator._raise_if_auth_barrier(page.url)
            raise LinkedInOperationError(
                f"The job search navigation ended on {page.url}"
            )
        asked = parse_qs(urlparse(url).query)
        landed = parse_qs(urlparse(page.url).query)
        lost = sorted(
            name
            for name in asked
            if not landed.get(name)
            or (name in _SEARCH_VALUE_PARAMS and landed[name][0] != asked[name][0])
        )
        if lost:
            raise LinkedInOperationError(
                f"The job search lost its {', '.join(lost)} on the way to {page.url}"
            )
        await self._session.dismiss_modal()
        try:
            await page.wait_for_selector(
                _SEARCH_CARD_SELECTOR,
                state="attached",
                timeout=int(_SEARCH_READY_TIMEOUT * 1000),
            )
        except PlaywrightTimeoutError:
            logger.debug("No result card rendered on %s", url)

    async def _find_search_card(self, job_id: str) -> dict[str, int]:
        """Count ``job_id``'s cards and buttons, and mark its dismiss button."""
        found = await self._session.page.evaluate(
            SEARCH_CARD_JS, {"jobId": job_id, "mark": _DISMISS_MARK}
        )
        if not isinstance(found, dict):
            raise LinkedInOperationError("The job search page could not be read")
        return found

    async def _locate_search_card(self, job_id: str) -> dict[str, int]:
        """Find ``job_id``'s card, scrolling the rail once when it is not there.

        A found card is polled until its buttons render, since an occluded
        card is empty until it nears the viewport. A scroll that
        raised leaves the rest of the rail unread, so it is an error rather
        than a job the search does not list.
        """
        found = await self._find_search_card(job_id)
        if not found["matches"]:
            if await self._session.scroll_job_sidebar(deadline=SCROLL_DEADLINE_MAX):
                raise LinkedInOperationError(
                    "The job search results could not be scrolled to the end"
                )
            found = await self._find_search_card(job_id)
        deadline = self._session.monotonic() + _SEARCH_CARD_RENDER_TIMEOUT
        while (
            found["matches"] == 1
            and not (found["dismiss"] or found["undo"])
            and self._session.monotonic() < deadline
        ):
            await self._session.delay(_SEARCH_POLL)
            found = await self._find_search_card(job_id)
        return found

    async def search_lists_job(self, url: str, job_id: str, min_cards: int = 1) -> bool:
        """Whether the job search at ``url`` shows a card for ``job_id``.

        Only a search that rendered other cards can say the job is not among
        them: a reload that rendered none, blank or still loading, raises
        instead, since reading it as "not listed" would confirm a dismissal
        nothing confirmed. So does one that rendered fewer than ``min_cards``,
        which the caller sets from what the same search showed before, since
        a partly rendered rail may stop short of the job's card.
        """
        await self._open_job_search(url)
        found = await self._locate_search_card(job_id)
        if found["matches"]:
            return True
        if not found["cards"]:
            raise LinkedInOperationError(
                f"The job search at {url} rendered no result cards, so whether "
                f"it still lists job {job_id} could not be read"
            )
        if found["cards"] < min_cards:
            raise LinkedInOperationError(
                f"The job search at {url} rendered {found['cards']} result cards "
                f"where {min_cards} were expected, so whether it still lists job "
                f"{job_id} could not be read"
            )
        return False

    async def dismiss_search_card(
        self, url: str, job_id: str
    ) -> tuple[SearchCardOutcome, int]:
        """Click the dismiss button on ``job_id``'s card in the search at ``url``.

        Success is the card turning to its undo state; the caller confirms it
        against a fresh load of the search, which should render as many cards
        as this one, returned alongside the outcome, less the job's own.
        """
        await self._open_job_search(url)
        found = await self._locate_search_card(job_id)
        cards = found["cards"]
        if not found["matches"]:
            return "absent", cards
        if found["dismiss"] != 1:
            if found["matches"] == 1 and found["undo"] and not found["dismiss"]:
                return "already_dismissed", cards
            return "no_button", cards

        page = self._session.page
        await page.locator(f"button[{_DISMISS_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _DISMISS_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_SEARCH_POLL)
            found = await self._find_search_card(job_id)
            if found["undo"] and not found["dismiss"]:
                return "dismissed", cards
        return "not_dismissed", cards
