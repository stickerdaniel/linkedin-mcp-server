"""Structured rows from the innerText of LinkedIn people and company search.

Both parsers work on text alone -- the ``sections.search_results`` blob a
search tool already returns -- plus the ``references`` list the same scrape
produced, which supplies the profile / company URL per card. No DOM, no
browser. The shapes below are claims about LinkedIn, pinned by tests over
text captured live (see tests/test_search_parse.py):

* A **people card** opens with the name and a degree marker, ``Name • 2nd``,
  on one line or split over two, and continues headline / location /
  optional snippet / optional mutual-connections line / optional
  ``· NK followers``. The marker is what bounds the card: a bullet followed
  by a token that starts with a digit 1-3 (``2nd``, ``3rd+``, ``2.``, ``2e``)
  and nothing else on the line. That is a structural signal, not a word, so
  it survives a UI language change; the ``• You`` self card does not and is
  absorbed into the card above it.

* A **company card** runs name / industry / location / follow button /
  tagline and ends with the followers line, ``... · NK followers``: the only
  block that carries a ``·`` and whose last ``·``-segment is a count and one
  word. Cards are read *backwards* from that line, so the promoted event and
  "Contact us" blocks LinkedIn drops *between* cards never shift a card's
  fields. A card may carry no tagline, which drops it to four lines; the
  two shapes are told apart by the ``company`` references: the slot whose
  text is a reference's text is the name slot. A card no reference names
  (the builder drops anchors past its label limit) is read as five lines.

Known locale limit: a count is only recognised with its magnitude suffix
attached (``2K``, ``2,5K``). A locale that spaces the suffix (French renders
``2 k abonnés``) is not a count to ``_COUNT``, so on such a page no followers
line is found and every company card is dropped; people rows survive with
``followers`` unset.

The page's chrome (Sales Navigator upsell, "Are these results helpful?",
pagination, the "About N results" header) is skipped by position -- before
the first card or after the last card's recognised lines -- not by matching
its English text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# innerText separates block elements with a blank line; within a card, the
# name and its degree marker may share one block across a single newline.
_BLOCK_SPLIT = re.compile(r"\n[ \t]*\n")
_WHITESPACE = re.compile(r"\s+")

# ``Name • 2nd``: the name is single-line and bullet-free; the degree token
# starts with a digit 1-3 that is not followed by another digit (so a
# headline's ``• 2024`` is not a marker) and is the last thing in the block.
_PERSON_HEAD = re.compile(r"^(?P<name>[^\n•]+?)\s*•\s*(?P<degree>[1-3](?!\d)\S{0,3})$")

# A displayed count: ``3K``, ``2.5K``, ``102K``, ``1M``, ``5,200``, ``5.200``,
# ``5 200``. Thousands groups are exactly three digits so a decimal fraction
# (one or two digits) is read as such rather than as a group.
_COUNT = re.compile(
    r"(?P<num>\d+(?:[.,\u202f\u00a0 ]\d{3})*)"
    r"(?:[.,](?P<frac>\d{1,2}))?"
    r"(?P<suffix>[KkMm])?"
)
# The followers line's last segment: a count and exactly one word. The word
# ("followers", "Follower:innen", "abonnés") is never inspected.
_COUNT_AND_WORD = re.compile(rf"^(?P<count>{_COUNT.pattern})\s+\S+$")
# The results header: at most two leading words, a count with optional "+",
# and one trailing word ("About 5,200 results", "Ungefähr 5.200 Ergebnisse",
# "Cerca de 5.200 resultados"). The leading words are taken lazily so a
# space-grouped count ("5 200") is not split between a word and the count.
# The upsell's "12+ additional advanced filters" fails on its three trailing
# words.
_RESULT_COUNT = re.compile(rf"^(?:\S+\s+){{0,2}}?{_COUNT.pattern}\+?\s+\S+$")

# The snippet's label is the only thing separating it from the
# mutual-connections line that follows it, and the label is UI-language text.
# English only, per locale so another table can be added alongside; a snippet
# under an unlisted language is dropped rather than misfiled as a headline.
_SNIPPET_LABELS = {
    "en": ("Current:", "Past:", "About:"),
}
_SNIPPET_LABEL_PREFIXES = tuple(
    label for labels in _SNIPPET_LABELS.values() for label in labels
)

# Reference text is cut at the first of these (link_metadata.clean_label), so
# a card name is cut the same way before the two are compared.
_LABEL_SEPARATORS = (" • ", " · ", " | ")


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in _BLOCK_SPLIT.split(text or "") if block.strip()]


def parse_count(value: str) -> int | None:
    """``3K`` -> 3000, ``2.5K`` -> 2500, ``5,200`` -> 5200; None when not a count."""
    m = _COUNT.fullmatch(value.strip())
    if not m:
        return None
    digits = int(re.sub(r"\D", "", m.group("num")))
    frac = m.group("frac")
    suffix = (m.group("suffix") or "").upper()
    if not suffix:
        # Without a magnitude a fraction cannot be a decimal: "1.234" is a
        # thousands group in half the world, so only whole numbers are read.
        return None if frac else digits
    scale = 1_000 if suffix == "K" else 1_000_000
    whole = digits * scale
    if frac:
        whole += int(frac) * scale // 10 ** len(frac)
    return whole


def _followers(block: str) -> int | None:
    """The count on a followers line, or None when the block is not one."""
    # LinkedIn renders the ``·`` even when nothing precedes it (a lone
    # `` · 3K followers`` on a people card), so a block without one is a
    # name or headline that happens to start with a number -- ``500
    # Startups``, ``100 Employees`` -- and reading it as a count would
    # swallow the neighbouring card.
    if "·" not in block:
        return None
    segment = block.rsplit("·", 1)[-1].strip()
    m = _COUNT_AND_WORD.match(segment)
    return parse_count(m.group("count")) if m else None


def _is_snippet(block: str) -> bool:
    return block.startswith(_SNIPPET_LABEL_PREFIXES)


def _label_key(value: str) -> str:
    value = _WHITESPACE.sub(" ", value).strip()
    for separator in _LABEL_SEPARATORS:
        if separator in value:
            value = value.split(separator, 1)[0].strip()
    return value.casefold()


def _keys_match(card: str, ref: str) -> bool:
    # A promoted top result's anchor reads "Page by <Company>"; the card is
    # still the bare name, so a ref that ends in the card name matches too.
    return bool(card) and (card == ref or ref.endswith(" " + card))


def _pair_urls(
    rows: list[dict[str, Any]], refs: Sequence[Mapping[str, Any]], kind: str
) -> None:
    """Assign each row the URL of the first unconsumed ``kind`` ref whose text
    is the row's name, scanning forward so a duplicate name resolves in page
    order. A ref without text is taken by position. A row nothing matches
    keeps ``url: None`` rather than borrowing the next anchor, which on a
    people page is usually a mutual connection."""
    pool = [ref for ref in refs or [] if ref.get("kind") == kind and ref.get("url")]
    cursor = 0
    for row in rows:
        row["url"] = None
        key = _label_key(row["name"])
        for index in range(cursor, len(pool)):
            text = pool[index].get("text")
            if text is None or _keys_match(key, _label_key(text)):
                row["url"] = pool[index]["url"]
                cursor = index + 1
                break


def parse_people_cards(
    text: str, refs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Rows ``{name, degree, headline, location, snippet, url[, followers]}``
    from a people-search page's innerText and its ``person`` references."""
    blocks = _blocks(text)
    heads = [
        (index, m)
        for index, block in enumerate(blocks)
        if (m := _PERSON_HEAD.match(block))
    ]
    rows: list[dict[str, Any]] = []
    for position, (start, head) in enumerate(heads):
        end = heads[position + 1][0] if position + 1 < len(heads) else len(blocks)
        row: dict[str, Any] = {
            "name": head.group("name").strip(),
            "degree": head.group("degree"),
            "headline": None,
            "location": None,
            "snippet": None,
        }
        plain: list[str] = []
        for block in blocks[start + 1 : end]:
            if _is_snippet(block):
                if row["snippet"] is None:
                    row["snippet"] = block
                continue
            followers = _followers(block)
            if followers is not None:
                # First one wins: the ``• You`` self card is absorbed into
                # the card above it, and its followers line comes after that
                # card's own.
                row.setdefault("followers", followers)
                continue
            plain.append(block)
        # The two positional lines; whatever plain text follows them (the
        # mutual-connections line, the page's trailing chrome) is not a field.
        if plain:
            row["headline"] = plain[0]
        if len(plain) > 1:
            row["location"] = plain[1]
        rows.append(row)
    _pair_urls(rows, refs, "person")
    return rows


