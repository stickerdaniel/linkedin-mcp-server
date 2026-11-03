"""Read the signed-in member's own connections list.

``/mynetwork/invite-connect/connections/`` lists every 1st-degree connection,
newest first, ten at a time, loading more as its scrolling container is
scrolled. Nothing else on LinkedIn lists the member's own network, which is
why this walk exists.

The walk is paced and bounded: it reads what has rendered, scrolls once, waits
a jittered interval, and repeats, handing each new batch to the caller as it
arrives so a call cut short by the tool timeout or a throttle keeps everything
read before it. It stops on its own when the list stops growing, when an
incremental walk has run into connections it already knows, when LinkedIn
answers with a throttling status, or when the time budget is spent.

Card reading follows the page rules: a card is found structurally (the
smallest ancestor of a ``/in/`` anchor that holds no other profile), its
lines are read per element from its visible text nodes with buttons left out,
and no class name or label text is matched.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from linkedin_mcp_server.core.throttle import throttled_count
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

# Seconds between scroll steps: a base plus uniform jitter. Slower than the
# other walks on purpose, because this one runs for minutes, not seconds.
SCROLL_PAUSE = 2.5
SCROLL_JITTER = 2.0

# Rounds with nothing new before the list is taken as fully loaded.
STALE_ROUNDS = 3

_YEAR = re.compile(r"\b(19|20)\d{2}\b")

# English only, and guarded as such: in any other locale the raw text is kept
# and `connected_on` stays empty, rather than guessing at a translation.
_CONNECTED_EN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{1,2}),\s+(\d{4})"
)

# Reads every card not yet returned in this walk. `arg` is the list of vanity
# names already returned, so a long list costs only its new cards per round.
_COLLECT_CARDS_JS = r"""
(already) => {
  const main = document.querySelector('main');
  if (!main) return [];
  const skip = new Set(already || []);
  const slugOf = (href) => {
    try {
      const m = new URL(href, location.href).pathname.match(/^\/in\/([^/]+)\/?$/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) { return null; }
  };
  const profileSlugs = (el) => {
    const out = new Set();
    for (const a of el.querySelectorAll('a[href*="/in/"]')) {
      const s = slugOf(a.href);
      if (s) { out.add(s); if (out.size > 1) break; }
    }
    return out;
  };
  const found = new Map();
  for (const a of main.querySelectorAll('a[href*="/in/"]')) {
    const slug = slugOf(a.href);
    if (!slug || skip.has(slug) || found.has(slug)) continue;
    let card = a;
    while (card.parentElement && card.parentElement !== main
           && profileSlugs(card.parentElement).size <= 1) {
      card = card.parentElement;
    }
    found.set(slug, card);
  }
  const out = [];
  for (const [slug, card] of found) {
    // One line per element that owns visible text, buttons excluded. Read
    // from text nodes rather than innerText, which joins inline elements
    // into one line and so depends on how LinkedIn styles the card.
    const lines = [];
    let lastParent = null;
    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const t = n.nodeValue.replace(/\s+/g, ' ').trim();
      const p = n.parentElement;
      if (!t || !p || p.closest('button') || p.getClientRects().length === 0) continue;
      if (p === lastParent) { lines[lines.length - 1] += ' ' + t; continue; }
      if (lines[lines.length - 1] !== t) lines.push(t);
      lastParent = p;
    }
    let name = null;
    for (const a of card.querySelectorAll('a[href*="/in/"]')) {
      const t = (a.innerText || '').trim().split('\n')[0].trim();
      if (t) { name = t; break; }
    }
    out.push({ slug, name, lines });
  }
  return out;
}
"""

# Brings the last profile into view and scrolls the list's own container to
# its end, which is what makes LinkedIn append the next page of cards.
# Measured on the live page: the window does not scroll at all, because the
# list sits in a scrollable <main>; scrolling that element loads ten more
# cards each time. The container is found as the nearest scrollable ancestor
# of a profile anchor, not by name, so a layout that scrolls the window again
# still works. Nothing is clicked: the only buttons after the last card on
# the live page belong to an ad ("Dismiss", "Submit"), and there is no
# load-more button to find. Answers the number of profile anchors, which the
# walk waits on to grow.
_ADVANCE_JS = r"""
() => {
  const main = document.querySelector('main');
  if (!main) return { anchors: 0, scrolled: false };
  const anchors = main.querySelectorAll('a[href*="/in/"]');
  const last = anchors[anchors.length - 1];
  if (!last) return { anchors: 0, scrolled: false };
  last.scrollIntoView({ block: 'end' });
  let scrolled = false;
  for (let el = last.parentElement; el; el = el.parentElement) {
    const style = getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight) {
      el.scrollTop = el.scrollHeight;
      scrolled = true;
      break;
    }
  }
  if (!scrolled) window.scrollTo(0, document.body.scrollHeight);
  return { anchors: anchors.length, scrolled };
}
"""

# True once the list holds more profile anchors than `arg`.
_GREW_JS = """(before) => {
    const main = document.querySelector('main');
    return !!main && main.querySelectorAll('a[href*="/in/"]').length > before;
}"""

# The size the page states for the list: the first line of <main> when it
# starts with a number ("1,473 connections" on the live page). Only the
# digits are kept, so any locale's thousands separator reads the same; a
# page without such a line answers null rather than a guess.
_LISTED_TOTAL_JS = r"""
() => {
  const main = document.querySelector('main');
  if (!main) return null;
  const first = (main.innerText || '').split('\n').map(s => s.trim()).find(Boolean);
  const m = first && first.match(/^(\d[\d.,\s\u00a0\u202f]*)\s*\D/);
  return m ? parseInt(m[1].replace(/\D/g, ''), 10) : null;
}
"""

# Seconds to wait for the list to grow after a scroll before counting the
# round as stale. Live, the next ten cards arrived within two seconds.
GROW_TIMEOUT_MS = 8000


@dataclass(frozen=True, slots=True)
class PageConnection:
    """One card read from the connections page."""

    slug: str
    name: str | None
    headline: str | None
    connected_text: str | None
    connected_on: str | None
    profile_url: str | None = None


def parse_connected_on(text: str | None) -> str | None:
    """ISO date from an English "Connected on March 5, 2024" line, else None."""
    if not text:
        return None
    m = _CONNECTED_EN.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def card_to_connection(card: dict[str, Any]) -> PageConnection | None:
    """Turn one raw card into a connection; None when it carries no slug.

    The first line is the name, the last line with a year in it is the
    connection date, and the first remaining line is the headline. A year is
    the one locale-independent mark on the date line; taking the last one
    leaves a headline such as "PhD candidate, class of 2026" alone, since the
    date renders below it.
    """
    slug = card.get("slug")
    if not slug:
        return None
    lines: list[str] = [str(x) for x in card.get("lines") or []]
    name = card.get("name") or (lines[0] if lines else None)
    rest = [ln for ln in lines if ln != name]
    # The headline comes first, so the first line is the date only when it
    # reads as one.
    connected_text = next(
        (
            ln
            for i, ln in reversed(list(enumerate(rest)))
            if _YEAR.search(ln) and (i > 0 or parse_connected_on(ln))
        ),
        None,
    )
    headline = next((ln for ln in rest if ln != connected_text), None)
    return PageConnection(
        slug=slug,
        name=name,
        headline=headline,
        connected_text=connected_text,
        connected_on=parse_connected_on(connected_text),
    )


@dataclass
class WalkResult:
    """How a walk ended and what it read."""

    seen: int = 0
    rounds: int = 0
    stop_reason: str = ""
    complete: bool = False
    listed_total: int | None = None
    throttled: bool = False
    new_slugs: list[str] = field(default_factory=list)


OnBatch = Callable[[list[PageConnection]], Awaitable[None] | None]
OnProgress = Callable[[str, int], Awaitable[None]]


class MyConnectionsReader:
    """Walk the connections page, handing each new batch to a callback."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def walk(
        self,
        on_batch: OnBatch,
        *,
        known: set[str] | None = None,
        stop_after_known: int | None = None,
        time_budget: float = 600.0,
        max_rounds: int = 400,
        on_progress: OnProgress | None = None,
    ) -> WalkResult:
        """Read connections until the list ends or a stop condition holds.

        Args:
            on_batch: Called with every round's new connections, in page order.
            known: Vanity names already stored. Only consulted with
                *stop_after_known*.
            stop_after_known: Incremental mode. The list is newest first, so
                once this many consecutive cards are already *known*, the
                rest of the list is too and the walk stops there.
            time_budget: Seconds the walk may run.
            max_rounds: Scroll steps the walk may take.
            on_progress: Optional ``(message, connections_read)`` reporter.
        """
        known = known or set()
        result = WalkResult()
        returned: list[str] = []
        consecutive_known = 0
        stale = 0
        throttle_before = throttled_count()
        deadline = self._session.monotonic() + time_budget

        await self._navigator._navigate_to_page(CONNECTIONS_URL)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        try:
            await self._session.page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    return !!main && main.querySelectorAll('a[href*="/in/"]').length > 0;
                }""",
                timeout=15000,
            )
        except Exception:
            logger.debug("Connections list did not render any profile links")
        listed = await self._session.run_on_linkedin(_LISTED_TOTAL_JS)
        result.listed_total = listed if isinstance(listed, int) else None

        while True:
            raw = await self._session.run_on_linkedin(_COLLECT_CARDS_JS, returned)
            batch = [c for c in (card_to_connection(r) for r in raw or []) if c]
            result.rounds += 1

            if batch:
                stale = 0
                returned.extend(c.slug for c in batch)
                result.seen += len(batch)
                result.new_slugs.extend(c.slug for c in batch if c.slug not in known)
                maybe = on_batch(batch)
                if maybe is not None:
                    await maybe
                if stop_after_known is not None:
                    for c in batch:
                        consecutive_known = (
                            consecutive_known + 1 if c.slug in known else 0
                        )
                        if consecutive_known >= stop_after_known:
                            break
            else:
                stale += 1

            if on_progress is not None:
                await on_progress(f"Read {result.seen} connections", result.seen)

            if stop_after_known is not None and consecutive_known >= stop_after_known:
                result.stop_reason = "reached_known_connections"
                result.complete = True
                break
            if stale >= STALE_ROUNDS:
                result.stop_reason = "end_of_list"
                result.complete = True
                break
            if throttled_count() > throttle_before:
                result.stop_reason = "throttled"
                result.throttled = True
                break
            if result.rounds >= max_rounds:
                result.stop_reason = "max_rounds"
                break
            if self._session.monotonic() >= deadline:
                result.stop_reason = "time_budget"
                break

            advanced = await self._session.run_on_linkedin(_ADVANCE_JS) or {}
            try:
                await self._session.page.wait_for_function(
                    _GREW_JS,
                    arg=int(advanced.get("anchors", 0)),
                    timeout=GROW_TIMEOUT_MS,
                )
            except Exception:
                logger.debug("Connections list did not grow after scrolling")
            await self._session.delay(SCROLL_PAUSE + random.uniform(0, SCROLL_JITTER))
            await self._session.check_rate_limit()

        return result
