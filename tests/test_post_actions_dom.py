# tests/test_post_actions_dom.py
"""Browser-DOM tests for the post-engagement programs.

The unit suite mocks ``page.evaluate``, so the programs in
``scraping/post_actions.py`` never execute there. These run the real ones
against synthetic HTML in headless chromium.

What these fixtures are and are not, stated plainly because AGENTS.md draws
the line: they drive synthetic containers, so every assertion here is a claim
about *this algorithm* and none of them is a claim about LinkedIn's markup.
They prove that the root-post search, the bar walk, the flyout counting and
the insert/submit path behave as specified against a DOM built to the shape
those programs assume. Whether LinkedIn still renders that shape is a
different question, and only a live check answers it.

Every fixture is built from one set of templates and four sets of words:
English, German, opaque tokens carrying no verb, and present-but-empty ARIA
values. The structure is identical across all four and each case asserts the
*same* answer for all of them in one assertion naming the label sets. A
decision that differs between two of them read a word, which the Scraping
Rules forbid; the opaque set is the control, since a label with no verb in it
cannot be matched by one, and the empty set separates attribute *presence*
from attribute truthiness.

The adversarial fixtures are the load-bearing ones. A post page renders its
own comments, each with a reaction toggle of its own and a URN that embeds the
post's id, so the cases that must refuse — a comment mistaken for the post, a
post appearing twice, a bar without a repost opener — are what keep a public
write off the wrong target.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.post_actions import (
    CLICK_REACT_TOGGLE_JS,
    CLICK_REACTION_JS,
    CLICK_REPOST_MENU_ITEM_JS,
    CLEAR_EDITOR_JS,
    COUNT_TEXT_UNITS_JS,
    OWN_EDITOR_JS,
    PIN_EDITOR_JS,
    PIN_VISIBLE_DIALOG_JS,
    OPEN_REPOST_MENU_JS,
    PIN_POST_ROOT_JS,
    POST_ACTION_SIGNALS_JS,
    READ_REACTION_FLYOUT_JS,
    READ_REPOST_MENU_JS,
    SUBMIT_EDITOR_JS,
)

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

POST_ID = "7506667649444237313"
POST_URN = f"urn:li:ugcPost:{POST_ID}"
SHARE_URL = f"https://www.linkedin.com/posts/member_topic-share-{POST_ID}-suffix/"
OTHER_URN = "urn:li:ugcPost:7000000000000000001"
ACTIVITY_URN = "urn:li:activity:7506667700000000000"
MISMATCH_ACTIVITY_URN = "urn:li:activity:7506667799999999999"
# A comment on the post. Its URN embeds the post's own id, which is why the
# root search anchors on the end of the value instead of searching for it.
COMMENT_URN = f"urn:li:comment:({POST_URN},4455667788)"
SOCIAL_URN = (
    f"urn:li:fsd_socialDetail:({POST_URN},{POST_URN},urn:li:highlightedReply:-)"
)


@dataclass(frozen=True, slots=True)
class Labels:
    """Every visible string and aria-label value one label set contributes.

    Only these change between the four sets, so nothing else can explain an
    answer that changes with them.
    """

    locale: str
    react: str
    comment: str
    repost: str
    send: str
    like: str
    celebrate: str
    support: str
    love: str
    insightful: str
    funny: str
    repost_now: str
    repost_thoughts: str
    submit: str
    reply: str
    emoji: str
    photo: str


ENGLISH = Labels(
    locale="en",
    react="Like Varun's post",
    comment="Comment on Varun's post",
    repost="Repost Varun's post",
    send="Send Varun's post in a message",
    like="Like",
    celebrate="Celebrate",
    support="Support",
    love="Love",
    insightful="Insightful",
    funny="Funny",
    repost_now="Repost",
    repost_thoughts="Repost with your thoughts",
    submit="Post comment",
    reply="Reply to this comment",
    emoji="Open Emoji Keyboard",
    photo="Add a photo",
)

GERMAN = Labels(
    locale="de",
    react="Beitrag von Varun mit Gefällt mir markieren",
    comment="Beitrag von Varun kommentieren",
    repost="Beitrag von Varun teilen",
    send="Beitrag von Varun als Nachricht senden",
    like="Gefällt mir",
    celebrate="Glückwunsch",
    support="Unterstützung",
    love="Liebe",
    insightful="Interessant",
    funny="Lustig",
    repost_now="Direkt teilen",
    repost_thoughts="Mit eigenem Beitrag teilen",
    submit="Kommentar veröffentlichen",
    reply="Auf diesen Kommentar antworten",
    emoji="Emoji-Tastatur öffnen",
    photo="Foto hinzufügen",
)

# No verb anywhere, in any language: these are identifiers. Whatever still
# works here is reading structure, and this set is the one a locale table
# could not rescue.
OPAQUE = Labels(
    locale="opaque",
    react="a7f3c1",
    comment="b2e9d4",
    repost="c8a0b5",
    send="d1f6e2",
    like="e4b7a9",
    celebrate="f0c3d8",
    support="a9e2f7",
    love="b5d8c0",
    insightful="c2f4a6",
    funny="d7b1e3",
    repost_now="e9a5b8",
    repost_thoughts="f3c7d1",
    submit="a0b4e6",
    reply="b8f2c9",
    emoji="c1d5f8",
    photo="d3e7a2",
)

# Attribute presence and attribute truthiness are different contracts. Every
# aria-label attribute stays in the markup with an empty value, so
# `hasAttribute` survives and `getAttribute(...)` truthiness does not.
EMPTY_ARIA = Labels(
    locale="empty-aria",
    react="",
    comment="",
    repost="",
    send="",
    like="",
    celebrate="",
    support="",
    love="",
    insightful="",
    funny="",
    repost_now="",
    repost_thoughts="",
    submit="",
    reply="",
    emoji="",
    photo="",
)

LOCALES = (ENGLISH, GERMAN, OPAQUE, EMPTY_ARIA)

Build = Callable[[Labels], str]


def action_bar(
    labels: Labels,
    *,
    pressed: str = "false",
    opener: bool = True,
    click_id: str = "post",
) -> str:
    """The post's own social bar: a reaction toggle plus three more controls.

    The toggle carries ``aria-pressed`` and the repost control carries
    ``aria-expanded``; neither is identified by its label anywhere.
    """
    repost = (
        f'<button type="button" aria-expanded="false" aria-label="{labels.repost}"'
        f" onclick=\"this.setAttribute('aria-expanded','true');"
        f" document.body.setAttribute('data-clicked','{click_id}-repost')\""
        f">{labels.repost_now}</button>"
        if opener
        else f'<button type="button" aria-label="{labels.repost}">'
        f"{labels.repost_now}</button>"
    )
    return f"""
  <div class="social-bar">
    <button type="button" aria-pressed="{pressed}" aria-label="{labels.react}"
      onclick="document.body.setAttribute('data-clicked','{click_id}-react')"
      >{labels.like}</button>
    <button type="button" aria-label="{labels.comment}"
      onclick="document.body.setAttribute('data-clicked','{click_id}-comment')"
      >{labels.comment}</button>
    {repost}
    <button type="button" aria-label="{labels.send}">{labels.send}</button>
  </div>
