"""Read the signed-in member's own connections list.

``/mynetwork/invite-connect/connections/`` lists every 1st-degree connection,
newest first, and loads more as the page is scrolled (some layouts add a
"show more" button at the end of the list). Nothing else on LinkedIn lists the
member's own network, which is why this walk exists.

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

# Scrolls to the end and presses a trailing load-more button when one exists.
# The button is identified by position, not label: a visible, enabled button
# inside <main>, after the last profile anchor in document order, outside every
# card, and not a menu or disclosure control.
_ADVANCE_JS = r"""
() => {
  window.scrollTo(0, document.body.scrollHeight);
  const main = document.querySelector('main');
  if (!main) return { clicked: false };
  const anchors = main.querySelectorAll('a[href*="/in/"]');
  const last = anchors[anchors.length - 1];
  if (!last) return { clicked: false };
  const slugs = (el) => {
    const out = new Set();
    for (const a of el.querySelectorAll('a[href*="/in/"]')) {
      const m = new URL(a.href, location.href).pathname.match(/^\/in\/([^/]+)\/?$/);
      if (m) { out.add(m[1]); if (out.size > 1) break; }
    }
    return out;
  };
  // Inside a card when the nearest ancestor holding any profile holds one.
  const inCard = (b) => {
    for (let el = b.parentElement; el && el !== document.body; el = el.parentElement) {
      if (el.querySelector('a[href*="/in/"]')) return slugs(el).size <= 1;
    }
    return false;
  };
  for (const b of main.querySelectorAll('button')) {
    if (!(last.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
    if (inCard(b)) continue;
    if (b.hasAttribute('aria-haspopup') || b.hasAttribute('aria-expanded')) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
    const r = b.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    b.click();
    return { clicked: true };
  }
  return { clicked: false };
}
"""


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

            await self._session.run_on_linkedin(_ADVANCE_JS)
            await self._session.delay(SCROLL_PAUSE + random.uniform(0, SCROLL_JITTER))
            await self._session.check_rate_limit()

        return result
