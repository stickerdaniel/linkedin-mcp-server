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


def entry_form_urls(vanity: str, kind: str, entry_id: str) -> tuple[str, ...]:
    """An existing entry's edit form, tried in order (position or education)."""
    base = profile_url(vanity)
    if kind == "position":
        return (
            experience_form_url(vanity, entry_id),
            f"{base}edit/forms/position/{entry_id}/",
        )
    return (
        f"{base}details/education/edit/forms/{entry_id}/",
        f"{base}edit/forms/education/{entry_id}/",
    )


def skills_list_url(vanity: str) -> str:
    return f"{profile_url(vanity)}details/skills/"


def skill_form_url(vanity: str, skill_id: str) -> str:
    return f"{profile_url(vanity)}details/skills/edit/forms/{skill_id}/"


def new_skill_form_url(vanity: str) -> str:
    return f"{profile_url(vanity)}skills/edit/forms/new/"


# Add-entry forms, tried in order; the first that opens a dialog with form
# controls wins. Candidates, not certainties, until calibrated live.
NEW_ENTRY_FORM_PATHS: dict[str, tuple[str, ...]] = {
    "position": ("edit/forms/position/new/", "details/experience/edit/forms/new/"),
    "education": ("edit/forms/education/new/", "details/education/edit/forms/new/"),
    "publication": (
        "edit/forms/publication/new/",
        "details/publications/edit/forms/new/",
    ),
}


def new_entry_form_urls(vanity: str, kind: str) -> tuple[str, ...]:
    base = profile_url(vanity)
    return tuple(f"{base}{p}" for p in NEW_ENTRY_FORM_PATHS[kind])


def entry_list_url(vanity: str, kind: str) -> str:
    section = {
        "position": "experience",
        "education": "education",
        "publication": "publications",
    }[kind]
    return f"{profile_url(vanity)}details/{section}/"


# Edit links on the details pages carry LinkedIn's own entity ids. These are the
# stable identities of positions and skills; list order never is.
EXPERIENCE_EDIT_HREF = re.compile(
    r"/(?:details/experience/edit/forms|edit/forms/position)/(\d+)"
)
SKILL_EDIT_HREF = re.compile(r"/details/skills/edit/forms/(\d+)")

# A maximum stated in a field's accessible label ("maximum 2,000 characters").
# Only the digits are read, so it does not depend on the language.
# Thousands separators include the no-break spaces some locales use, written as
# escapes so the source holds no invisible characters.
STATED_MAX = re.compile(r"(\d{1,2}[,.\u00a0\u202f]?\d{3}|\d{2,4})")

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
# The visible control drawn over it (measured 12 October 2026).
NOTIFY_SWITCH_HANDLE = 'div[role="switch"]'
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
        "title": ("Title*", "Title", "Job title*", "Job title"),
        "company": (
            "Company or organization*",
            "Company or organization",
            "Organization",
        ),
        "location": ("Country/Region*", "Country/Region"),
        "save": ("Save",),
        "delete_skill": ("Delete skill",),
        "confirm_delete": ("Delete",),
        # Add-entry forms, measured 12 October 2026 (en-US).
        "position.title": ("Job title*",),
        "position.company": (
            "Organization",
            "Company or organization*",
            "Company or organization",
        ),
        "position.location": ("Location",),
        "position.locationType": ("Location type",),
        "position.employmentType": ("Employment type",),
        "education.school": ("School*",),
        "education.degree": ("Degree",),
        "education.fieldOfStudy": ("Field of study",),
        "education.grade": ("Grade",),
        "education.activities": ("Activities and societies",),
        "education.description": ("Description",),
        "publication.title": ("Title*",),
        "publication.publisher": ("Publication/Publisher",),
        "publication.date": ("Publication date",),
        "publication.url": ("Publication URL",),
        "publication.description": ("Description",),
    },
    # Measured 12 October 2026 (ru UI).
    "ru": {
        "title": ("Название должности*", "Название должности"),
        "company": ("Организация",),
        "location": ("Страна/регион",),
        "save": ("Сохранить",),
        "position.title": ("Название должности*",),
        "position.company": ("Организация",),
        "position.location": ("Регион",),
        "position.locationType": ("Тип места работы",),
        "position.employmentType": ("Тип занятости",),
        "education.school": ("Уч. заведение*",),
        "education.degree": ("Степень",),
        "education.fieldOfStudy": ("Специализация",),
        "education.grade": ("Средний балл",),
        "education.activities": ("Деятельность и сообщества",),
        "education.description": ("Описание",),
        "publication.title": ("Название*",),
        "publication.publisher": ("Издание/издательство",),
        "publication.date": ("Дата размещения публикации",),
        "publication.url": ("URL-адрес публикации",),
        "publication.description": ("Описание",),
    },
}
DEFAULT_LOCALE = "en"

