"""List the signed-in member's connections and remove chosen ones.

Removal works from LinkedIn's own connections page, one person at a time: the
row is found by the person's profile slug (never by position or name), its
"more actions" menu is opened, the remove item is clicked, the confirmation is
accepted, and the person is then looked up again and must be gone. A dry run
opens the menu, checks that the remove item is there, and closes it.

Every LinkedIn fact used here was measured on 12 October 2026 (en UI):

- ``/mynetwork/invite-connect/connections/`` lists connections; more rows load
  as the page scrolls, and the page states the total ("32 connections").
- Each row holds one profile link (``/in/<slug>/``) and one button with
  ``aria-expanded`` that opens the row's menu.
"""

from __future__ import annotations

from typing import Any

import logging
import re
from urllib.parse import unquote

from patchright.async_api import Locator, Page

import linkedin_mcp_server.linkedin.profile_selectors as sel
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import NAV_DELAY, PageSession
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)

logger = logging.getLogger(__name__)

_SETTLE_MS = 1_500
_MAX_SCROLLS = 30
_MENU_TIMEOUT_MS = 5_000
_DIALOG_TIMEOUT_MS = 8_000

# Visible-text fallbacks for the two controls that have no other identity.
# Exact matches only; extend per locale.
REMOVE_LABELS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "menu_remove": ("Remove connection",),
        "confirm_remove": ("Remove connection", "Remove"),
    },
}

# Runs in the page: every connection row currently rendered, by slug.
ROWS_JS = r"""
() => {
  const main = document.querySelector('main') || document.body;
  const out = [], seen = new Set();
  for (const a of main.querySelectorAll('a[href*="/in/"]')) {
    const m = a.getAttribute('href').match(/\/in\/([^/?#]+)/);
    if (!m || seen.has(m[1])) continue;
    let row = a;
    for (let i = 0; i < 8 && row.parentElement; i++) {
      row = row.parentElement;
      if (row.querySelector('button[aria-expanded]')) break;
    }
    const menus = row.querySelectorAll('button[aria-expanded]');
    if (menus.length !== 1) continue;
    seen.add(m[1]);
    const lines = row.innerText.split('\n').map((s) => s.trim()).filter(Boolean);
    // Name: the row's first text line or, when the row shows none, the menu
    // button's accessible name ("More actions for <Name>", en).
    const aria = menus[0].getAttribute('aria-label') || '';
    out.push({slug: m[1], name: lines[0] || aria.replace(/^More actions for /, '') || null});
  }
  const stated = (main.innerText.match(/(\d[\d,]*)\s+connections?/i) || [])[1];
  return {rows: out, stated: stated ? Number(stated.replace(/,/g, '')) : null};
}
"""

# Runs in the page (diagnostics): the buttons of every visible dialog.
DIALOG_BUTTONS_JS = r"""
() => [...document.querySelectorAll('dialog, [role="alertdialog"], [role="dialog"]')]
  .filter((d) => d.offsetWidth || d.offsetHeight)
  .map((d) => ({
    role: d.getAttribute('role') || d.tagName.toLowerCase(),
    heading: ((d.querySelector('h1, h2, h3') || {}).innerText || '').trim().slice(0, 80),
    buttons: [...d.querySelectorAll('button')].map((b) => ({
      text: b.innerText.trim().slice(0, 40),
      aria: b.getAttribute('aria-label'),
    })),
  }))
"""

# Runs in the page: bring the last row into view so the next batch loads.
SCROLL_JS = r"""
() => {
  const links = [...(document.querySelector('main') || document.body).querySelectorAll('a[href*="/in/"]')];
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  window.scrollBy(0, 2000);
  return links.length;
}
"""


def _slug(value: str) -> str:
    """A profile slug from a username or profile URL, decoded and lower-cased."""
    m = re.search(r"/in/([^/?#]+)", value)
    return unquote(m.group(1) if m else value.strip().strip("/")).lower()


