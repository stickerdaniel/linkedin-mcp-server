"""Browser-DOM tests for the messaging sidebar programs.

The unit suite mocks ``page.evaluate``, so the three programs in
``scraping/conversations.py`` never execute there: the click-to-capture loop,
the scrollable-region walk and the main-text wait are all asserted as call
arguments and nothing else. These cases run them in headless chromium.

The click loop is the reason this file exists. Selecting a conversation row
marks the thread read on LinkedIn, so the order of the name filter and the
click is the closest thing to a write anywhere in the scraping package, and
that ordering lives entirely inside the JavaScript. A mocked ``evaluate``
cannot tell a loop that filters first from one that clicks first. The same
holds for thread ownership: whether a row is credited with the thread its own
click opened, or with one that was already open or that an earlier click
opened late, is decided entirely inside the loop.

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

import json

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
COMPOSE_URL = "https://www.linkedin.com/messaging/compose/"
ORIGIN = "https://www.linkedin.com"


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
    rows: list[tuple[str, str]],
    *,
    clickable: bool = True,
    routes: bool = True,
    unclickable: set[str] | None = None,
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

    ``clickable=False`` drops every inner div, while ``unclickable`` can drop
    individual rows, standing in for a row LinkedIn rendered without a handler.
    ``routes=False`` keeps the handler and drops only the ``pushState``, standing
    in for a row whose click never reaches the thread route. Both are built here
    rather than patched in afterwards,
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
            if clickable and thread_id not in (unclickable or set())
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


def delayed_thread_route_sidebar() -> str:
    """One row whose click visits a non-thread route before its thread route."""
    return """<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <p id="log"></p>
    <main><ul><li><label aria-label="Select conversation with Ada Lovelace">
      <div class="msg-conversation-listitem__link" onclick="
        document.getElementById('log').textContent += ' 2-ada';
        history.pushState({}, '', '/messaging/loading/');
        setTimeout(() => history.pushState(
          {}, '', '/messaging/thread/2-ada/'
        ), 250);
      "><span>Select conversation with Ada Lovelace</span></div>
    </label></li></ul></main>
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


_PHASED_SIDEBAR = """<!DOCTYPE html>
<html lang="en">
  <head><meta charset="utf-8"><title>Messaging</title></head>
  <body>
    <p id="events">[]</p>
    <main><ul id="rows"></ul></main>
    <script>
      const rows = __ROWS__;
      const pending = new Map();
      const record = entry => {
        const node = document.getElementById('events');
        node.textContent = JSON.stringify(
          [...JSON.parse(node.textContent), entry]
        );
      };
      const land = id => {
        if (!pending.has(id)) {
          record({ orphan: id });
          return;
        }
        const dest = pending.get(id);
        pending.delete(id);
        history.pushState({}, '', dest);
        record({ land: id, path: location.pathname });
      };
      for (const row of rows) {
        const li = document.createElement('li');
        const label = document.createElement('label');
        label.setAttribute('aria-label', row.label);
        if (row.mode !== 'missing') {
          const target = document.createElement('div');
          target.className = 'msg-conversation-listitem__link';
          target.textContent = row.label;
          target.addEventListener('click', () => {
            record({ click: row.id, pre: location.pathname });
            const dest = row.dest ?? `/messaging/thread/${row.id}/`;
            if (row.mode === 'noop' && location.pathname === dest) {
              record({ noop: row.id });
            } else if (row.mode === 'route' || row.mode === 'noop') {
              history.pushState({}, '', dest);
              record({ land: row.id, path: location.pathname });
            } else if (row.mode === 'late' || row.mode === 'loading') {
              pending.set(row.id, dest);
              record({ dispatch: row.id, dest });
              if (row.mode === 'loading') {
                history.pushState({}, '', '/messaging/loading/');
              }
            }
            if (row.releases) land(row.releases);
          });
          label.append(target);
        }
        if (row.mode === 'late' || row.mode === 'loading') {
          document.addEventListener(
            `fixture-release-${row.id}`, () => land(row.id)
          );
        }
        li.append(label);
        document.getElementById('rows').append(li);
      }
    </script>
  </body>
</html>
"""


