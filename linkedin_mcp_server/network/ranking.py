"""Rank introduction paths: you -> introducer -> target.

A 2nd-degree search answers far more people than anyone can approach, and
each through several mutual connections. What makes a path worth taking is
mostly the first hop: whether the member actually knows the introducer. Then
whether the introducer plausibly knows the target, and how many paths lead to
the same person. Every score comes with the reasons that produced it, so a
ranking can be argued with rather than trusted.

The signals, strongest first:

- the member's own closeness mark (``mark_connections``), which beats any
  proxy;
- message history from LinkedIn's ``messages.csv`` export: how many messages
  and how recently;
- a shared organisation between introducer and target, read from the
  target's card and the introducer's company, headline and enriched
  experience;
- the number of mutual connections on the card;
- how long the member has been connected to the introducer, a weak proxy.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

# Weights. A closeness mark dominates; messages come close; the rest nudge.
MARK_WEIGHT = 1.0
MESSAGE_WEIGHT = 0.8
MESSAGE_SATURATION = 40  # messages at which the message signal is full
RECENT_DAYS = 365
RECENT_BONUS = 0.2
AGE_WEIGHT = 0.2
AGE_SATURATION_YEARS = 10
SHARED_ORG_BONUS = 0.5
EXTRA_PATH_BONUS = 0.15
EXTRA_PATH_CAP = 3
RELEVANCE_WEIGHT = 0.3

# "Role at Org", "Role @ Org". English " at " is the one locale-specific
# separator here; "@" reads the same everywhere. A headline in another
# language still contributes its company field and "@" segments.
_ORG_SPLIT = re.compile(r"\s+at\s+|\s*@\s*", re.IGNORECASE)
_SEGMENT_END = re.compile(r"\s*[|•·,;/(]\s*")
_MIN_ORG = 4


def _orgs_in(text: str | None) -> list[str]:
    if not text:
        return []
    parts = _ORG_SPLIT.split(text)
    out = []
    for part in parts[1:]:
        org = _SEGMENT_END.split(part, maxsplit=1)[0].strip(" .-")
        if len(org) >= _MIN_ORG:
            out.append(org)
    return out


def organisations(
    person: dict[str, Any], experience_text: str | None = None
) -> list[str]:
    """Organisations a stored person is or was at, as far as the store says."""
    orgs = []
    if person.get("company"):
        orgs.append(str(person["company"]))
    orgs += _orgs_in(person.get("headline"))
    orgs += _orgs_in(person.get("position"))
    if experience_text:
        # Experience pages list one employer per entry line; keep lines that
        # read as names rather than dates or descriptions.
        for line in experience_text.splitlines():
            line = line.strip()
            if _MIN_ORG <= len(line) <= 60 and not re.search(r"\d", line):
                orgs.append(line.split(" · ")[0])
    seen: dict[str, str] = {}
    for org in orgs:
        seen.setdefault(org.casefold(), org)
    return list(seen.values())


def shared_organisations(
    introducer: dict[str, Any],
    target_lines: list[str],
    introducer_experience: str | None = None,
) -> list[str]:
    """Organisations of the introducer that the target's card names."""
    card = " \n ".join(target_lines).casefold()
    card_orgs = {o.casefold() for line in target_lines for o in _orgs_in(line)}
    shared = []
    for org in organisations(introducer, introducer_experience):
        key = org.casefold()
        if key in card_orgs or re.search(rf"(?<!\w){re.escape(key)}(?!\w)", card):
            shared.append(org)
    return shared


def tie_strength(tie: dict[str, Any] | None, today: date) -> tuple[float, list[str]]:
    """How well the member knows a connection, with the reasons."""
    if not tie:
        return 0.0, []
    score = 0.0
    reasons: list[str] = []
    level = tie.get("level")
    if level is not None:
        score += MARK_WEIGHT * int(level) / 3
        reasons.append(
            f"you marked closeness {level}/3"
            + (f" ({tie['note']})" if tie.get("note") else "")
        )
    n = tie.get("messages") or 0
    if n:
        score += MESSAGE_WEIGHT * min(
            1.0, math.log1p(n) / math.log1p(MESSAGE_SATURATION)
        )
        last = tie.get("last_message")
        reasons.append(f"{n} LinkedIn messages" + (f", last {last}" if last else ""))
        if last:
            try:
                if (today - date.fromisoformat(last)).days <= RECENT_DAYS:
                    score += RECENT_BONUS
            except ValueError:
                pass
    connected = tie.get("connected_on")
    if connected:
        try:
            years = (today - date.fromisoformat(connected)).days / 365.25
        except ValueError:
            years = 0.0
        if years >= 1:
            score += AGE_WEIGHT * min(1.0, years / AGE_SATURATION_YEARS)
            reasons.append(
                f"connected {int(years)} year{'' if int(years) == 1 else 's'}"
            )
    if level == 0:
        # An explicit "I don't know them" outweighs every proxy.
        score *= 0.2
    return round(score, 3), reasons


def rank_paths(
    results: list[dict[str, Any]],
    ties: dict[str, dict[str, Any]],
    experience: dict[str, str | None],
    today: date,
) -> list[dict[str, Any]]:
    """Score each result's best path and order the results by it.

    *results* are in LinkedIn's relevance order, each with ``card`` lines
    and ``introducers`` (stored people). Each introducer gains ``tie``,
    ``shared`` and ``reasons``; each result gains ``score``, ``best_via``
    and ``reasons``.
    """
    n = len(results)
    for rank, result in enumerate(results):
        scored = []
        for intro in result["introducers"]:
            strength, reasons = tie_strength(ties.get(intro["slug"]), today)
            shared = shared_organisations(
                intro, result.get("card", []), experience.get(intro["slug"])
            )
            value = strength
            if shared:
                value += SHARED_ORG_BONUS
                reasons = [*reasons, f"also at {', '.join(shared)}"]
            intro["tie"] = round(value, 3)
            intro["reasons"] = reasons
            scored.append(intro)
        scored.sort(key=lambda i: -i["tie"])
        result["introducers"] = scored
        best = scored[0]["tie"] if scored else 0.0
        extra = min(max(len(scored) - 1, 0), EXTRA_PATH_CAP) * EXTRA_PATH_BONUS
        relevance = RELEVANCE_WEIGHT * (1 - rank / n) if n else 0.0
        result["score"] = round(best + extra + relevance, 3)
        result["best_via"] = scored[0]["slug"] if scored else None
        why = []
        if scored:
            why.append(f"best path via {scored[0].get('name') or scored[0]['slug']}")
            why += scored[0]["reasons"]
        if len(scored) > 1:
            why.append(f"{len(scored)} mutual connections you know")
        why.append(f"search rank {rank + 1} of {n}")
        result["reasons"] = why
    return sorted(results, key=lambda r: -r["score"])