class ConnectionRemover:
    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator
        self._labels = REMOVE_LABELS["en"]
        self._navigations = 0

    @property
    def _page(self) -> Page:
        return self._session.page

    async def _goto(self, url: str) -> None:
        if self._navigations:
            await self._session.delay(NAV_DELAY)
        self._navigations += 1
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        lang = (await self._page.evaluate("document.documentElement.lang") or "")[:2]
        table = REMOVE_LABELS.get(lang.lower())
        if table is None:
            raise ProfileEditError(
                ProfileEditErrorCode.SELECTOR_NOT_FOUND,
                f"Connection removal knows no labels for UI language '{lang}'. "
                "Switch LinkedIn's display language to English.",
                language=lang,
            )
        self._labels = table

    async def list_connections(self) -> dict[str, Any]:
        """Every connection, loading rows until the page's stated total is reached."""
        await self._goto(sel.CONNECTIONS_URL)
        await self._page.wait_for_timeout(_SETTLE_MS)
        found: dict[str, dict[str, Any]] = {}
        stated = None
        stalls = 0
        for _ in range(_MAX_SCROLLS):
            data = await self._page.evaluate(ROWS_JS)
            stated = data["stated"] or stated
            before = len(found)
            for row in data["rows"]:
                found.setdefault(_slug(row["slug"]), row)
            if stated is not None and len(found) >= stated:
                break
            stalls = stalls + 1 if len(found) == before else 0
            if stalls >= 3:
                break
            await self._page.evaluate(SCROLL_JS)
            await self._page.wait_for_timeout(_SETTLE_MS)
        rows = [
            {"username": slug, "name": r["name"], "url": sel.profile_url(slug)}
            for slug, r in found.items()
        ]
        return {
            "connections": rows,
            "count": len(rows),
            "statedCount": stated,
            "complete": stated is None or len(rows) >= stated,
        }

    async def _row(self, slug: str) -> Locator | None:
        """The row of *slug*, scrolling until it is rendered; None if absent."""
        for _ in range(_MAX_SCROLLS):
            rows = await self._page.evaluate(ROWS_JS)
            if any(_slug(r["slug"]) == slug for r in rows["rows"]):
                link = self._page.locator(f'main a[href*="/in/{slug}"]').first
                if not await link.count():
                    # Slugs with non-ASCII characters are percent-encoded in hrefs.
                    return await self._row_by_scan(slug)
                return link
            before = len(rows["rows"])
            await self._page.evaluate(SCROLL_JS)
            await self._page.wait_for_timeout(_SETTLE_MS)
            after = len((await self._page.evaluate(ROWS_JS))["rows"])
            if after == before:
                return None
        return None

    async def _row_by_scan(self, slug: str) -> Locator | None:
        links = self._page.locator('main a[href*="/in/"]')
        for i in range(await links.count()):
            href = await links.nth(i).get_attribute("href") or ""
            if _slug(href) == slug:
                return links.nth(i)
        return None

    async def _menu_button(self, link: Locator) -> Locator:
        """The one menu opener in the row that holds *link*."""
        row = link
        for _ in range(8):
            row = row.locator("xpath=..")
            buttons = row.locator("button[aria-expanded]")
            n = await buttons.count()
            if n == 1:
                return buttons
            if n > 1:
                break
        raise ProfileEditError(
            ProfileEditErrorCode.SELECTOR_NOT_FOUND,
            "Could not find exactly one menu button in the connection's row.",
        )

    async def _menu_items(self) -> list[str]:
        items = self._page.locator(
            '[role="menu"]:visible [role="menuitem"], [role="menu"]:visible li'
        )
        texts: list[str] = []
        for i in range(await items.count()):
            t = " ".join((await items.nth(i).inner_text()).split())
            if t and t not in texts:
                texts.append(t)
        return texts

    async def remove(self, username: str, *, dry_run: bool) -> dict[str, Any]:
        slug = _slug(username)
        await self._goto(sel.CONNECTIONS_URL)
        await self._page.wait_for_timeout(_SETTLE_MS)
        link = await self._row(slug)
        if link is None:
            return {"username": slug, "status": "NOT_CONNECTED", "verified": True}
        opener = await self._menu_button(link)
        await opener.click()
        try:
            await self._page.locator('[role="menu"]:visible').last.wait_for(
                state="visible", timeout=_MENU_TIMEOUT_MS
            )
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.SELECTOR_NOT_FOUND,
                "The connection's menu did not open.",
                username=slug,
            ) from None
        # The menu just opened: the page can hold other, hidden menus.
        menu = self._page.locator('[role="menu"]:visible').last
        item = None
        for text in self._labels["menu_remove"]:
            # By role: the item's own text node also matches a plain text query.
            loc = menu.get_by_role("menuitem", name=text, exact=True)
            if await loc.count() != 1:
                loc = menu.locator('[role="menuitem"], li').filter(
                    has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")
                )
            if await loc.count() == 1:
                item = loc
                break
        if item is None:
            items = await self._menu_items()
            markup = await menu.evaluate(
                "(m) => m.outerHTML.replace(/\\s+/g, ' ').replace(/ class=\"[^\"]*\"/g, '').slice(0, 1500)"
            )
            await self._page.keyboard.press("Escape")
            raise ProfileEditError(
                ProfileEditErrorCode.SELECTOR_NOT_FOUND,
                "The menu has no exact 'remove connection' item; nothing was clicked.",
                username=slug,
                menuItems=items,
                menuMarkup=markup,
            )
        if dry_run:
            items = await self._menu_items()
            await self._page.keyboard.press("Escape")
            return {
                "username": slug,
                "status": "WOULD_REMOVE",
                "verified": True,
                "menuItems": items,
            }
        await item.click()
        # Any visible dialog may be the confirmation (the page also keeps other
        # dialogs); the confirm button must be the only exact match among them.
        dialogs = self._page.locator(
            'dialog:visible, [role="alertdialog"]:visible, [role="dialog"]:visible'
        )
        try:
            await dialogs.first.wait_for(state="visible", timeout=_DIALOG_TIMEOUT_MS)
        except Exception:
            raise ProfileEditError(
                ProfileEditErrorCode.SELECTOR_NOT_FOUND,
                "No confirmation appeared after 'remove connection'; nothing was confirmed.",
                username=slug,
            ) from None
        await self._page.wait_for_timeout(500)
        confirm = None
        for text in self._labels["confirm_remove"]:
            loc = dialogs.get_by_role("button", name=text, exact=True)
            if await loc.count() == 1:
                confirm = loc
                break
        if confirm is None:
            buttons = await self._page.evaluate(DIALOG_BUTTONS_JS)
            await self._page.keyboard.press("Escape")
            raise ProfileEditError(
                ProfileEditErrorCode.SELECTOR_NOT_FOUND,
                "The confirmation has no single exact 'Remove' button; it was dismissed.",
                username=slug,
                dialogButtons=buttons,
            )
        await confirm.click()
        await self._page.wait_for_timeout(_SETTLE_MS)
        # Verify from a fresh load of the page.
        await self._goto(sel.CONNECTIONS_URL)
        await self._page.wait_for_timeout(_SETTLE_MS)
        still = await self._row(slug)
        if still is not None:
            raise ProfileEditError(
                ProfileEditErrorCode.VERIFICATION_FAILED,
                "The person is still listed after removal.",
                username=slug,
            )
        return {"username": slug, "status": "REMOVED", "verified": True}
