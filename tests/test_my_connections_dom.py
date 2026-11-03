"""Browser-DOM tests for the connections-page programs.

The walk's unit tests stand in for ``run_on_linkedin``, so the two programs in
``linkedin/my_connections.py`` never run there. What a card is, which lines
are dropped as buttons, and which button counts as "load more" are all decided
inside the JavaScript, so these cases run it in headless chromium against a
synthetic list. The fixture uses no LinkedIn class names on purpose: the
programs match none, and a fixture that carried them would hide a regression
that started depending on one.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.linkedin.my_connections import (
    _ADVANCE_JS,
    _COLLECT_CARDS_JS,
    CONNECTIONS_URL,
    card_to_connection,
)
from linkedin_mcp_server.linkedin.session import PageSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def card(slug: str, name: str, headline: str, connected: str) -> str:
    # Two anchors to the same profile (photo and name), as the real card has.
    return f"""
    <li>
      <div>
        <a href="/in/{slug}/"><img alt=""></a>
        <div>
          <a href="https://www.linkedin.com/in/{slug}/"><span>{name}</span></a>
          <span>{headline}</span>
          <time>{connected}</time>
        </div>
        <div><button>Message</button><button aria-haspopup="true">More</button></div>
      </div>
    </li>"""


def page_html(cards: list[str], *, load_more: bool = False) -> str:
    more = (
        """<button onclick="document.getElementById('log').textContent += 'more'">
             Show more results</button>"""
        if load_more
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Connections</title></head>
<body>
  <nav><a href="/in/me/">Me</a></nav>
  <p id="log"></p>
  <main>
    <header><button aria-expanded="false">Sort by</button></header>
    <ul>{"".join(cards)}</ul>
    {more}
  </main>
</body></html>"""


async def load(page: Page, html: str) -> None:
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    await page.goto(CONNECTIONS_URL)


CARDS = [
    card(
        "ada-lovelace",
        "Ada Lovelace",
        "Analyst at Engines Ltd",
        "Connected on March 5, 2024",
    ),
    card(
        "alan-turing",
        "Alan Turing",
        "PhD candidate, class of 2026",
        "Connected on January 12, 2023",
    ),
    card("grac%C3%A9-h", "Gracé Hopper", "Rear Admiral", "Connected on July 1, 2020"),
]


async def test_each_profile_is_one_card_with_its_own_lines(dom_page):
    await load(dom_page, page_html(CARDS))
    raw = await PageSession(dom_page).run_on_linkedin(_COLLECT_CARDS_JS, [])

    assert [c["slug"] for c in raw] == ["ada-lovelace", "alan-turing", "gracé-h"]
    first = raw[0]
    assert first["name"] == "Ada Lovelace"
    # Button text is dropped, and the nav's own-profile link is outside <main>.
    assert "Message" not in first["lines"] and "More" not in first["lines"]
    assert first["lines"] == [
        "Ada Lovelace",
        "Analyst at Engines Ltd",
        "Connected on March 5, 2024",
    ]

    parsed = [c for c in map(card_to_connection, raw) if c is not None]
    assert len(parsed) == 3
    assert parsed[0].headline == "Analyst at Engines Ltd"
    assert parsed[0].connected_on == "2024-03-05"
    # A year in the headline is not mistaken for the connection date.
    assert parsed[1].headline == "PhD candidate, class of 2026"
    assert parsed[1].connected_on == "2023-01-12"


async def test_already_returned_profiles_are_skipped(dom_page):
    await load(dom_page, page_html(CARDS))
    raw = await PageSession(dom_page).run_on_linkedin(
        _COLLECT_CARDS_JS, ["ada-lovelace", "gracé-h"]
    )
    assert [c["slug"] for c in raw] == ["alan-turing"]


async def test_advance_presses_the_trailing_button_and_not_menus(dom_page):
    await load(dom_page, page_html(CARDS, load_more=True))
    answer = await PageSession(dom_page).run_on_linkedin(_ADVANCE_JS)
    log = await dom_page.evaluate("() => document.getElementById('log').textContent")
    assert answer == {"clicked": True}
    assert log == "more"


async def test_advance_without_a_load_more_button_clicks_nothing(dom_page):
    await load(dom_page, page_html(CARDS))
    answer = await PageSession(dom_page).run_on_linkedin(_ADVANCE_JS)
    log = await dom_page.evaluate("() => document.getElementById('log').textContent")
    assert answer == {"clicked": False}
    assert log == ""
