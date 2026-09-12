"""Browser-DOM tests for the messaging sidebar programs.

The unit suite mocks ``page.evaluate``, so the three programs in
``scraping/conversations.py`` never execute there: the click-to-capture loop,
the scrollable-region walk and the main-text wait are all asserted as call
arguments and nothing else. These cases run them in headless chromium.

The click loop is the reason this file exists. Selecting a conversation row
marks the thread read on LinkedIn, so the order of the name filter and the
click is the closest thing to a write anywhere in the scraping package, and
that ordering lives entirely inside the JavaScript. A mocked ``evaluate``
cannot tell a loop that filters first from one that clicks first.

Every fixture drives a synthetic container, so each case is a claim about the
algorithm rather than about LinkedIn's markup — with one deliberate exception
named in ``sidebar()``: the click target is selected by a class-name substring
that only LinkedIn's Ember output produces, and a fixture that did not
reproduce it would assert nothing about the selector.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.conversations import ConversationReader
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
#: Without that distribution mode the group mark is inert.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

BASE_URL = "https://www.linkedin.com/messaging/"


async def _no_message_target() -> Any:
    raise AssertionError("the conversation reader never reads a message target")


def _reader(page: Page) -> ConversationReader:
    """Wire the conversation owner the way the facade does."""
    session = ScrapingSession(page)
    return ConversationReader(
        session,
        PageNavigator(session),
        PageContentReader(session),
        ProfilePageReader(session, _no_message_target),
    )


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it so
    an assertion failure or JS error in a test body is never swallowed into a
    skip.

    ``channel="chromium"`` names the browser this project installs. Without it
    Playwright picks the *binary* from the ``headless`` flag alone and asks for
    ``chromium-headless-shell``, which nothing here installs since the setup
    moved to ``--no-shell``.
    """
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


def sidebar(
    rows: list[tuple[str, str]], *, clickable: bool = True, routes: bool = True
) -> str:
    """A messaging sidebar of ``(aria-label, thread-id)`` rows.

    Each row carries an inner ``div`` whose class name contains
    ``listitem__link``, because that substring is the production selector: the
    Ember click handler sits on that div and neither the ``<li>`` nor the
    ``<label>`` triggers SPA navigation. A row appends its own id to ``#log``
    and then rewrites ``location`` through ``history.pushState``, which is how
    the real sidebar moves — the recorded order is what separates a loop that
    filters before it clicks from one that does not.

    The log is a DOM node rather than a global, because patchright evaluates
    in an isolated world: it shares the document with the page and shares no
    JavaScript globals with it, so a ``window`` property set by an inline
    handler reads back as ``undefined`` and every such assertion would pass
    vacuously.

    ``clickable=False`` drops that inner div, standing in for a row LinkedIn
    rendered without a handler. ``routes=False`` keeps the handler and drops
    only the ``pushState``, standing in for a row whose click never reaches the
    thread route. Both are built here rather than patched in afterwards,
    because rewriting an ``onclick`` attribute from the isolated world leaves
    the page's own compiled handler in place and adds a second one: measured,
    the row then logs its click twice.
    """
    items = []
    for label, thread_id in rows:
        route = (
            f"history.pushState({{}}, '', '/messaging/thread/{thread_id}/')"
            if routes
            else ""
        )
        inner = (
            f'<div class="msg-conversation-listitem__link" '
            f"onclick=\"document.getElementById('log')"
            f".textContent += ' {thread_id}'; "
            f'{route}">'
            f"<span>{label}</span></div>"
            if clickable
            else f"<span>{label}</span>"
        )
        items.append(f'<li><label aria-label="{label}">{inner}</label></li>')
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <p id="log"></p>
    <main><ul>{"".join(items)}</ul></main>
  </body>
</html>
"""


def scroll_document(
    *, panes: list[tuple[str, int, int]], overflow: str = "auto"
) -> str:
    """``main`` holding ``(id, content-height, visible-height)`` panes.

    A pane overflows, and therefore counts as scrollable, only when its content
    is taller than its box by more than the program's own 20px slack.
    """
    boxes = "".join(
        f'<div id="{name}" style="overflow-y:{overflow};height:{visible}px">'
        f'<div style="height:{content}px">{name}</div></div>'
        for name, content, visible in panes
    )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Scroll</title></head>
  <body><main style="overflow-y:hidden;height:200px">{boxes}</main></body>
</html>
"""


async def serve(page: Page, html: str) -> None:
    """Serve ``html`` from a LinkedIn origin.

    The origin matters for the click loop: it reads ``location.href`` and
    matches it against ``/messaging/thread/``, and a ``pushState`` from
    ``about:blank`` is refused by the browser outright.
    """
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    await page.goto(BASE_URL)


