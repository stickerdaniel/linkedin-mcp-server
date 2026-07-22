"""Parse the fully-scrolled LinkedIn skills section into structured records.

The skills detail page (``/details/skills/``) renders each skill as a block::

    <Skill Name>
    [<N> experiences at <company> ...]      (optional)
    [Endorsed by <...>]                     (zero or more lines)
    [Passed LinkedIn Skill Assessment]      (optional)
    [<N> endorsements | 99+ endorsements]   (optional)

Some skills additionally render an inline "associated experience" preview (a job
title such as "Director, Solutions Architecture at Acme") plus a "Show all N
details" expander. Those lines are structurally indistinguishable from a skill
name in plain text, so pure segmentation mis-reads them as skills.

To avoid that, the parser is *keyed* on an authoritative list of skill names
supplied by the caller. The extractor reads those names from the per-skill
aria-labels — ``Edit <name> skill`` on the owner's own profile, or
``Endorse <name>`` / ``Endorsed <name>`` on other people's profiles. Only a line
that exactly matches a known name starts a new record; every following line up to
the next known name is that skill's metadata. This makes the associated-experience
and expander lines fall through harmlessly (they are not in the name set).

When no authoritative names are available (rare — e.g. a profile that exposes
neither edit nor endorse affordances) the parser falls back to a heuristic
segmentation that treats any non-metadata line as a skill name.

LOCALE NOTE: the metadata keywords and aria verbs handled here are English-only.
Per this repo's locale-independence rule that is a documented limitation; extend
the token tables below to support other UI languages.
"""

from __future__ import annotations

import re
from typing import Any

# --- Locale-dependent tokens (English UI). Documented limitation; see module doc. ---

# Filter tabs at the top of the skills panel, plus category headers.
_TAB_HEADERS: frozenset[str] = frozenset(
    {
        "Skills",
        "All",
        "Industry Knowledge",
        "Tools & Technologies",
        "Interpersonal Skills",
        "Other Skills",
        "Languages",
    }
)

# aria-label verb prefixes that carry an authoritative skill name.
# "Edit <name> skill" (own profile), "Endorse/Endorsed <name>" (others).
_ARIA_EDIT_RE = re.compile(r"^Edit (?P<name>.+) skill$")
_ARIA_ENDORSE_RE = re.compile(r"^Endorsed? (?P<name>.+)$")

# Metadata line patterns (used by the heuristic fallback and count extraction).
_RE_ENDORSEMENT_COUNT = re.compile(r"^(?P<n>\d+\+?) endorsements?$", re.IGNORECASE)
_RE_EXPERIENCES = re.compile(r"^\d+ experiences? at ", re.IGNORECASE)
_RE_SHOW_ALL_DETAILS = re.compile(r"^Show all \d+ details?$", re.IGNORECASE)
_META_EXACT: frozenset[str] = frozenset({"Passed LinkedIn Skill Assessment"})
_META_PREFIXES: tuple[str, ...] = ("Endorsed by ",)


def skill_names_from_aria_labels(aria_labels: list[str]) -> list[str]:
    """Extract authoritative, de-duplicated skill names from per-skill aria-labels.

    Order is preserved (LinkedIn renders skills most-endorsed / pinned first).
    """
    names: list[str] = []
    seen: set[str] = set()
    for label in aria_labels:
        label = (label or "").strip()
        m = _ARIA_EDIT_RE.match(label) or _ARIA_ENDORSE_RE.match(label)
        if not m:
            continue
        name = m.group("name").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _parse_count(line: str) -> tuple[int, str] | None:
    """Return (numeric_count, display) for an 'N endorsements' line, else None.

    '99+ endorsements' -> (99, '99+'); '32 endorsements' -> (32, '32').
    """
    m = _RE_ENDORSEMENT_COUNT.match(line)
    if not m:
        return None
    raw = m.group("n")
    numeric = int(raw.rstrip("+"))
    return numeric, raw


def _new_record(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "endorsements": 0,
        "endorsements_display": "0",
        "endorsers": [],
    }


def _apply_meta(record: dict[str, Any], line: str) -> None:
    """Attach a single metadata line to the current skill record."""
    count = _parse_count(line)
    if count is not None:
        record["endorsements"], record["endorsements_display"] = count
    elif line.startswith(_META_PREFIXES):
        record["endorsers"].append(line)


def _is_metadata(line: str) -> bool:
    """Heuristic: does this line look like skill metadata rather than a name?"""
    if line in _META_EXACT:
        return True
    if line.startswith(_META_PREFIXES):
        return True
    if _RE_ENDORSEMENT_COUNT.match(line):
        return True
    if _RE_EXPERIENCES.match(line):
        return True
    if _RE_SHOW_ALL_DETAILS.match(line):
        return True
    return False


def parse_skills(text: str, names: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse the skills section innerText into ``[{name, endorsements, ...}]``.

    Args:
        text: The noise-truncated innerText of the fully-scrolled skills panel.
        names: Authoritative skill names (from aria-labels). When provided, the
            parser keys on them for robust segmentation. When ``None``/empty it
            falls back to heuristic segmentation.

    Returns:
        One record per skill: ``{name, endorsements (int), endorsements_display
        (str, e.g. "99+"), endorsers (list[str])}``, in page order.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if names:
        return _parse_keyed(lines, names)
    return _parse_heuristic(lines)


def _parse_keyed(lines: list[str], names: list[str]) -> list[dict[str, Any]]:
    nameset = set(names)
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line in nameset and line not in records:
            record = _new_record(line)
            records[line] = record
            order.append(line)
            j = i + 1
            while j < len(lines) and lines[j] not in nameset:
                _apply_meta(record, lines[j])
                j += 1
            i = j
        else:
            i += 1
    return [records[n] for n in order]


def _parse_heuristic(lines: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        if line in _TAB_HEADERS or _RE_SHOW_ALL_DETAILS.match(line):
            continue
        if _is_metadata(line):
            if current is not None:
                _apply_meta(current, line)
            continue
        current = _new_record(line)
        records.append(current)
    return records