"""


def comment_thread(labels: Labels) -> str:
    """Two comments, each with a reaction toggle and a URN naming the post.

    This is the fixture the root search exists for. Both URNs contain the
    post's id, and both rows carry ``aria-pressed``, so a substring match or a
    first-toggle-anywhere walk lands here.
    """
    rows = ""
    for index in (1, 2):
        rows += f"""
    <article data-id="urn:li:comment:({POST_URN},44556677{index}8)">
      <p>Existing comment {index}</p>
      <div class="comment-actions">
        <button type="button" aria-pressed="false" aria-label="{labels.react}"
          onclick="document.body.setAttribute('data-clicked','comment-react')"
          >{labels.like}</button>
        <button type="button" aria-label="{labels.reply}">{labels.reply}</button>
      </div>
    </article>
"""
    return f'<section class="comments">{rows}</section>'


def comment_editor(
    labels: Labels, *, draft: str = "", submit: bool = True, detours: bool = True
) -> str:
    """The comment box, with the controls LinkedIn renders beside the editor.

    ``detours`` are the emoji trigger and the photo attachment, and they are
    here because omitting them is what let a wrong rule look right: the emoji
    one is dropped for its ``aria-expanded`` and the photo one is then the only
    enabled button left in the form. ``submit=False`` is the state a live
    comment box is actually in before it has text — the submit control does not
    exist yet — which is the state where that lone photo button was clicked.
    """
    extras = (
        f"""
    <button type="button" aria-expanded="false" aria-label="{labels.emoji}"
      onclick="document.body.setAttribute('data-clicked','comment-emoji')"
      >{labels.emoji}</button>
    <button type="button" aria-label="{labels.photo}"
      onclick="document.body.setAttribute('data-clicked','comment-photo')"
      ></button>
"""
        if detours
        else ""
    )
    submit_control = (
        f"""
    <button type="submit" aria-label="{labels.submit}"
      onclick="document.body.setAttribute('data-clicked','comment-submit');
               return false;"
      >{labels.submit}</button>
"""
        if submit
        else ""
    )
    return f"""
  <form class="comment-form">
    <div role="textbox" contenteditable="true"
      aria-label="{labels.comment}">{draft}</div>
    {extras}
    {submit_control}
  </form>
"""


def sdui_comment_editor(labels: Labels) -> str:
    """Three icon controls before typing; one text control appears afterward."""
    return f"""
  <div class="comment-composer">
    <div role="textbox" contenteditable="true" aria-label="{labels.comment}"
      oninput="
        const controls = this.parentElement.querySelector('.composer-controls');
        if (!controls.querySelector('[data-generated-submit]')) {{
          const submit = document.createElement('button');
          submit.type = 'button';
          submit.setAttribute('data-generated-submit', '');
          submit.innerHTML = '<span>{labels.submit}</span>';
          submit.onclick = () => document.body.setAttribute(
            'data-clicked', 'sdui-comment-submit'
          );
          controls.appendChild(submit);
        }}
      "></div>
    <div class="composer-controls">
      <button type="button" aria-expanded="false" aria-label="{labels.emoji}">
        <svg></svg>
      </button>
      <button type="button" aria-expanded="false" aria-label="{labels.emoji}">
        <svg></svg>
      </button>
      <button type="button" aria-label="{labels.photo}">
        <svg></svg>
      </button>
    </div>
  </div>
"""


def post(
    labels: Labels,
    *,
    urn: str = POST_URN,
    pressed: str = "false",
    opener: bool = True,
    editor: bool = True,
    comments: bool = True,
    draft: str = "",
    submit: bool = True,
    detours: bool = True,
    click_id: str = "post",
) -> str:
    """One post container, the way a permalink page renders it."""
    return f"""
<div class="update" data-urn="{urn}">
  <div class="actor">
    <button type="button" aria-pressed="false">Follow</button>
    <button type="button" aria-expanded="false">More</button>
    {"".join('<button type="button">actor control</button>' for _ in range(7))}
  </div>
  <div data-id="{urn}">
    <h2>Varun Bhartiya</h2>
    <p>Post body text</p>
  </div>
  <div data-urn="{SOCIAL_URN}">
    {action_bar(labels, pressed=pressed, opener=opener, click_id=click_id)}
  </div>
  {comment_editor(labels, draft=draft, submit=submit, detours=detours) if editor else ""}
  {comment_thread(labels) if comments else ""}
</div>
"""


def plain_post(labels: Labels) -> str:
    return post(labels)


def sdui_post(labels: Labels) -> str:
    """Current detail-page shape: a facepile id and no untouched pressed state."""
    return f"""