# The intro form's first and last name inputs carry no label, aria-label or
# name; they are the dialog's first two required text inputs, in that order.
NAME_INPUTS = 'input[type="text"][required]'

# The position form's "I am currently working in this role" box: the dialog's
# only checkbox that is not the notify switch.
CURRENT_ROLE_CHECKBOX = 'input[type="checkbox"]:not([role="switch"])'

# Entry edit links on each section's details page; the number is LinkedIn's
# entity id.
ENTRY_EDIT_HREF: dict[str, re.Pattern[str]] = {
    "position": EXPERIENCE_EDIT_HREF,
    "education": re.compile(
        r"/(?:details/education/edit/forms|edit/forms/education)/(\d+)"
    ),
    "publication": re.compile(
        r"/(?:details/publications/edit/forms|edit/forms/publication)/(\d+)"
    ),
}

# After a successful save LinkedIn may open a follow-up prompt ("share this
# new position") at this URL instead of closing the form (12 October 2026).
FOLLOW_UP_AFTER_SAVE = re.compile(r"/edit/forms/next-action/")

CONNECTIONS_URL = f"{LINKEDIN}/mynetwork/invite-connect/connections/"

# Runs in the page (calibration): the connection rows' structure: profile
# links, and the buttons near each with their aria attributes and text.
DESCRIBE_CONNECTIONS_JS = r"""
() => {
  const main = document.querySelector('main') || document.body;
  const links = [...main.querySelectorAll('a[href*="/in/"]')];
  const seen = new Set(), rows = [];
  for (const a of links) {
    const slug = (a.getAttribute('href').match(/\/in\/([^/?#]+)/) || [])[1];
    if (!slug || seen.has(slug)) continue;
    seen.add(slug);
    let row = a;
    for (let i = 0; i < 8 && row.parentElement; i++) {
      row = row.parentElement;
      if (row.querySelector('button')) break;
    }
    rows.push({
      slug,
      text: a.innerText.trim().replace(/\s+/g, ' ').slice(0, 80),
      buttons: [...row.querySelectorAll('button')].slice(0, 4).map((b) => ({
        aria: b.getAttribute('aria-label'), expanded: b.getAttribute('aria-expanded'),
        text: b.innerText.trim().slice(0, 30),
      })),
    });
  }
  const count = (main.innerText.match(/(\d[\d,]*)\s+connections?/i) || [])[1] || null;
  return {url: location.href, lang: document.documentElement.lang, count, rows: rows.slice(0, 12), total: rows.length};
}
"""

# Runs in the page: every dropdown in the visible dialog, with the first few
# option values and the option count, for diagnosing date lookups.
DESCRIBE_SELECTS_JS = r"""
() => {
  const d = [...document.querySelectorAll('dialog, [role="dialog"]')]
    .find((x) => (x.offsetWidth || x.offsetHeight) && x.querySelector('select'));
  if (!d) return [];
  return [...d.querySelectorAll('select')].map((s) => ({
    visible: !!(s.offsetWidth || s.offsetHeight),
    disabled: s.disabled,
    values: [...s.options].slice(0, 4).map((o) => o.value),
    count: s.options.length,
  }));
}
"""

# Runs in the page: the visible dialog's month and year dropdowns, found by
# their option values (1..12, four-digit years), not by their localized labels.
# Returns, for each, its index among the dialog's selects, in document order:
# the first month/year pair is the start, the second the end.
DATE_SELECTS_JS = r"""
() => {
  const d = [...document.querySelectorAll('dialog, [role="dialog"]')]
    .find((x) => (x.offsetWidth || x.offsetHeight) && x.querySelector('select'));
  if (!d) return {months: [], years: []};
  const selects = [...d.querySelectorAll('select')];
  const values = (s) => [...s.options].map((o) => o.value).filter(Boolean);
  const months = [], years = [];
  selects.forEach((s, i) => {
    const v = values(s);
    if (v.length === 12 && v.every((x, j) => x === String(j + 1))) months.push(i);
    // An end-year list holds only the years from the chosen start onward.
    else if (v.length > 0 && v.every((x) => /^\d{4}$/.test(x))) years.push(i);
  });
  return {months, years};
}
"""


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