def row(
    row_id: str,
    mode: str = "route",
    *,
    name: str | None = None,
    label: str | None = None,
    dest: str | None = None,
    releases: str | None = None,
) -> dict[str, Any]:
    """One row of ``phased_sidebar``.

    ``mode`` is what the row's click does, all in the page's own world:

    - ``route``: moves to ``dest`` (its own thread by default) at once.
    - ``none``: a handler that never navigates.
    - ``noop``: navigates only when ``dest`` is not already the open path, the
      way a click on the already-open conversation leaves the URL alone.
    - ``late``: dispatches a navigation that lands only when released.
    - ``loading``: moves to a non-thread route at once, then lands on ``dest``
      only when released.
    - ``missing``: no click target at all.

    ``releases`` names another row's pending landing that this row's click
    completes. A release keyed on a click the correct loop never makes stays
    unreleased, which is how a forbidden continuation becomes observable.
    """
    spec: dict[str, Any] = {
        "id": row_id,
        "mode": mode,
        "label": label
        if label is not None
        else f"Select conversation with {name or row_id}",
    }
    if dest is not None:
        spec["dest"] = dest
    if releases is not None:
        spec["releases"] = releases
    return spec


def phased_sidebar(rows: list[dict[str, Any]]) -> str:
    """A sidebar whose rows navigate, stall, or land only when released.

    Every click, dispatched navigation, landing, no-op and orphaned release is
    appended to ``#events`` as JSON. A release of a landing nobody dispatched
    is recorded as an orphan rather than inventing a navigation, so a test can
    tell a release that fired from one that had nothing to release.
    """
    return _PHASED_SIDEBAR.replace("__ROWS__", json.dumps(rows))


async def install_poll_control(
    page: Page,
    *,
    release: tuple[str, int] | None = None,
    accelerate: bool = False,
) -> None:
    """Wrap the scan's poll timer inside Patchright's isolated world.

    Every requested delay is recorded. ``release=(row, n)`` completes that
    row's pending landing immediately before its ``n``-th poll callback runs,
    so observations ``1`` to ``n - 1`` are known to have seen the pre-landing
    path and observation ``n`` sees the landing. Counting callbacks rather
    than timer requests matters: a request is recorded before its callback
    and before the observation that follows it, so a count of requests
    releases one observation early.

    ``accelerate`` fires each callback without waiting; the requested delay is
    still recorded. Only the timer is replaced, so the production loop still
    executes in the browser.
    """
    await page.evaluate(
        """({ release, accelerate }) => {
            globalThis.linkedinMcpPollDelays = [];
            const nativeSetTimeout = globalThis.setTimeout;
            let current = null;
            let count = 0;
            globalThis.setTimeout = (callback, delay, ...args) => {
                const events = JSON.parse(
                    document.getElementById('events').textContent
                );
                const clicked = events.filter(e => 'click' in e).at(-1)?.click
                    ?? null;
                if (clicked !== current) {
                    current = clicked;
                    count = 0;
                }
                count += 1;
                const rowId = current;
                const observation = count;
                globalThis.linkedinMcpPollDelays.push(delay);
                return nativeSetTimeout(() => {
                    if (release && rowId === release[0]
                        && observation === release[1]) {
                        document.dispatchEvent(
                            new Event(`fixture-release-${release[0]}`)
                        );
                    }
                    callback(...args);
                }, accelerate ? 0 : delay);
            };
        }""",
        {"release": list(release) if release else None, "accelerate": accelerate},
    )


async def serve(page: Page, html: str, *, start: str = BASE_URL) -> None:
    """Serve ``html`` from a LinkedIn origin, starting at ``start``.

    The origin matters for the click loop: it reads ``location.pathname`` and
    matches it against the thread route, and a ``pushState`` from
    ``about:blank`` is refused by the browser outright. Every LinkedIn request
    is answered by the fixture; nothing reaches the network.
    """
    await page.route(
        "https://www.linkedin.com/**",
        lambda route: route.fulfill(content_type="text/html", body=html),
    )
    await page.goto(start)


