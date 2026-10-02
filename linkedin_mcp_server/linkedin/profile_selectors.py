"""Every LinkedIn-specific fact the own-profile editor depends on, in one place.

LinkedIn changes its markup often, so this module is the only file that should
need editing when it does. Measured on 2 October 2026 against the en-US UI:

- Each edit form opens by URL as a native ``<dialog>``. The page also keeps
  hidden ``<dialog>`` elements (ad menus), so the edit dialog is the *visible*
  one that contains form controls.
- Field ids are generated (``_r_4_``) and change between renders: never used.
- Headline, About and position description are ProseMirror rich-text boxes:
  ``[role="textbox"][contenteditable="true"]``, the only one in their dialog.
  The description's ``aria-label`` states its limit ("maximum 2,000 characters").
- Title and company are inputs labelled through ``aria-labelledby``.
- Save is a plain ``<button>`` whose only identity is its text.
- Positions offer a "notify your network" ``role="switch"``.
- A skill's edit dialog names the skill only in its heading ("Edit React.js").
- Skill suggestions are ``role="option"`` elements.

Locators prefer, in order: structure and stable attributes (role, type,
contenteditable), then a visible label from the per-locale ``LABELS`` table.
Text is used only where nothing else identifies a control, so a non-English
account fails with SELECTOR_NOT_FOUND and diagnostics instead of clicking
something else. Every field locator is relative to the edit dialog.
"""

from __future__ import annotations

from dataclasses import dataclass

import re

LINKEDIN = "https://www.linkedin.com"
OWN_PROFILE_URL = f"{LINKEDIN}/in/me/"
VANITY_FROM_URL = re.compile(r"linkedin\.com/in/([^/?#]+)/?")

# The edit dialog: visible, and holding at least one form control.
DIALOG = 'dialog:visible, [role="dialog"]:visible'
DIALOG_HAS = 'input, select, textarea, [role="textbox"]'


def profile_url(vanity: str) -> str:
    return f"{LINKEDIN}/in/{vanity}/"


def intro_form_url(vanity: str) -> str:
    return f"{profile_url(vanity)}edit/intro/"


def about_form_urls(vanity: str) -> tuple[str, ...]:
    """Tried in order; the first that opens a dialog with an About field wins."""
    base = profile_url(vanity)
    return (f"{base}edit/forms/summary/new/", f"{base}edit/about/")


def experience_list_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/experience/"


def experience_form_url(vanity: str, position_id: str) -> str:
    return f"{profile_url(vanity)}details/experience/edit/forms/{position_id}/"


def skills_list_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/skills/"


def skill_form_url(vanity: str, skill_id: str) -> str:
    return f"{profile_url(vanity)}details/skills/edit/forms/{skill_id}/"


def new_skill_form_url(vanity: str) -> str:
    return f"{profile_url(vanity)}skills/edit/forms/new/"


# Edit links on the details pages carry LinkedIn's own entity ids. These are the
# stable identities of positions and skills; list order never is.
EXPERIENCE_EDIT_HREF = re.compile(
    r"/(?:details/experience/edit/forms|edit/forms/position)/(\d+)"
)
SKILL_EDIT_HREF = re.compile(r"/details/skills/edit/forms/(\d+)")

# A maximum stated in a field's accessible label ("maximum 2,000 characters").
# Only the digits are read, so it does not depend on the language.
STATED_MAX = re.compile(r"(\d{1,2}[,.  ]?\d{3}|\d{2,4})")

RICH_TEXT = '[role="textbox"][contenteditable="true"]'


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """How to find one form control inside the edit dialog."""

    name: str
    css: tuple[str, ...] = ()
    label_keys: tuple[str, ...] = ()


HEADLINE = FieldSpec("headline", (RICH_TEXT,))
ABOUT = FieldSpec("about", (RICH_TEXT,))
EXPERIENCE_DESCRIPTION = FieldSpec("experience_description", (RICH_TEXT,))
EXPERIENCE_TITLE = FieldSpec("experience_title", label_keys=("title",))
EXPERIENCE_COMPANY = FieldSpec("experience_company", label_keys=("company",))
LOCATION = FieldSpec("location", label_keys=("location",))
# The add-skill dialog's only text input; every other input there is a checkbox.
SKILL_INPUT = FieldSpec(
    "skill",
    ('input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"])',),
)
NOTIFY_SWITCH = 'input[role="switch"]'
HEADINGS = "h1, h2, h3"