async def clicks(page: Page) -> list[str]:
    """The thread ids whose rows were clicked, in the order they were."""
    log = await page.evaluate("() => document.getElementById('log').textContent")
    return log.split()


class TestTheClickLoopAgainstRealDom:
    async def test_a_name_filter_clicks_only_the_row_it_names(self, dom_page):
        """The filter runs before the click, which is the read-marking bound.

        Two rows, one wanted. A loop that clicked first and filtered the
        results afterwards would return the same single ref while having
        marked the other participant's thread read, so the recorded clicks are
        the only assertion that separates the two.
        """
        await serve(
            dom_page,
            sidebar(
                [
                    ("Select conversation with Ada Lovelace", "2-ada"),
                    ("Select conversation with Grace Hopper", "2-grace"),
                ]
            ),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="Grace Hopper"
        )

        assert await clicks(dom_page) == ["2-grace"]
        assert refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-grace/",
                "context": "inbox",
                "text": "Grace Hopper",
            }
        ]

    async def test_the_filter_matches_whole_names_and_not_prefixes(self, dom_page):
        """``Ada Lovelace`` must not select ``Ada Lovelace-Group``.

        A substring match would click a group thread nobody asked about and
        hand its id back as the participant's own.
        """
        await serve(
            dom_page,
            sidebar(
                [
                    ("Select conversation with Ada Lovelace-Group", "2-group"),
                    ("Select conversation with Ada Lovelace", "2-ada"),
                ]
            ),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="Ada Lovelace"
        )

        assert await clicks(dom_page) == ["2-ada"]
        assert [ref["url"] for ref in refs] == ["/messaging/thread/2-ada/"]

    async def test_the_filter_ignores_case_and_collapses_whitespace(self, dom_page):
        """Normalized on both sides, the same way the Python strip is.

        The row's label is what LinkedIn rendered and the filter is what a
        profile page reported; the two disagree on spacing routinely, and a
        raw comparison turns that into "Could not find a conversation".
        """
        await serve(
            dom_page,
            sidebar([("Select conversation with   Ada    Lovelace", "2-ada")]),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox", name_filter="  ada lovelace  "
        )

        assert await clicks(dom_page) == ["2-ada"]
        assert [ref["url"] for ref in refs] == ["/messaging/thread/2-ada/"]

    async def test_without_a_filter_every_row_up_to_the_cap_is_visited(self, dom_page):
        """``limit`` is the click budget, not a slice of the results.

        Each visit marks a thread read, so a cap applied after the loop would
        cost the user exactly the side effect the cap exists to bound.
        """
        await serve(
            dom_page,
            sidebar(
                [
                    ("Select conversation with Ada Lovelace", "2-ada"),
                    ("Select conversation with Grace Hopper", "2-grace"),
                    ("Select conversation with Alan Turing", "2-alan"),
                ]
            ),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=2, context="inbox"
        )

        assert await clicks(dom_page) == ["2-ada", "2-grace"]
        assert [ref["url"] for ref in refs] == [
            "/messaging/thread/2-ada/",
            "/messaging/thread/2-grace/",
        ]

    async def test_a_null_limit_visits_the_whole_sidebar(self, dom_page):
        """``None`` is every row, and it is what the resolver passes.

        Read as a number this would be a cap of zero and the resolver would
        find no thread for anybody.
        """
        await serve(
            dom_page,
            sidebar(
                [
                    ("Select conversation with Ada Lovelace", "2-ada"),
                    ("Select conversation with Grace Hopper", "2-grace"),
                ]
            ),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert await clicks(dom_page) == ["2-ada", "2-grace"]
        assert len(refs) == 2

    async def test_a_row_with_no_click_handler_is_skipped_not_reported(self, dom_page):
        """No handler means no thread id, and a ref without one names nothing."""
        await serve(
            dom_page,
            sidebar(
                [("Select conversation with Ada Lovelace", "2-ada")], clickable=False
            ),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert refs == []
        assert await clicks(dom_page) == []

    async def test_the_aria_label_reaches_python_unmodified(self, dom_page):
        """The locale strip is Python's job, so the browser must not do it.

        A label in a locale the table does not carry has to arrive whole;
        stripping it here would leave Python a name it cannot recognise as
        unstripped.
        """
        await serve(
            dom_page,
            sidebar([("Konversation auswählen mit Ada Lovelace", "2-ada")]),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ada/",
                "context": "inbox",
                "text": "Konversation auswählen mit Ada Lovelace",
            }
        ]

    async def test_a_row_that_never_routes_yields_no_ref(self, dom_page):
        """The loop waits for the SPA URL and gives up rather than guessing.

        Without the ``/messaging/thread/`` match the loop would take whatever
        address the page happened to hold and parse a thread id out of the
        inbox URL itself.
        """
        await serve(
            dom_page,
            sidebar([("Select conversation with Ada Lovelace", "2-ada")], routes=False),
        )

        refs = await _reader(dom_page)._extract_conversation_thread_refs(
            limit=None, context="inbox"
        )

        assert await clicks(dom_page) == ["2-ada"]
        assert refs == []


class TestTheScrollWalkAgainstRealDom:
    async def test_the_tallest_scrollable_region_is_the_one_moved(self, dom_page):
        """Tallest by scroll height, not first in document order.

        LinkedIn's messaging page holds several overflowing panes, and the
        conversation list is the tall one. Taking the first match moves the
        short rail beside it and the list never loads another row.
        """
        await serve(
            dom_page,
            scroll_document(panes=[("short", 400, 100), ("tall", 4000, 100)]),
        )

        await _reader(dom_page)._scroll_main_scrollable_region(
            position="bottom", attempts=1, pause_time=0
        )

        assert (
            await dom_page.evaluate("() => document.getElementById('tall').scrollTop")
            > 0
        )
        assert (
            await dom_page.evaluate("() => document.getElementById('short').scrollTop")
            == 0
        )

    async def test_top_returns_the_region_to_its_start(self, dom_page):
        """A thread loads older messages upward, so ``top`` has to mean zero."""
        await serve(dom_page, scroll_document(panes=[("tall", 4000, 100)]))
        await dom_page.evaluate(
            "() => { document.getElementById('tall').scrollTop = 900; }"
        )

        await _reader(dom_page)._scroll_main_scrollable_region(
            position="top", attempts=1, pause_time=0
        )

        assert (
            await dom_page.evaluate("() => document.getElementById('tall').scrollTop")
            == 0
        )

    async def test_a_pane_that_does_not_overflow_is_not_a_candidate(self, dom_page):
        """``main`` itself is the fallback when nothing inside it scrolls.

        Without the overflow test every div qualifies, and the walk would pick
        whichever one happened to be tallest rather than the one that scrolls.
        """
        await serve(
            dom_page,
            scroll_document(panes=[("flat", 110, 100)], overflow="visible"),
        )

        await _reader(dom_page)._scroll_main_scrollable_region(
            position="bottom", attempts=1, pause_time=0
        )

        assert (
            await dom_page.evaluate("() => document.getElementById('flat').scrollTop")
            == 0
        )

    async def test_a_document_without_main_is_left_alone(self, dom_page):
        """No ``main`` is a page that has not rendered, not an error."""
        await serve(
            dom_page,
            "<!DOCTYPE html><html><body><div style='height:9000px'>x</div></body></html>",
        )

        await _reader(dom_page)._scroll_main_scrollable_region(
            position="bottom", attempts=2, pause_time=0
        )

        assert await dom_page.evaluate("() => window.scrollY") == 0


class TestTheMainTextWaitAgainstRealDom:
    async def test_the_wait_returns_once_main_is_long_enough(self, dom_page):
        """Measured against ``main``'s own innerText, not the document's.

        The chrome around ``main`` is long enough to clear any threshold on
        its own, so reading the body would return before the page rendered
        anything the caller asked for.
        """
        filler = "outside " * 50
        await serve(
            dom_page,
            f"<!DOCTYPE html><html><body><p>{filler}</p>"
            "<main id='m'>short</main>"
            "<script>setTimeout(() => {"
            "document.getElementById('m').textContent = 'x'.repeat(200);"
            "}, 150);</script></body></html>",
        )

        await _reader(dom_page)._wait_for_main_text(
            minimum_length=100, timeout=5000, log_context="Messaging inbox"
        )

        assert (
            await dom_page.evaluate("() => document.querySelector('main').innerText")
        ).startswith("xxx")

    async def test_a_main_that_never_fills_times_out_without_raising(
        self, dom_page, caplog
    ):
        """The caller reads whatever is there rather than failing the call."""
        await serve(
            dom_page,
            "<!DOCTYPE html><html><body><main>short</main></body></html>",
        )

        with caplog.at_level(
            "DEBUG", logger="linkedin_mcp_server.scraping.conversations"
        ):
            await _reader(dom_page)._wait_for_main_text(
                minimum_length=100, timeout=300, log_context="Messaging inbox"
            )

        assert "Messaging inbox content did not appear" in caplog.text