<div class="update">
  <div class="actor">
    <button type="button" aria-label="{labels.react}">Follow</button>
    <button type="button" aria-expanded="false">More</button>
    {"".join('<button type="button">actor control</button>' for _ in range(7))}
  </div>
  <div data-testid="ReactionFacepileCollection-{POST_URN}"></div>
  <div class="social-bar">
    <button type="button" aria-label="{labels.react}"
      onclick="document.body.setAttribute('data-clicked','post-react')"
      >{labels.like}</button>
    <button type="button" aria-expanded="false" aria-label="{labels.react}"
      onclick="this.setAttribute('aria-expanded','true');
               document.body.setAttribute('data-clicked','reaction-menu')"
      ></button>
    <button type="button">{labels.comment}</button>
    <button type="button" aria-expanded="false"
      onclick="this.setAttribute('aria-expanded','true');
               document.body.setAttribute('data-clicked','post-repost')"
      >{labels.repost_now}</button>
  </div>
  {comment_editor(labels)}
</div>
"""


def sdui_post_with_current_editor(labels: Labels) -> str:
    return sdui_post(labels).replace(
        comment_editor(labels), sdui_comment_editor(labels)
    )


def split_sdui_post(labels: Labels, *, marker_href: str | None = None) -> str:
    """The identity marker may sit outside the post action-bar subtree."""
    comment_bars = "".join(
        f"""
        <div class="comment-bar">
          <button type="button" aria-label="{labels.react}">{labels.like}</button>
          <button type="button" aria-expanded="false"></button>
          <button type="button" aria-expanded="false">{labels.comment}</button>
        </div>
        """
        for _ in range(2)
    )
    marker = f'<div data-testid="ReactionFacepileCollection-{POST_URN}"></div>'
    if marker_href is not None:
        marker = f'<a href="{marker_href}">{marker}</a>'
    return f"""
<div class="post-content">
  <div class="social-bar">
    <button type="button" aria-label="{labels.react}">{labels.like}</button>
    <button type="button" aria-expanded="false"></button>
    <button type="button">{labels.comment}</button>
    <button type="button" aria-expanded="false">{labels.repost_now}</button>
  </div>
  {comment_editor(labels)}
</div>
<div class="comment-list">
  {marker}
  {comment_bars}
</div>
"""


def post_with_activity_urn(labels: Labels) -> str:
    """The permalink's ugcPost id and rendered activity id can differ."""
    return post(labels, urn=ACTIVITY_URN)


def two_posts_without_the_permalink_id(labels: Labels) -> str:
    """A structural fallback cannot choose between two unrelated posts."""
    return post(labels, urn=ACTIVITY_URN) + post(
        labels, urn=OTHER_URN, editor=False, comments=False
    )


def post_already_reacted(labels: Labels) -> str:
    return post(labels, pressed="true")


def post_without_repost_opener(labels: Labels) -> str:
    """A bar with no ``aria-expanded``, which is not a full social bar."""
    return post(labels, opener=False)


def post_with_a_decoy_post(labels: Labels) -> str:
    """Another post on the page, which must not be mistaken for this one."""
    return post(labels) + post(labels, urn=OTHER_URN, editor=False, comments=False)


def post_with_nested_original(labels: Labels) -> str:
    """A reshare: the named post wraps another post that has its own bar.

    The nested original's controls come first in DOM order. Acting on them
    would engage the embedded post, so the named root's bar has to win.
    """
    inner = post(
        labels,
        urn=OTHER_URN,
        editor=False,
        comments=False,
        click_id="nested",
    )
    return f"""
<div class="update" data-urn="{POST_URN}">
  <div class="actor">
    <button type="button" aria-pressed="false">Follow</button>
    <button type="button" aria-expanded="false">More</button>
    {"".join('<button type="button">actor control</button>' for _ in range(7))}
  </div>
  {inner}
  <div data-urn="{SOCIAL_URN}">
    {action_bar(labels)}
  </div>
</div>
"""


def post_rendered_twice(labels: Labels) -> str:
    """The same post twice, as a reshare renders it. Ambiguous, so refused."""
    return post(labels) + post(labels, editor=False, comments=False)


def comments_only(labels: Labels) -> str:
    """Comments naming the post, with no post container at all."""
    return f'<div class="update">{comment_thread(labels)}</div>'


def post_with_sibling_comments(labels: Labels) -> str:
    """The comment thread as a sibling of the post rather than inside it.

    This is the fixture that makes the end-anchored URN match load-bearing.
    While the comments sit *inside* the post, a substring match still resolves
    because the outermost filter discards anything the post contains. Outside
    it, each comment URN carrying ``urn:li:comment:(urn:li:ugcPost:<postId>,…)``
    becomes an independent outermost candidate and the page turns ambiguous.
    """
    return (
        post(labels, comments=False)
        + f'<section class="comments-outside">{comment_thread(labels)}</section>'
    )


def post_with_a_wide_wrapper(labels: Labels) -> str:
    """A two-button bar, so the walk climbs past it into a crowded wrapper.

    The upper bound on the button count is what stops that climb from
    returning the wrapper as if it were the bar.
    """
    return f"""
<div class="update" data-urn="{POST_URN}">
  <div class="wrapper">
    <div class="social-bar">
      <button type="button" aria-pressed="false" aria-label="{labels.react}"
        >{labels.like}</button>
      <button type="button" aria-expanded="false" aria-label="{labels.repost}"
        >{labels.repost_now}</button>
    </div>
    {comment_thread(labels)}
    <button type="button">1</button>
    <button type="button">2</button>
    <button type="button">3</button>
    <button type="button">4</button>
    <button type="button">5</button>
  </div>
</div>
"""


def reaction_flyout(labels: Labels, *, count: int = 6) -> str:
    """The reaction picker, portal-mounted outside the post as LinkedIn does."""
    names = [
        labels.like,
        labels.celebrate,
        labels.support,
        labels.love,
        labels.insightful,
        labels.funny,
        "seventh",
    ][:count]
    controls = "".join(
        f'<button type="button" aria-label="{name}"'
        f" onclick=\"document.body.setAttribute('data-clicked','reaction-{index}')\""
        f">{name}</button>"
        for index, name in enumerate(names)
    )
    return f'<div class="flyout">{controls}</div>'