def parse_company_cards(
    text: str, refs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Rows ``{name, industry, location, tagline, url[, followers]}`` from a
    company-search page's innerText and its ``company`` references.
    ``tagline`` is None for a card that carries none."""
    blocks = _blocks(text)
    ref_keys = [
        _label_key(ref["text"])
        for ref in refs or []
        if ref.get("kind") == "company" and ref.get("text")
    ]
    rows: list[dict[str, Any]] = []
    previous_end = -1
    for end, block in enumerate(blocks):
        followers = _followers(block)
        if followers is None:
            continue
        window = blocks[previous_end + 1 : end]
        previous_end = end
        # name / industry / location / follow / tagline: five blocks above the
        # followers line, or four for a card without a tagline. The shape is
        # read from the anchors: the slot that carries a company ref's text
        # is the name slot. Five is tried first because a five-line card's
        # industry sits where a four-line name would, and a company on the
        # page can be named like an industry. Five is also what is assumed
        # when no ref names either slot.
        for size in (5, 4):
            if len(window) >= size and any(
                _keys_match(_label_key(window[-size]), key) for key in ref_keys
            ):
                break
        else:
            size = 5
            # Fewer means a card missing a line, which cannot be told from a
            # shifted one, so the card is skipped rather than guessed.
            if len(window) < size:
                continue
            # A tagline-less card slides the window up into the block before
            # it; at the top of the page that is the results header. Only
            # the page's first block is judged: a company can be named
            # "500 Startups", which reads as a count and a word too.
            if end - size == 0 and _RESULT_COUNT.match(window[-size]):
                logger.debug("Skipping a company card under the header: %r", window)
                continue
        name, industry, location, button = window[-size:][:4]
        tagline = window[-1] if size == 5 else None
        # The follow button is one token in every locale. Whitespace there
        # means the card is short a line and the window has slid up into
        # whatever block precedes it -- a promoted event, the page header.
        # (A card short of its tagline that no ref names puts its location in
        # this slot, so a one-word location slips past; the live pages carry
        # "City, Region".)
        if _WHITESPACE.search(button.strip()):
            logger.debug("Skipping a company card short of a line: %r", name)
            continue
        rows.append(
            {
                "name": name,
                "industry": industry,
                "location": location.strip(" ,") or None,
                "tagline": tagline,
                "followers": followers,
            }
        )
    _pair_urls(rows, refs, "company")
    return rows


def parse_result_count(text: str) -> int | None:
    """The "About 5,200 results" header as an int, from the page's first
    blocks; None when the page shows no count."""
    for block in _blocks(text)[:3]:
        if "\n" in block:
            continue
        m = _RESULT_COUNT.match(block)
        if m and not m.group("frac"):
            return int(re.sub(r"\D", "", m.group("num")))
    return None