async def clicks(page: Page) -> list[str]:
    """The thread ids whose rows were clicked, in the order they were."""
    log = await page.evaluate("() => document.getElementById('log').textContent")
    return log.split()


async def events(page: Page) -> list[dict[str, Any]]:
    """The ``phased_sidebar`` event log."""
    return json.loads(
        await page.evaluate("() => document.getElementById('events').textContent")
    )


async def clicked(page: Page) -> list[str]:
    """The ``phased_sidebar`` row ids clicked, in order."""
    return [entry["click"] for entry in await events(page) if "click" in entry]


async def poll_delays(page: Page) -> list[int]:
    return await page.evaluate("() => globalThis.linkedinMcpPollDelays")


async def scan(
    page: Page,
    *,
    limit: int | None = None,
    context: str = "inbox",
    name_filter: str | None = None,
) -> Any:
    return await _reader(page)._extract_conversation_thread_refs(
        limit=limit, context=context, name_filter=name_filter
    )


def attributed(outcome: Any) -> list[tuple[str | None, str]]:
    """``(participant, thread id)`` for every click-derived reference."""
    return [
        (ref.get("text"), ref["url"].removeprefix("/messaging/thread/").rstrip("/"))
        for ref in outcome.refs
    ]


def stop_of(outcome: Any) -> tuple[str, int] | None:
    stop = outcome.stopped_at
    return None if stop is None else (stop.aria_label, stop.position)