def six_control_decoy(labels: Labels) -> str:
    """Six labelled controls grouped as three pairs, not six picker entries."""
    controls = "".join(
        f"""
        <div>
          <button aria-label="{labels.react}"></button>
          <button aria-label="{labels.comment}"></button>
        </div>
        """
        for _ in range(3)
    )
    return f'<div class="decoy">{controls}</div>'


def repost_menu(labels: Labels, *, count: int = 2, layout: str = "popover") -> str:
    """One of LinkedIn's two measured portal-mounted repost layouts."""
    names = (
        [labels.repost_thoughts, labels.repost_now, "extra"]
        if layout == "popover"
        else [labels.repost_now, labels.repost_thoughts, "extra"]
    )[:count]
    role = "button" if layout == "popover" else "menuitem"
    items = "".join(
        f'<div role="{role}" tabindex="0"'
        f" onclick=\"document.body.setAttribute('data-clicked','menu-{index}')\""
        f">{name}</div>"
        for index, name in enumerate(names)
    )
    if layout == "popover":
        return f'<div popover="manual" style="display:block">{items}</div>'
    return f'<div role="menu">{items}</div>'


def composer_dialog(labels: Labels) -> str:
    """A portal-mounted commentary composer sitting beside the post.

    The permalink page already has a comment box, so an unscoped editor pin
    sees two textboxes. The dialog pin exists so the commentary path types
    into this one instead of the post's.
    """
    return f"""
<dialog open role="dialog">
  <div role="textbox" contenteditable="true"
    aria-label="{labels.repost_thoughts}"></div>
  <button type="submit" aria-label="{labels.submit}"
    onclick="document.body.setAttribute('data-clicked','composer-submit');
             return false;"
    >{labels.submit}</button>
</dialog>
"""


def current_composer_dialog(labels: Labels) -> str:
    return f"""
<dialog open>
  <div data-composer-row>
    <button type="button" aria-label="{labels.comment}"><svg></svg></button>
    <div role="button" aria-expanded="false"><svg></svg></div>
    <div role="textbox" contenteditable="true"
      aria-label="{labels.repost_thoughts}"></div>
    <button type="button" aria-expanded="false" aria-label="{labels.emoji}">
      <svg></svg>
    </button>
    <div data-submit-slot></div>
  </div>
</dialog>
"""


def _page_html(*blocks: str) -> str:
    return f"<html><body><main>{''.join(blocks)}</main></body></html>"


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed.

    Only launch/setup is guarded by the skip — the ``yield`` is outside it so
    an assertion failure or JS error in a test body is never swallowed into a
    skip. ``channel="chromium"`` names the browser this project installs.
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


async def _signals(page, html: str) -> dict:
    await page.set_content(_page_html(html))
    return await page.evaluate(POST_ACTION_SIGNALS_JS, POST_ID)


async def _pinned(page, html: str):
    """Set the page and pin the root post, as every write flow does first."""
    await page.set_content(_page_html(html))
    handle = await page.evaluate_handle(PIN_POST_ROOT_JS, POST_ID)
    return handle


async def _pinned_before_flyout(page, html: str):
    """Pin while the marked portal flyout is absent, then attach it."""
    await page.set_content(_page_html(html))
    flyout = await page.evaluate_handle(
        """() => {
          const node = document.querySelector('.flyout.new');
          if (!node) return null;
          node.remove();
          return node;
        }"""
    )
    root = await page.evaluate_handle(PIN_POST_ROOT_JS, POST_ID)
    await page.evaluate("(node) => document.body.append(node)", flyout)
    return root


async def _clicked(page) -> str | None:
    """What the page recorded being clicked.

    Patchright evaluates in an isolated world, so page-world variables are
    invisible there while the DOM is shared; the inline onclick records the
    click as a body attribute.
    """
    return await page.evaluate("document.body.getAttribute('data-clicked')")


async def _in_every_locale(
    page,
    build: Build,
    expected: Any,
    read: Callable[[Any, str], Awaitable[Any]],
) -> None:
    """Assert one answer for the same structure in all four label sets.

    The assertion carries the whole mapping rather than one set at a time, so
    a divergence names the set that diverged instead of failing on whichever
    case ran first.
    """
    answers = {labels.locale: await read(page, build(labels)) for labels in LOCALES}
    assert answers == {labels.locale: expected for labels in LOCALES}