SAVE_BUTTON = ('button[type="submit"]',)
TYPEAHEAD_OPTION = '[role="option"]'
# Presence of either means the form refused the save; text is only reported.
FORM_ERROR = ('[role="alert"]', '[aria-invalid="true"]')

# Visible-text fallbacks, per locale. Extend per locale rather than loosening a
# match: an inexact label is how the wrong control gets clicked. A tuple lists
# the exact variants LinkedIn has been seen to use.
LABELS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "title": ("Title*", "Title"),
        "company": ("Company or organization*", "Company or organization"),
        "location": ("Country/Region*", "Country/Region"),
        "save": ("Save",),
        "delete_skill": ("Delete skill",),
        "confirm_delete": ("Delete",),
    },
}
DEFAULT_LOCALE = "en"


# Runs in the page: list the controls in the visible dialog, for diagnostics
# when a field cannot be found. Reports identity, not values.
DESCRIBE_DIALOG_JS = r"""
() => {
  const d = [...document.querySelectorAll('dialog, [role="dialog"]')]
    .find((x) => (x.offsetWidth || x.offsetHeight) && x.querySelector('input, select, textarea, [role="textbox"]'));
  if (!d) return {dialog: false, controls: []};
  const text = (el) => el ? el.innerText.trim().replace(/\s+/g, ' ').slice(0, 60) : null;
  const controls = [...d.querySelectorAll('input, textarea, select, [contenteditable="true"], button')].slice(0, 60);
  return {
    dialog: true,
    heading: text(d.querySelector('h1, h2, h3')),
    controls: controls.map((el) => ({
      tag: el.tagName.toLowerCase(), type: el.getAttribute('type'), role: el.getAttribute('role'),
      ariaLabel: el.getAttribute('aria-label'), placeholder: el.getAttribute('placeholder'),
      labelledBy: (el.getAttribute('aria-labelledby') || '').split(' ')
        .map((i) => text(document.getElementById(i))).filter(Boolean).join(' | ') || null,
      text: el.tagName === 'BUTTON' ? text(el) : null,
    })),
  };
}
"""

# Runs in the page: one record per entity edit link, with the text lines of the
# item it belongs to. The item is the <li> around the link or, where LinkedIn
# renders no list, the largest ancestor holding no other entity's edit link.
# A role grouped under its company also gets the group's lines.
LIST_ITEMS_JS = r"""
(pattern) => {
  const re = new RegExp(pattern);
  const links = [...document.querySelectorAll('main a[href]')].filter((a) => re.test(a.getAttribute('href')));
  const idOf = (a) => a.getAttribute('href').match(re)[1];
  const itemOf = (a) => {
    let el = a, best = a;
    while (el.parentElement && el.parentElement.tagName !== 'MAIN') {
      const ids = new Set([...el.parentElement.querySelectorAll('a[href]')]
        .filter((x) => re.test(x.getAttribute('href'))).map(idOf));
      if (ids.size > 1) break;
      el = el.parentElement;
      best = el;
    }
    return best;
  };
  // An item's own icon controls (the pencil link, which carries an aria-label
  // and hidden text, and buttons) are not its content; their text is dropped by
  // identity, not by matching a word. The item's content is itself a link to
  // the same edit form, without an aria-label, and is kept.
  const lines = (el) => {
    if (!el) return [];
    const own = new Set([...el.querySelectorAll('a[aria-label][href*="/edit/"], button')]
      .flatMap((c) => c.innerText.split('\n').map((s) => s.trim())).filter(Boolean));
    return el.innerText.split('\n').map((s) => s.trim()).filter(Boolean)
      .filter((s) => !own.has(s))
      .filter((s, i, arr) => i === 0 || s !== arr[i - 1]);
  };
  const seen = new Set();
  const out = [];
  for (const a of links) {
    const id = idOf(a);
    if (seen.has(id)) continue;
    seen.add(id);
    const li = a.closest('li');
    const item = li || itemOf(a);
    const group = li && li.parentElement ? li.parentElement.closest('li') : null;
    out.push({id, href: a.getAttribute('href'), lines: lines(item), groupLines: lines(group)});
  }
  return out;
}
"""

# The skills page shows a bounded "All" view plus category views (Industry
# Knowledge, Tools & Technologies, ...) that together hold every skill. The
# category buttons are the siblings of the one marked aria-current, found by
# structure rather than by their (localized) names.
SKILL_FILTER_BUTTONS = "main ul:has(> li > button[aria-current]) > li > button"