# Runs in the page: a fuller description of the visible dialog's controls, for
# calibrating a form (describe_profile_form). Reports each control's identity,
# label, limits and, for selects and checkboxes, their state; text field
# values are reported only as lengths.
DESCRIBE_FORM_JS = r"""
() => {
  const d = [...document.querySelectorAll('dialog, [role="dialog"]')]
    .find((x) => (x.offsetWidth || x.offsetHeight) && x.querySelector('input, select, textarea, [role="textbox"]'));
  if (!d) return {dialog: false, url: location.href, lang: document.documentElement.lang};
  const text = (el) => el ? el.innerText.trim().replace(/\s+/g, ' ').slice(0, 80) : null;
  const label = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
    const byIds = ids.map((i) => text(document.getElementById(i))).filter(Boolean).join(' | ');
    if (byIds) return byIds;
    if (el.id) {
      const l = d.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) return text(l);
    }
    const wrap = el.closest('label');
    return wrap ? text(wrap) : null;
  };
  const controls = [...d.querySelectorAll('input, textarea, select, [contenteditable="true"], button, [role="switch"], [role="combobox"]')]
    .filter((e) => e.getAttribute('type') !== 'hidden').slice(0, 100);
  return {
    dialog: true,
    url: location.href,
    lang: document.documentElement.lang,
    heading: text(d.querySelector('h1, h2, h3')),
    controls: controls.map((e) => {
      const tag = e.tagName.toLowerCase();
      const plain = tag === 'input' || tag === 'textarea';
      return {
        tag, type: e.getAttribute('type'), role: e.getAttribute('role'),
        name: e.getAttribute('name'), autocompleteAttr: e.getAttribute('autocomplete'),
        describedBy: (e.getAttribute('aria-describedby') || '').split(' ').filter(Boolean)
          .map((i) => text(document.getElementById(i))).filter(Boolean).join(' | ') || null,
        nearbyLabel: (() => {
          let p = e.parentElement;
          for (let i = 0; p && p !== d && i < 4; i++, p = p.parentElement) {
            const l = p.querySelector('label');
            if (l) return text(l);
          }
          return null;
        })(),
        label: label(e), ariaLabel: e.getAttribute('aria-label'),
        placeholder: e.getAttribute('placeholder'),
        required: e.required || e.getAttribute('aria-required') === 'true',
        maxlength: e.getAttribute('maxlength'),
        autocomplete: e.getAttribute('aria-autocomplete'),
        checked: (e.type === 'checkbox' || e.getAttribute('role') === 'switch') ? !!e.checked : undefined,
        valueLength: plain && e.type !== 'checkbox' ? e.value.length : undefined,
        selected: tag === 'select' ? (e.options[e.selectedIndex] || {}).text : undefined,
        options: tag === 'select' ? [...e.options].slice(0, 5).map((o) => o.value + '=' + o.text.trim()).concat(['count=' + e.options.length]) : undefined,
        text: tag === 'button' ? text(e) : undefined,
        // Checkboxes: the markup around them (no values), to find the visible control.
        around: e.type === 'checkbox' ? (e.parentElement && e.parentElement.parentElement
          ? e.parentElement.parentElement.outerHTML.replace(/\s+/g, ' ').slice(0, 900) : null) : undefined,
        labelsCount: e.type === 'checkbox' ? (e.labels ? e.labels.length : 0) : undefined,
        visible: e.type === 'checkbox' ? !!(e.offsetWidth || e.offsetHeight) : undefined,
      };
    }),
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
      // A lone entry would otherwise climb to its whole section and take the
      // section heading ("Skills", "Experience") as its first line: stop
      // before an ancestor holding a heading the entry itself does not.
      const heads = [...el.parentElement.querySelectorAll('h1, h2')];
      if (heads.some((h) => !el.contains(h))) break;
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

# Runs in the page: bring the last entity edit link into view, so the list's own
# scroll container (not the window) moves and loads its next batch.
SCROLL_LAST_ITEM_JS = r"""
(pattern) => {
  const re = new RegExp(pattern);
  const links = [...document.querySelectorAll('main a[href]')].filter((a) => re.test(a.getAttribute('href')));
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  return links.length;
}
"""