class TestFindingTheRootPost:
    """Which element the programs treat as the post being acted on."""

    async def test_a_post_page_resolves_to_one_root_with_a_full_bar(
        self, dom_page
    ) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (
                signals["hasRoot"],
                signals["hasBar"],
                signals["barButtonCount"],
                signals["hasRepostOpener"],
                signals["reactPressed"],
                signals["editorCount"],
            )

        # Four buttons is the post's own bar. The comments container holds more
        # and the wrapper more still, so this number is the evidence that the
        # walk stopped at the bar.
        await _in_every_locale(
            dom_page, plain_post, (True, True, 4, True, False, 1), read
        )

    async def test_sdui_facepile_resolves_but_refuses_unknown_pressed_state(
        self, dom_page
    ) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            handle = await _pinned(page, html)
            reaction = await page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": handle})
            clicked = await _clicked(page)
            return (
                signals["hasRoot"],
                signals["hasBar"],
                signals["barButtonCount"],
                signals["hasRepostOpener"],
                signals["reactPressedPresent"],
                reaction,
                clicked,
            )

        await _in_every_locale(
            dom_page,
            sdui_post,
            (True, True, 4, True, False, "unsupported_state", None),
            read,
        )

    async def test_sdui_repost_uses_the_second_expanding_control(
        self, dom_page
    ) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            outcome = await page.evaluate(OPEN_REPOST_MENU_JS, {"root": handle})
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, sdui_post, ("clicked", "post-repost"), read)

    async def test_repost_counts_ignore_controls_outside_the_bar(
        self, dom_page
    ) -> None:
        async def read(page, html):
            return len((await _signals(page, html))["counts"])

        # The bar holds four buttons and no extra anchors. The comment thread
        # and editor hold more, so a root-wide scan would return a larger list.
        await _in_every_locale(dom_page, plain_post, 4, read)

    async def test_a_different_activity_id_resolves_by_unique_structure(
        self, dom_page
    ) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["hasBar"], signals["barButtonCount"])

        await _in_every_locale(dom_page, post_with_activity_urn, (True, True, 4), read)

    async def test_a_detached_sdui_identity_uses_the_unique_four_button_bar(
        self, dom_page
    ) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["hasBar"], signals["barButtonCount"])

        await _in_every_locale(dom_page, split_sdui_post, (True, True, 4), read)

    async def test_a_share_slug_can_own_one_bar_with_a_different_activity_id(
        self, dom_page
    ) -> None:
        async def read(page, html):
            async def fulfill(route):
                await route.fulfill(body=_page_html(html), content_type="text/html")

            await page.route(SHARE_URL, fulfill)
            try:
                await page.goto(SHARE_URL)
                signals = await page.evaluate(POST_ACTION_SIGNALS_JS, POST_ID)
                return (
                    signals["hasRoot"],
                    signals["hasBar"],
                    signals["barButtonCount"],
                )
            finally:
                await page.unroute(SHARE_URL, fulfill)

        await _in_every_locale(
            dom_page,
            lambda labels: (
                split_sdui_post(labels, marker_href=SHARE_URL).replace(
                    POST_URN, MISMATCH_ACTIVITY_URN
                )
                + action_bar(labels, click_id="later-decoy")
            ),
            (True, True, 4),
            read,
        )

    async def test_a_share_slug_refuses_a_facepile_linked_elsewhere(
        self, dom_page
    ) -> None:
        async def read(page, html):
            async def fulfill(route):
                await route.fulfill(body=_page_html(html), content_type="text/html")

            await page.route(SHARE_URL, fulfill)
            try:
                await page.goto(SHARE_URL)
                return (await page.evaluate(POST_ACTION_SIGNALS_JS, POST_ID))["hasRoot"]
            finally:
                await page.unroute(SHARE_URL, fulfill)

        await _in_every_locale(
            dom_page,
            lambda labels: split_sdui_post(
                labels, marker_href="https://www.linkedin.com/posts/someone_else/"
            ).replace(POST_URN, MISMATCH_ACTIVITY_URN),
            False,
            read,
        )

    async def test_two_bars_before_the_owned_facepile_are_refused(
        self, dom_page
    ) -> None:
        async def read(page, html):
            async def fulfill(route):
                await route.fulfill(body=_page_html(html), content_type="text/html")

            await page.route(SHARE_URL, fulfill)
            try:
                await page.goto(SHARE_URL)
                return (await page.evaluate(POST_ACTION_SIGNALS_JS, POST_ID))["hasRoot"]
            finally:
                await page.unroute(SHARE_URL, fulfill)

        await _in_every_locale(
            dom_page,
            lambda labels: (
                action_bar(labels, click_id="earlier-decoy")
                + split_sdui_post(labels, marker_href=SHARE_URL).replace(
                    POST_URN, MISMATCH_ACTIVITY_URN
                )
            ),
            False,
            read,
        )

    async def test_two_structural_fallback_candidates_are_refused(
        self, dom_page
    ) -> None:
        async def read(page, html):
            return (await _signals(page, html))["hasRoot"]

        await _in_every_locale(
            dom_page, two_posts_without_the_permalink_id, False, read
        )

    async def test_a_comment_urn_embedding_the_post_id_is_not_the_post(
        self, dom_page
    ) -> None:
        # Each comment carries `urn:li:comment:(urn:li:ugcPost:<postId>,…)` and
        # sits outside the post container, so a substring search makes every
        # one of them an independent candidate root and the page ambiguous. The
        # end-anchored match leaves only the post, and the bar it finds is the
        # post's own four-button bar rather than a two-button comment row.
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["hasBar"], signals["barButtonCount"])

        await _in_every_locale(
            dom_page, post_with_sibling_comments, (True, True, 4), read
        )

    async def test_comments_without_their_post_resolve_to_nothing(
        self, dom_page
    ) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasMain"], signals["hasRoot"])

        await _in_every_locale(dom_page, comments_only, (True, False), read)

    async def test_another_post_on_the_page_is_ignored(self, dom_page) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["barButtonCount"])

        await _in_every_locale(dom_page, post_with_a_decoy_post, (True, 4), read)

    async def test_a_nested_originals_bar_is_not_the_named_posts_bar(
        self, dom_page
    ) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            outcome = await page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": handle})
            return (outcome, await _clicked(page))

        await _in_every_locale(
            dom_page,
            post_with_nested_original,
            ("clicked", "post-react"),
            read,
        )

    async def test_the_same_post_twice_is_ambiguous_and_refused(self, dom_page) -> None:
        async def read(page, html):
            return (await _signals(page, html))["hasRoot"]

        await _in_every_locale(dom_page, post_rendered_twice, False, read)

    async def test_a_bar_without_a_repost_opener_is_not_a_bar(self, dom_page) -> None:
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["hasBar"])

        await _in_every_locale(
            dom_page, post_without_repost_opener, (True, False), read
        )

    async def test_the_walk_does_not_escape_into_a_crowded_wrapper(
        self, dom_page
    ) -> None:
        # The bar itself holds two buttons, under the floor, so the walk climbs
        # to a wrapper holding many. The upper bound is what refuses it.
        async def read(page, html):
            signals = await _signals(page, html)
            return (signals["hasRoot"], signals["hasBar"])

        await _in_every_locale(dom_page, post_with_a_wide_wrapper, (True, False), read)

    async def test_pinning_fails_for_an_ambiguous_page(self, dom_page) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            return await handle.evaluate("node => node === null")

        await _in_every_locale(dom_page, post_rendered_twice, True, read)