def gap_of(outcome: Any) -> tuple[str, int, int] | None:
    gap = outcome.first_index_gap
    return None if gap is None else (gap.aria_label, gap.position, gap.preceded_by)


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

        outcome = await scan(dom_page, name_filter="Grace Hopper")

        assert await clicks(dom_page) == ["2-grace"]
        assert outcome.refs == [
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

        outcome = await scan(dom_page, name_filter="Ada Lovelace")

        assert await clicks(dom_page) == ["2-ada"]
        assert [ref["url"] for ref in outcome.refs] == ["/messaging/thread/2-ada/"]

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

        outcome = await scan(dom_page, name_filter="  ada lovelace  ")

        assert await clicks(dom_page) == ["2-ada"]
        assert [ref["url"] for ref in outcome.refs] == ["/messaging/thread/2-ada/"]

    async def test_the_filter_itself_is_not_stripped_of_the_ui_verb(self, dom_page):
        """Only the row label loses its en-US verb; the filter is a name.

        A display name that happens to read ``Select conversation with Bob``
        must match the row LinkedIn labels with the verb in front of exactly
        that name. Stripping the verb from the filter too would look for
        ``Bob`` and click nothing.
        """
        await serve(
            dom_page,
            phased_sidebar(
                [
                    row(
                        "B",
                        label=("Select conversation with Select conversation with Bob"),
                    )
                ]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page, name_filter="Select conversation with Bob")

        assert await clicked(dom_page) == ["B"]
        assert attributed(outcome) == [("Select conversation with Bob", "B")]
        assert stop_of(outcome) is None

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

        outcome = await scan(dom_page, limit=2)

        assert await clicks(dom_page) == ["2-ada", "2-grace"]
        assert [ref["url"] for ref in outcome.refs] == [
            "/messaging/thread/2-ada/",
            "/messaging/thread/2-grace/",
        ]

    async def test_the_cap_counts_considered_labels_not_attributed_rows(self, dom_page):
        """An early label without a click target still spends the cap.

        Counting only attributed rows would click one row further than the
        caller's budget every time an early row was skipped.
        """
        await serve(
            dom_page,
            phased_sidebar([row("G", "missing"), row("A"), row("B")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page, limit=2)

        assert await clicked(dom_page) == ["A"]
        assert attributed(outcome) == [("A", "A")]

    async def test_the_cap_counts_labels_the_name_filter_passes_over(self, dom_page):
        """Under a filter, a label for someone else still spends the cap.

        Alice takes the one considered position, so Bob lies outside the
        window. Counting only matching labels would click Bob and mark his
        thread read beyond the caller's budget.
        """
        await serve(
            dom_page,
            phased_sidebar([row("A", name="Alice"), row("B", name="Bob")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page, limit=1, name_filter="Bob")

        assert await clicked(dom_page) == []
        assert outcome.refs == []
        assert stop_of(outcome) is None
        assert gap_of(outcome) is None

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

        outcome = await scan(dom_page)

        assert await clicks(dom_page) == ["2-ada", "2-grace"]
        assert len(outcome.refs) == 2

    async def test_a_row_with_no_click_handler_is_skipped_not_reported(self, dom_page):
        """No handler means no thread id, and a ref without one names nothing."""
        await serve(
            dom_page,
            sidebar(
                [("Select conversation with Ada Lovelace", "2-ada")], clickable=False
            ),
        )

        outcome = await scan(dom_page)

        assert outcome.refs == []
        assert await clicks(dom_page) == []

    async def test_an_unclickable_row_cannot_reuse_the_previous_thread(self, dom_page):
        """A later row without a handler is omitted after a successful click.

        The page still carries the prior thread URL, so proceeding past the
        missing click target could report that thread again under the later
        row's participant label.
        """
        await serve(
            dom_page,
            sidebar(
                [
                    ("Select conversation with Ada Lovelace", "2-ada"),
                    ("Select conversation with Grace Hopper", "2-grace"),
                ],
                unclickable={"2-grace"},
            ),
        )

        outcome = await scan(dom_page)

        assert await clicks(dom_page) == ["2-ada"]
        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ada/",
                "context": "inbox",
                "text": "Ada Lovelace",
            }
        ]

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

        outcome = await scan(dom_page)

        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ada/",
                "context": "inbox",
                "text": "Konversation auswählen mit Ada Lovelace",
            }
        ]

    async def test_a_non_thread_transition_waits_for_the_thread_route(self, dom_page):
        """The first changed URL is not necessarily the settled SPA route.

        An ordinary wall-clock control: the landing is a page timer, not a
        phase this test controls. The callback-controlled cases below pin
        which observation sees which path.
        """
        await serve(dom_page, delayed_thread_route_sidebar())

        outcome = await scan(dom_page)

        assert await clicks(dom_page) == ["2-ada"]
        assert outcome.refs == [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ada/",
                "context": "inbox",
                "text": "Ada Lovelace",
            }
        ]


class TestThreadOwnershipAgainstRealDom:
    """A row earns a thread id only when its own click moves the pathname.

    The acceptance rule is an observed thread id, read from the pathname,
    that differs from the id the pathname carried immediately before this
    row's click. The first click that does not produce one stops the scan:
    no later row is clicked, because a late landing of the unresolved click
    would otherwise be credited to whichever row was being polled.
    """

    async def test_compose_rows_that_all_route_are_all_attributed(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A"), row("B"), row("C")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "A"), ("B", "B"), ("C", "C")]
        assert stop_of(outcome) is None
        assert outcome.start_thread_id is None
        assert await clicked(dom_page) == ["A", "B", "C"]

    async def test_a_first_click_that_stalls_stops_the_scan(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A", "none"), row("B"), row("C")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert await clicked(dom_page) == ["A"]
        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)

    async def test_original_1_a_stalled_middle_row_keeps_the_verified_prefix(
        self, dom_page
    ):
        """#1093 case 1: the stalled row used to inherit the previous thread."""
        await serve(
            dom_page,
            phased_sidebar(
                [row("A", name="Alice"), row("B", "none", name="Bob"), row("C")]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("Alice", "A")]
        assert stop_of(outcome) == ("Select conversation with Bob", 1)
        assert await clicked(dom_page) == ["A", "B"]

    async def test_original_2_a_stalled_row_never_inherits_the_open_thread(
        self, dom_page
    ):
        """#1093 case 2: a filtered scan that starts on someone else's thread."""
        await serve(
            dom_page,
            phased_sidebar(
                [row("A", "noop", name="Alice"), row("Bob", "none", name="Bob")]
            ),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page, name_filter="Bob")

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with Bob", 1)
        assert outcome.start_thread_id == "A"
        assert await clicked(dom_page) == ["Bob"]

    async def test_original_3_a_thread_start_does_not_refuse_a_real_move(
        self, dom_page
    ):
        """#1093 case 3: starting on a thread is context, not a refusal."""
        await serve(
            dom_page,
            phased_sidebar([row("Bob", name="Bob", dest="/messaging/thread/B/")]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page, name_filter="Bob")

        assert attributed(outcome) == [("Bob", "B")]
        assert stop_of(outcome) is None
        assert outcome.start_thread_id == "A"

    async def test_original_4_a_late_landing_is_never_credited_to_a_later_row(
        self, dom_page
    ):
        """#1093 case 4: B lands only if C is clicked, and C must not be.

        The release is keyed on C's click, so a loop that went on past the
        unresolved B would click C, B's navigation would land during C's
        polls, and C would be handed B's thread. B's dispatch is asserted so
        the stop is known to face a navigation still pending, not a click
        that did nothing.
        """
        await serve(
            dom_page,
            phased_sidebar(
                [
                    row("A", name="Alice"),
                    row("B", "late", name="Bob"),
                    row("C", "none", name="Carol", releases="B"),
                ]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("Alice", "A")]
        assert stop_of(outcome) == ("Select conversation with Bob", 1)
        assert await clicked(dom_page) == ["A", "B"]
        log = await events(dom_page)
        assert {"dispatch": "B", "dest": "/messaging/thread/B/"} in log
        assert not any(entry.get("land") == "B" for entry in log)
        assert not any("orphan" in entry for entry in log)

    async def test_a_delayed_first_row_stops_before_any_later_click(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A", "late"), row("B", releases="A"), row("C")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)
        assert await clicked(dom_page) == ["A"]
        log = await events(dom_page)
        assert {"dispatch": "A", "dest": "/messaging/thread/A/"} in log
        assert not any(entry.get("land") == "A" for entry in log)
        assert not any("orphan" in entry for entry in log)

    async def test_the_auto_opened_row_clicked_first_is_not_credited(self, dom_page):
        """Bare ``/messaging/`` opens a thread; its row's click moves nothing."""
        await serve(
            dom_page,
            phased_sidebar([row("A", "noop"), row("B")]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)
        assert outcome.start_thread_id == "A"
        assert await clicked(dom_page) == ["A"]
        assert {"noop": "A"} in await events(dom_page)

    async def test_the_initially_open_row_is_credited_once_its_click_moves(
        self, dom_page
    ):
        """The comparison is against each row's own pre-click id.

        After B moves the page to B, a click on A really opens A, and a rule
        that remembered the scan's starting thread would refuse it.
        """
        await serve(
            dom_page,
            phased_sidebar([row("B"), row("A", "noop")]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("B", "B"), ("A", "A")]
        assert stop_of(outcome) is None
        assert await clicked(dom_page) == ["B", "A"]

    @pytest.mark.parametrize(
        "dest",
        ["/messaging/thread/A/?x=1", "/messaging/thread/A/#m"],
        ids=["query", "hash"],
    )
    async def test_a_query_or_hash_change_on_the_same_thread_is_not_a_move(
        self, dom_page, dest
    ):
        await serve(
            dom_page,
            phased_sidebar([row("A", dest=dest)]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)

    async def test_a_thread_marker_in_the_query_is_not_a_thread(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar(
                [row("A", dest="/messaging/compose/?next=/messaging/thread/X/")]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)

    async def test_a_real_thread_with_a_query_or_hash_is_accepted(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar(
                [
                    row("A", dest="/messaging/thread/B/?ref=1"),
                    row("B", dest="/messaging/thread/C/#latest"),
                ]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "B"), ("B", "C")]
        assert stop_of(outcome) is None

    async def test_a_trailing_slash_spelling_is_the_same_thread(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A", dest="/messaging/thread/A")]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)

    async def test_a_loading_route_is_waited_through_to_the_thread(self, dom_page):
        """Observation 1 sees ``/messaging/loading/``; observation 2 sees B.

        Native 100 ms timers; the landing is released immediately before the
        second callback, so the first observation is known to have failed.
        """
        await serve(
            dom_page,
            phased_sidebar([row("A", "loading", dest="/messaging/thread/B/")]),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, release=("A", 2))

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "B")]
        assert stop_of(outcome) is None
        assert await poll_delays(dom_page) == [100, 100]
        assert await events(dom_page) == [
            {"click": "A", "pre": "/messaging/compose/"},
            {"dispatch": "A", "dest": "/messaging/thread/B/"},
            {"land": "A", "path": "/messaging/thread/B/"},
        ]

    async def test_a_loading_route_that_never_lands_is_a_stop(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A", "loading", dest="/messaging/thread/B/")]),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, accelerate=True)

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)
        assert await poll_delays(dom_page) == [100] * 12

    async def test_loading_back_to_the_same_thread_is_not_a_move(self, dom_page):
        """A → loading → A: the route changed twice and the thread did not."""
        await serve(
            dom_page,
            phased_sidebar([row("A", "loading")]),
            start=f"{ORIGIN}/messaging/thread/A/",
        )
        await install_poll_control(dom_page, release=("A", 2), accelerate=True)

        outcome = await scan(dom_page)

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with A", 0)
        assert await poll_delays(dom_page) == [100] * 12
        assert {"land": "A", "path": "/messaging/thread/A/"} in await events(dom_page)

    async def test_a_slow_later_row_gets_its_own_full_poll_budget(self, dom_page):
        """B lands before its second callback, after A already succeeded.

        Native timers. Four requested waits: one for A, two for B, one for C.
        """
        await serve(
            dom_page,
            phased_sidebar(
                [row("A"), row("B", "loading", dest="/messaging/thread/B/"), row("C")]
            ),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, release=("B", 2))

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "A"), ("B", "B"), ("C", "C")]
        assert stop_of(outcome) is None
        assert await clicked(dom_page) == ["A", "B", "C"]
        assert await poll_delays(dom_page) == [100] * 4
        assert await events(dom_page) == [
            {"click": "A", "pre": "/messaging/compose/"},
            {"land": "A", "path": "/messaging/thread/A/"},
            {"click": "B", "pre": "/messaging/thread/A/"},
            {"dispatch": "B", "dest": "/messaging/thread/B/"},
            {"land": "B", "path": "/messaging/thread/B/"},
            {"click": "C", "pre": "/messaging/thread/B/"},
            {"land": "C", "path": "/messaging/thread/C/"},
        ]

    async def test_a_later_row_that_never_lands_stops_before_the_next(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A"), row("B", "late"), row("C")]),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, accelerate=True)

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "A")]
        assert stop_of(outcome) == ("Select conversation with B", 1)
        assert await clicked(dom_page) == ["A", "B"]

    async def test_an_unfiltered_missing_target_is_skipped_without_a_gap(
        self, dom_page
    ):
        """Listings skip a row without a click target; only indices need gaps."""
        await serve(
            dom_page,
            phased_sidebar([row("A"), row("G", "missing"), row("C")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "A"), ("C", "C")]
        assert stop_of(outcome) is None
        assert gap_of(outcome) is None
        assert await clicked(dom_page) == ["A", "C"]

    async def test_a_filtered_missing_target_records_the_first_index_gap(
        self, dom_page
    ):
        """Position counts considered labels; ``preceded_by`` counts matches.

        The leading row belongs to someone else, so the two numbers differ and
        a count taken from the position would move the index cut.
        """
        await serve(
            dom_page,
            phased_sidebar(
                [
                    row("X", name="Xavier"),
                    row("T", name="Tess"),
                    row("G", "missing", name="Tess"),
                    row("T2", name="Tess"),
                ]
            ),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page, name_filter="Tess")

        assert attributed(outcome) == [("Tess", "T"), ("Tess", "T2")]
        assert gap_of(outcome) == ("Select conversation with Tess", 2, 1)
        assert stop_of(outcome) is None
        assert await clicked(dom_page) == ["T", "T2"]

    async def test_a_gap_before_a_stall_keeps_both_in_source_order(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar(
                [row("G", "missing", name="Tess"), row("S", "none", name="Tess")]
            ),
            start=f"{ORIGIN}/messaging/thread/Q/",
        )

        outcome = await scan(dom_page, name_filter="Tess")

        assert attributed(outcome) == []
        assert gap_of(outcome) == ("Select conversation with Tess", 0, 0)
        assert stop_of(outcome) == ("Select conversation with Tess", 1)

    async def test_a_stall_before_a_gap_never_visits_the_gap(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar(
                [row("S", "none", name="Tess"), row("G", "missing", name="Tess")]
            ),
            start=f"{ORIGIN}/messaging/thread/Q/",
        )

        outcome = await scan(dom_page, name_filter="Tess")

        assert attributed(outcome) == []
        assert stop_of(outcome) == ("Select conversation with Tess", 0)
        assert gap_of(outcome) is None
        assert await clicked(dom_page) == ["S"]

    async def test_a_stall_on_a_row_with_an_empty_label_is_still_a_stop(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("E", "none", label="")]),
            start=COMPOSE_URL,
        )

        outcome = await scan(dom_page)

        assert outcome.stopped_at is not None
        assert stop_of(outcome) == ("", 0)

    async def test_a_search_page_scan_follows_the_same_rule(self, dom_page):
        await serve(
            dom_page,
            phased_sidebar([row("A"), row("B", "none"), row("C")]),
            start=f"{ORIGIN}/messaging/?searchTerm=ada",
        )

        outcome = await scan(dom_page, context="search_results")

        assert attributed(outcome) == [("A", "A")]
        assert outcome.refs[0]["context"] == "search_results"
        assert stop_of(outcome) == ("Select conversation with B", 1)
        assert outcome.start_thread_id is None
        assert await clicked(dom_page) == ["A", "B"]

    async def test_a_row_that_never_routes_uses_the_exact_poll_budget(self, dom_page):
        """A stalled route gets exactly twelve 100 ms chances, then a stop.

        The timer is replaced inside Patchright's isolated evaluation world so
        the browser still executes the production loop without making this test
        spend the full 1.2 seconds.
        """
        await serve(
            dom_page,
            phased_sidebar([row("A", "none")]),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, accelerate=True)

        outcome = await scan(dom_page)

        assert await clicked(dom_page) == ["A"]
        assert outcome.refs == []
        assert await poll_delays(dom_page) == [100] * 12
        assert stop_of(outcome) == ("Select conversation with A", 0)

    async def test_a_landing_at_the_twelfth_observation_is_accepted(self, dom_page):
        """Released immediately before callback 12, not on the 11th request."""
        await serve(
            dom_page,
            phased_sidebar([row("A", "late", dest="/messaging/thread/B/")]),
            start=COMPOSE_URL,
        )
        await install_poll_control(dom_page, release=("A", 12), accelerate=True)

        outcome = await scan(dom_page)

        assert attributed(outcome) == [("A", "B")]
        assert stop_of(outcome) is None
        assert await poll_delays(dom_page) == [100] * 12
        assert await events(dom_page) == [
            {"click": "A", "pre": "/messaging/compose/"},
            {"dispatch": "A", "dest": "/messaging/thread/B/"},
            {"land": "A", "path": "/messaging/thread/B/"},
        ]


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
