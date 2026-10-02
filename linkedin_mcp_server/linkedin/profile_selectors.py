"""Every LinkedIn-specific fact the own-profile editor depends on, in one place.

LinkedIn changes its markup often, so this module is the only file that should
need editing when it does. The editor reaches each form by URL and finds fields
by, in order: a stable attribute (an ``id`` fragment, ``role``, ``type``) —
never a layout class — then a structural fact (the dialog's only textarea),
then, last, a visible label from the per-locale table below. Text is used only
where nothing else identifies a control, and only through ``LABELS``, so a
non-English account fails with SELECTOR_NOT_FOUND and diagnostics instead of
clicking something else.

Every locator here is scoped to the open edit dialog.
"""

from __future__ import annotations

from dataclasses import dataclass

import re

LINKEDIN = "https://www.linkedin.com"
OWN_PROFILE_URL = f"{LINKEDIN}/in/me/"
VANITY_FROM_URL = re.compile(r"linkedin\.com/in/([^/?#]+)/?")

DIALOG = '[role="dialog"]'


def profile_url(vanity: str) -> str:
    return f"{LINKEDIN}/in/{vanity}/"


def intro_form_url(vanity: str) -> str:
    return f"{profile_url(vanity)}edit/intro/"


def about_form_urls(vanity: str) -> tuple[str, ...]:
    """Tried in order; the first that opens a dialog with an About field wins."""
    base = profile_url(vanity)
    return (f"{base}edit/about/", f"{base}edit/forms/summary/new/")


def experience_list_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/experience/"


def experience_form_url(vanity: str, position_id: str) -> str:
    return f"{profile_url(vanity)}details/experience/edit/forms/{position_id}/"


def skills_list_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/skills/"


def skill_form_url(vanity: str, skill_id: str) -> str:
    return f"{profile_url(vanity)}details/skills/edit/forms/{skill_id}/"


def new_skill_form_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/skills/edit/forms/new/"


# Edit links on the details pages carry LinkedIn's own entity ids. These are the
# stable identities of positions and skills; list order never is.
EXPERIENCE_EDIT_HREF = re.compile(
    r"/(?:details/experience/edit/forms|edit/forms/position)/(\d+)"
)
SKILL_EDIT_HREF = re.compile(r"/details/skills/edit/forms/(\d+)")

# A character counter such as "57/220" next to a field. Digits only, so it is
# locale-independent; separators are stripped before parsing.
COUNTER = re.compile(r"(\d[\d,.   ]*)\s*/\s*(\d[\d,.   ]*)")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """How to find one form control inside the edit dialog."""

    name: str
    css: tuple[str, ...]
    label_key: str | None = None
    only_textarea: bool = False


HEADLINE = FieldSpec(
    "headline",
    (
        f'{DIALOG} textarea[id$="-headline"]',
        f'{DIALOG} input[id$="-headline"]',
        f'{DIALOG} textarea[id*="headline" i]',
        f'{DIALOG} input[id*="headline" i]',
    ),
    label_key="headline",
)
LOCATION = FieldSpec(
    "location",
    (
        f'{DIALOG} input[id*="geoLocation" i]',
        f'{DIALOG} input[id*="location" i]',
        f'{DIALOG} input[id*="city" i]',
    ),
    label_key="location",
)
ABOUT = FieldSpec(
    "about",
    (f'{DIALOG} textarea[id*="summary" i]', f'{DIALOG} textarea[id*="about" i]'),
    label_key="about",
    only_textarea=True,
)
EXPERIENCE_TITLE = FieldSpec(
    "experience_title",
    (
        f'{DIALOG} input[id$="-title"]',
        f'{DIALOG} input[id*="title" i]:not([id*="subtitle" i])',
    ),
    label_key="title",
)
EXPERIENCE_DESCRIPTION = FieldSpec(
    "experience_description",
    (f'{DIALOG} textarea[id*="description" i]',),
    label_key="description",
)
EXPERIENCE_COMPANY = FieldSpec(
    "experience_company",
    (f'{DIALOG} input[id*="company" i]', f'{DIALOG} input[id*="organization" i]'),
    label_key="company",
)
SKILL_INPUT = FieldSpec(
    "skill",
    (f'{DIALOG} input[role="combobox"]', f'{DIALOG} input[id*="skill" i]'),
    label_key="skill",
)

SAVE_BUTTON = (f'{DIALOG} button[type="submit"]',)
TYPEAHEAD_OPTION = '[role="listbox"] [role="option"]'
# Presence of either means the form refused the save; text is only reported.
FORM_ERROR = (f'{DIALOG} [role="alert"]', f'{DIALOG} [aria-invalid="true"]')

# Visible-text fallbacks, per locale. Extend per locale rather than loosening a
# match: an inexact label is how the wrong control gets clicked.
LABELS: dict[str, dict[str, str]] = {
    "en": {
        "headline": "Headline",
        "location": "City",
        "about": "About",
        "title": "Title",
        "description": "Description",
        "company": "Company or organization",
        "skill": "Skill",
        "save": "Save",
        "delete_skill": "Delete skill",
        "confirm_delete": "Delete",
    },
}
DEFAULT_LOCALE = "en"


# Runs in the page: list the controls in the open dialog, for diagnostics when a
# field cannot be found. Reports identity, not values.
DESCRIBE_DIALOG_JS = r"""
() => {
  const d = document.querySelector('[role="dialog"]');
  if (!d) return {dialog: false, controls: []};
  const controls = [...d.querySelectorAll('input, textarea, select, [contenteditable="true"], button')].slice(0, 60);
  return {
    dialog: true,
    controls: controls.map((el) => {
      const lab = el.id ? d.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
      return {
        tag: el.tagName.toLowerCase(), id: el.id || null, type: el.getAttribute('type'),
        role: el.getAttribute('role'), name: el.getAttribute('name'),
        ariaLabel: el.getAttribute('aria-label'), label: lab ? lab.innerText.trim().slice(0, 60) : null,
        maxlength: el.getAttribute('maxlength'),
      };
    }),
  };
}
"""

# Runs in the page: every element whose href matches, with the text of its list
# item and of the enclosing group item (a company grouping several roles).
LIST_ITEMS_JS = r"""
(pattern) => {
  const re = new RegExp(pattern);
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll('main a[href]')) {
    const m = a.getAttribute('href').match(re);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const li = a.closest('li');
    const group = li && li.parentElement ? li.parentElement.closest('li') : null;
    // An item's own controls (the edit pencil's hidden label, buttons) are not
    // its content; their text is dropped by identity, not by matching a word.
    const lines = (el) => {
      if (!el) return [];
      const own = new Set([...el.querySelectorAll('a[href*="/edit/"], button')]
        .flatMap((c) => c.innerText.split('\n').map((s) => s.trim())).filter(Boolean));
      return el.innerText.split('\n').map((s) => s.trim()).filter(Boolean)
        .filter((s) => !own.has(s))
        .filter((s, i, arr) => i === 0 || s !== arr[i - 1]);
    };
    out.push({id: m[1], href: a.getAttribute('href'), lines: lines(li), groupLines: lines(group)});
  }
  return out;
}
"""