class TestReacting:
    """The reaction click, and the click it must not make."""

    async def test_the_click_lands_on_the_post_and_not_on_a_comment(
        self, dom_page
    ) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            outcome = await page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": handle})
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, plain_post, ("clicked", "post-react"), read)

    async def test_an_already_pressed_toggle_is_reported_and_not_clicked(
        self, dom_page
    ) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            outcome = await page.evaluate(CLICK_REACT_TOGGLE_JS, {"root": handle})
            return (outcome, await _clicked(page))

        # None as the recorded click is the assertion: a pressed toggle that is
        # clicked retracts the reaction.
        await _in_every_locale(
            dom_page, post_already_reacted, ("already_pressed", None), read
        )

    async def test_the_flyout_reports_its_own_control_count(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned_before_flyout(page, html)
            return (
                await page.evaluate(
                    READ_REACTION_FLYOUT_JS, {"expected": 6, "root": root}
                )
            )["count"]

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels)
                + reaction_flyout(labels).replace(
                    'class="flyout"', 'class="flyout new"'
                )
            ),
            6,
            read,
        )

    async def test_a_reaction_is_picked_by_index(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned_before_flyout(page, html)
            await page.evaluate(READ_REACTION_FLYOUT_JS, {"expected": 6, "root": root})
            clicked = await page.evaluate(
                CLICK_REACTION_JS, {"expected": 6, "index": 5, "root": root}
            )
            return (clicked, await _clicked(page))

        # Index 5 is the sixth control, which is what "funny" means positionally.
        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + reaction_flyout(labels).replace(
                    'class="flyout"', 'class="flyout new"'
                )
            ),
            (True, "reaction-5"),
            read,
        )

    async def test_six_controls_outside_six_entries_do_not_ambiguous_the_picker(
        self, dom_page
    ) -> None:
        async def read(page, html):
            root = await _pinned_before_flyout(page, html)
            count = (
                await page.evaluate(
                    READ_REACTION_FLYOUT_JS, {"expected": 6, "root": root}
                )
            )["count"]
            clicked = await page.evaluate(
                CLICK_REACTION_JS, {"expected": 6, "index": 1, "root": root}
            )
            return (count, clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + six_control_decoy(labels)
                + reaction_flyout(labels).replace(
                    'class="flyout"', 'class="flyout new"'
                )
            ),
            (6, True, "reaction-1"),
            read,
        )

    async def test_an_existing_six_entry_picker_is_not_claimed(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned_before_flyout(page, html)
            count = (
                await page.evaluate(
                    READ_REACTION_FLYOUT_JS, {"expected": 6, "root": root}
                )
            )["count"]
            clicked = await page.evaluate(
                CLICK_REACTION_JS, {"expected": 6, "index": 4, "root": root}
            )
            return (count, clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + reaction_flyout(labels).replace(
                    'class="flyout"', 'class="flyout old"'
                )
                + reaction_flyout(labels)
                .replace('class="flyout"', 'class="flyout new"')
                .replace("reaction-", "new-reaction-")
            ),
            (6, True, "new-reaction-4"),
            read,
        )

    @pytest.mark.parametrize("offered", [5, 7])
    async def test_a_flyout_that_is_not_six_controls_clicks_nothing(
        self, dom_page, offered: int
    ) -> None:
        # Both directions matter and they fail differently. At five, index 5
        # does not exist. At seven it does, and it names the wrong reaction —
        # which is why the guard is an exact count and not a lower bound.
        async def read(page, html):
            root = await _pinned_before_flyout(page, html)
            await page.evaluate(READ_REACTION_FLYOUT_JS, {"expected": 6, "root": root})
            clicked = await page.evaluate(
                CLICK_REACTION_JS, {"expected": 6, "index": 5, "root": root}
            )
            return (clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + reaction_flyout(labels, count=offered).replace(
                    'class="flyout"', 'class="flyout new"'
                )
            ),
            (False, None),
            read,
        )


class TestReposting:
    """The repost menu, where the wrong index publishes to your own feed."""

    async def test_the_opener_is_the_aria_expanded_control_in_the_bar(
        self, dom_page
    ) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            outcome = await page.evaluate(OPEN_REPOST_MENU_JS, {"root": handle})
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, plain_post, ("clicked", "post-repost"), read)

    async def test_the_menu_reports_its_own_item_count(self, dom_page) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            return await page.evaluate(READ_REPOST_MENU_JS)

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False) + repost_menu(labels)
            ),
            {"menus": 1, "items": 2, "layout": "popover"},
            read,
        )

    async def test_the_second_item_is_the_one_a_bare_repost_clicks(
        self, dom_page
    ) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            clicked = await page.evaluate(
                CLICK_REPOST_MENU_ITEM_JS,
                {"expected": 2, "index": 1, "layout": "popover"},
            )
            return (clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False) + repost_menu(labels)
            ),
            (True, "menu-1"),
            read,
        )

    async def test_the_legacy_menu_keeps_immediate_repost_first(self, dom_page) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            measured = await page.evaluate(READ_REPOST_MENU_JS)
            clicked = await page.evaluate(
                CLICK_REPOST_MENU_ITEM_JS,
                {"expected": 2, "index": 0, "layout": "menu"},
            )
            return (measured, clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + repost_menu(labels, layout="menu")
            ),
            ({"menus": 1, "items": 2, "layout": "menu"}, True, "menu-0"),
            read,
        )

    async def test_a_menu_of_three_items_clicks_nothing(self, dom_page) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            clicked = await page.evaluate(
                CLICK_REPOST_MENU_ITEM_JS,
                {"expected": 2, "index": 0, "layout": "popover"},
            )
            return (clicked, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels, editor=False, comments=False)
                + repost_menu(labels, count=3)
            ),
            (False, None),
            read,
        )


