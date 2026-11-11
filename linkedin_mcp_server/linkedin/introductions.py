"""Read 2nd-degree search results together with the mutual connections on each.

A 2nd-degree people search card names the person and, below them, the mutual
connections that link the member to that person ("Nicky Dries is a mutual
connection", "Melanie Wiener, Francesco Paolo Appio & 2 other mutual
connections"). Measured on the live page: every named mutual connection is a
profile link inside the card. That makes an introduction readable without any
text: the caller passes the member's own 1st-degree vanity names, a profile
link outside that set is a search result, and the 1st-degree links inside its
card are the people who could introduce the member.

The generic search capture is not used here because its references are
deduplicated and capped at 15 per page: a mutual connection shared by two
results would be credited to the first only, and the last results on a page
lost their links altogether.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

# Every profile link in <main>, in document order and without deduplication,
# grouped into cards. `arg` is the member's own 1st-degree vanity names. A
# card is the largest ancestor of a result's link that holds no other
# result's link; the 1st-degree links inside it are that result's mutual
# connections. Card lines come from visible text nodes, buttons left out, as
# for the connections list.
_SECOND_DEGREE_CARDS_JS = r"""
(firstDegree) => {
  const main = document.querySelector('main');
  if (!main) return [];
  const mine = new Set(firstDegree || []);
  const slugOf = (a) => {
    try {
      const m = new URL(a.href, location.href).pathname.match(/^\/in\/([^/]+)\/?$/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) { return null; }
  };
  const anchors = [...main.querySelectorAll('a[href*="/in/"]')]
    .map(a => ({ a, slug: slugOf(a) })).filter(x => x.slug);
  const resultSlugsIn = (el, except) => {
    for (const x of anchors) {
      if (x.slug !== except && !mine.has(x.slug) && el.contains(x.a)) return true;
    }
    return false;
  };
  const lineOf = (card) => {
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
    return lines;
  };
  const out = [];
  const seen = new Set();
  for (const x of anchors) {
    if (mine.has(x.slug) || seen.has(x.slug)) continue;
    seen.add(x.slug);
    let card = x.a;
    while (card.parentElement && card.parentElement !== main
           && !resultSlugsIn(card.parentElement, x.slug)) {
      card = card.parentElement;
    }
    const mutual = [];
    for (const y of anchors) {
      if (mine.has(y.slug) && card.contains(y.a) && !mutual.includes(y.slug)) {
        mutual.push(y.slug);
      }
    }
    let name = null;
    for (const y of anchors) {
      if (y.slug === x.slug && card.contains(y.a)) {
        // The degree badge renders inside the name link (" • 2nd"); the
        // bullet is the separator in every locale, so cut there.
        const t = (y.a.innerText || '').trim().split('\n')[0].split(' \u2022 ')[0]
          .replace(/\s*\u2022.*$/, '').trim();
        if (t) { name = t; break; }
      }
    }
    out.push({ slug: x.slug, name, mutual, lines: lineOf(card) });
  }
  return out;
}
"""


@dataclass
class SecondDegreeCard:
    """One search result and the member's connections it shares."""

    slug: str
    name: str | None
    mutual: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


class IntroductionReader:
    """Read the cards of one people-search results page."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def read_cards(
        self, url: str, first_degree: set[str]
    ) -> list[SecondDegreeCard]:
        """Navigate to *url* and answer its result cards, in page order."""
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        await self._session.dismiss_modal()
        try:
            await self._session.page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    return !!main && main.innerText.length > 100;
                }""",
                timeout=10000,
            )
        except Exception:
            logger.debug("Search results did not render on %s", url)
        raw = await self._session.run_on_linkedin(
            _SECOND_DEGREE_CARDS_JS, sorted(first_degree)
        )
        return [
            SecondDegreeCard(
                slug=str(c["slug"]),
                name=c.get("name"),
                mutual=[str(m) for m in c.get("mutual") or []],
                lines=[str(x) for x in c.get("lines") or []],
            )
            for c in raw or []
            if c.get("slug")
        ]