async def _typed(page, html, text: str):
    """Pin the post and its editor, then type into it the way production does.

    Real key events rather than a scripted write, because that is the whole
    difference measured against a live comment box: text written from
    JavaScript reads back correctly and still leaves LinkedIn's editor state
    empty, so its submit control is never drawn.
    """
    root = await _pinned(page, html)
    pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": root})
    editor = (await pinned.get_property("editor")).as_element()
    if editor is not None:
        await editor.click()
        await page.keyboard.type(text)
        await page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": text})
    return root, editor


class TestPinningTheEditor:
    """Finding the one editor to type into, scoped to the pinned post."""

    async def test_the_one_editor_in_the_post_is_pinned(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned(page, html)
            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": root})
            status = await (await pinned.get_property("status")).json_value()
            editor = (await pinned.get_property("editor")).as_element()
            return (status, editor is not None)

        await _in_every_locale(dom_page, plain_post, ("pinned", True), read)

    async def test_an_existing_draft_is_left_untouched(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned(page, html)
            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": root})
            status = await (await pinned.get_property("status")).json_value()
            held = await root.evaluate(
                "node => node.querySelector('[role=\"textbox\"]').innerText"
            )
            return (status, held)

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels, draft="half a thought"),
            ("draft_present", "half a thought"),
            read,
        )

    async def test_a_post_with_no_editor_refuses(self, dom_page) -> None:
        async def read(page, html):
            root = await _pinned(page, html)
            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": root})
            return await (await pinned.get_property("status")).json_value()

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels, editor=False),
            "ambiguous_editor",
            read,
        )


class TestPinningTheDialog:
    """The commentary composer is a dialog, not the post's comment box."""

    async def test_the_one_visible_dialog_is_pinned(self, dom_page) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            handle = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            return handle.as_element() is not None

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels) + composer_dialog(labels),
            True,
            read,
        )

    async def test_two_visible_dialogs_pin_nothing(self, dom_page) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            handle = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            return handle.as_element() is None

        await _in_every_locale(
            dom_page,
            lambda labels: (
                post(labels) + composer_dialog(labels) + composer_dialog(labels)
            ),
            True,
            read,
        )

    async def test_an_unscoped_pin_is_ambiguous_when_the_post_has_a_comment_box(
        self, dom_page
    ) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": None})
            return await (await pinned.get_property("status")).json_value()

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels) + composer_dialog(labels),
            "ambiguous_editor",
            read,
        )

    async def test_a_dialog_scoped_pin_takes_the_composer_not_the_comment_box(
        self, dom_page
    ) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            dialog = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            pinned = await page.evaluate_handle(
                PIN_EDITOR_JS, arg={"scope": dialog.as_element()}
            )
            status = await (await pinned.get_property("status")).json_value()
            editor = (await pinned.get_property("editor")).as_element()
            in_dialog = await editor.evaluate(
                "node => Boolean(node.closest('dialog, [role=\"dialog\"]'))"
            )
            return (status, in_dialog)

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels) + composer_dialog(labels),
            ("pinned", True),
            read,
        )


class TestWritingText:
    """Typing and submission, scoped to the post that was pinned."""

    async def test_typing_lands_in_the_editor_and_submitting_clicks_its_submit(
        self, dom_page
    ) -> None:
        async def read(page, html):
            root, editor = await _typed(page, html, "Well put, thanks")
            held = await editor.evaluate("node => node.innerText")
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": root, "text": "Well put, thanks"}
            )
            return (held, outcome, await _clicked(page))

        await _in_every_locale(
            dom_page,
            plain_post,
            ("Well put, thanks", "submitted", "comment-submit"),
            read,
        )

    async def test_sdui_submit_is_the_one_control_added_after_typing(
        self, dom_page
    ) -> None:
        async def read(page, html):
            root, editor = await _typed(page, html, "Well put, thanks")
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": root, "text": "Well put, thanks"}
            )
            return (outcome, await _clicked(page), editor is not None)

        await _in_every_locale(
            dom_page,
            sdui_post_with_current_editor,
            ("submitted", "sdui-comment-submit", True),
            read,
        )

    async def test_current_repost_dialog_has_one_structural_submit(
        self, dom_page
    ) -> None:
        async def read(page, html):
            await page.set_content(_page_html(html))
            dialog = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            pinned = await page.evaluate_handle(
                PIN_EDITOR_JS, arg={"scope": dialog.as_element()}
            )
            editor = (await pinned.get_property("editor")).as_element()
            await editor.click()
            await page.keyboard.type("Worth a read")
            await page.evaluate(
                OWN_EDITOR_JS, {"editor": editor, "text": "Worth a read"}
            )
            await page.evaluate(
                """() => {
                  const button = document.createElement('button');
                  button.type = 'button';
                  button.innerHTML = '<span>submit</span>';
                  button.onclick = () => document.body.setAttribute(
                    'data-clicked', 'current-repost-submit'
                  );
                  document.querySelector('[data-submit-slot]').append(button);
                }"""
            )
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS,
                {"scope": dialog.as_element(), "text": "Worth a read"},
            )
            return (outcome, await _clicked(page))

        await _in_every_locale(
            dom_page,
            current_composer_dialog,
            ("submitted", "current-repost-submit"),
            read,
        )

    async def test_repost_dialog_does_not_click_a_preexisting_unlabelled_button(
        self, dom_page
    ) -> None:
        def build(labels: Labels) -> str:
            return current_composer_dialog(labels).replace(
                "<dialog open>",
                '<dialog open><button type="button" '
                'onclick="document.body.setAttribute('
                "'data-clicked','preexisting-dialog-control')\">"
                "<span>close</span></button>",
            )

        async def read(page, html):
            await page.set_content(_page_html(html))
            dialog = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            pinned = await page.evaluate_handle(
                PIN_EDITOR_JS, arg={"scope": dialog.as_element()}
            )
            editor = (await pinned.get_property("editor")).as_element()
            await editor.click()
            await page.keyboard.type("Worth a read")
            await page.evaluate(
                OWN_EDITOR_JS, {"editor": editor, "text": "Worth a read"}
            )
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS,
                {"scope": dialog.as_element(), "text": "Worth a read"},
            )
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, build, ("no_submit_control", None), read)

    async def test_submitting_text_the_editor_does_not_hold_refuses(
        self, dom_page
    ) -> None:
        async def read(page, html):
            root, _ = await _typed(page, html, "Well put, thanks")
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": root, "text": "something else"}
            )
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, plain_post, ("not_owned", None), read)

    async def test_two_submit_candidates_click_nothing(self, dom_page) -> None:
        def build(labels: Labels) -> str:
            # A second control carrying `type="submit"` and neither
            # aria-expanded nor aria-pressed, so nothing distinguishes it from
            # the real one except its existence.
            return post(labels).replace(
                "</form>",
                f'<button type="submit" aria-label="{labels.submit}">'
                f"{labels.submit}</button></form>",
            )

        async def read(page, html):
            root, _ = await _typed(page, html, "hello")
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": root, "text": "hello"}
            )
            return (outcome, await _clicked(page))

        await _in_every_locale(dom_page, build, ("ambiguous_submit", None), read)

    async def test_a_form_with_no_submit_control_clicks_no_other_button(
        self, dom_page
    ) -> None:
        # The live failure this exists for. Before a comment box has text
        # LinkedIn renders no submit control, and the buttons that are there
        # are an emoji trigger and a photo attachment. The emoji one is dropped
        # for its aria-expanded, leaving the photo one looking unambiguous; the
        # earlier rule relaxed to "the only enabled button" and clicked it,
        # which opened a file picker and published nothing while the tool
        # reported the comment as posted. No label can separate those two
        # buttons in every locale, so the only safe answer is to click neither.
        async def read(page, html):
            root, _ = await _typed(page, html, "hello")
            outcome = await page.evaluate(
                SUBMIT_EDITOR_JS, {"scope": root, "text": "hello"}
            )
            return (outcome, await _clicked(page))

        await _in_every_locale(
            dom_page,
            lambda labels: post(labels, submit=False),
            ("no_submit_control", None),
            read,
        )

    async def test_a_cleared_editor_holds_nothing(self, dom_page) -> None:
        async def read(page, html):
            _, editor = await _typed(page, html, "typed then withdrawn")
            emptied = await page.evaluate(CLEAR_EDITOR_JS, {"editor": editor})
            # Trimmed, because emptying a contenteditable leaves the block break
            # behind: innerText reads "\n" for a box with nothing typed in it.
            return (emptied, await editor.evaluate("node => node.innerText.trim()"))

        await _in_every_locale(dom_page, plain_post, (True, ""), read)


class TestCountingRenderedText:
    """The confirmation read, which is what turns ``acted`` true."""

    async def test_text_absent_from_the_post_counts_zero(self, dom_page) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            return await page.evaluate(
                COUNT_TEXT_UNITS_JS, {"root": handle, "text": "Well put, thanks"}
            )

        await _in_every_locale(dom_page, plain_post, 0, read)

    async def test_one_rendered_comment_counts_once(self, dom_page) -> None:
        async def read(page, html):
            handle = await _pinned(page, html)
            return await page.evaluate(
                COUNT_TEXT_UNITS_JS, {"root": handle, "text": "Existing comment 1"}
            )

        # The smallest unit holding the text, so the <p> counts and the
        # <article> and every wrapper above it do not.
        await _in_every_locale(dom_page, plain_post, 1, read)

    async def test_an_identical_pair_counts_twice(self, dom_page) -> None:
        def build(labels: Labels) -> str:
            return post(labels).replace("Existing comment 2", "Existing comment 1")

        async def read(page, html):
            handle = await _pinned(page, html)
            return await page.evaluate(
                COUNT_TEXT_UNITS_JS, {"root": handle, "text": "Existing comment 1"}
            )

        # Two is what makes the pre-submit baseline meaningful: a duplicate of
        # an existing comment is only confirmed by the count going up.
        await _in_every_locale(dom_page, build, 2, read)

    async def test_the_draft_in_the_editor_is_not_counted(self, dom_page) -> None:
        # The bug this file exists to prevent from coming back. The editor is a
        # descendant of the post and its innerText is exactly the text that was
        # typed, so counting it makes the confirmation compare the draft against
        # itself: the count rises on the typing alone and reports a comment as
        # published whether the submit landed, clicked the wrong control, or was
        # rejected outright. Measured against a live post, which returned 1 with
        # nothing published and no comment node anywhere in the DOM.
        async def read(page, html):
            root, editor = await _typed(page, html, "Well put, thanks")
            return (
                await editor.evaluate("node => node.innerText"),
                await page.evaluate(
                    COUNT_TEXT_UNITS_JS, {"root": root, "text": "Well put, thanks"}
                ),
            )

        # The text is demonstrably in the editor, and the count is still zero.
        await _in_every_locale(dom_page, plain_post, ("Well put, thanks", 0), read)

    async def test_a_comment_rendered_after_the_draft_counts_once(
        self, dom_page
    ) -> None:
        # The other direction, so the exclusion above is not passing by refusing
        # everything: with the same text in the editor, a comment LinkedIn
        # rendered back does count, and that is the transition `acted` reports.
        async def read(page, html):
            root, _ = await _typed(page, html, "Well put, thanks")
            await root.evaluate(
                "node => node.querySelector('.comments').insertAdjacentHTML("
                "'beforeend',"
                '\'<article data-id="urn:li:comment:x">'
                "<p>Well put, thanks</p></article>')"
            )
            return await page.evaluate(
                COUNT_TEXT_UNITS_JS, {"root": root, "text": "Well put, thanks"}
            )

        await _in_every_locale(dom_page, plain_post, 1, read)
