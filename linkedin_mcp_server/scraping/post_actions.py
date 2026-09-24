"""Engagement actions taken on one loaded LinkedIn post.

Three writes live here: a reaction, a comment and a repost. They share one
hard problem, which is why they share a module: *which* post is being acted
on. A permalink page renders the post, every comment on it, and often a
reshared post inside it, and each of those carries its own action controls.
Acting on the wrong one is not a failed scrape, it is a public write on
somebody else's content, so every flow below anchors on the post the caller
named before it touches anything.

The anchor is the numeric entity id, which both permalink shapes carry:
``/feed/update/urn:li:ugcPost:7506667649444237313/`` holds it in the URN and
``/posts/name_words-ugcPost-7506667649444237313-g3b0`` holds it in the slug.
That id is first matched against the URN-bearing ``data-`` attributes LinkedIn
puts on a post container. LinkedIn can render the same entity under a different
activity id, so a permalink-page fallback accepts the *outermost* single
URN-bearing container only when it has a complete social action bar. Two
structurally valid roots or none is a refusal rather than a guess.

Per the AGENTS.md Scraping Rules nothing here reads a label value. The
controls are told apart by which ARIA attribute they carry, which is a fact
about the control rather than about the language the page is in:

- the reaction toggle is the ``button[aria-pressed]`` in the bar, and its
  value is also the answer to "has this account already reacted";
- the repost menu opener is the ``button[aria-expanded]`` in the bar, the
  same inverse-of-aria-label trick the profile More menu uses;
- the comment editor is the ``[role="textbox"][contenteditable="true"]``
  inside the root post;
- a repost-with-commentary editor lives in a portal-mounted dialog outside the
  post, even though opening it changes the URL to `/sharing/compose`.

Two positional assumptions remain, both guarded by an exact count so a
layout change refuses instead of clicking the wrong thing. They are named at
their call sites: ``_REACTION_ORDER`` for the reaction flyout and
``_REPOST_MENU_ITEMS`` for the repost menu.

What the tests here can and cannot prove is worth stating plainly, because
AGENTS.md draws the line: ``tests/test_post_actions_dom.py`` drives synthetic
containers, so it is a claim about this algorithm and not a claim about
LinkedIn's markup. The attribute *names* below are the standing risk, and a
rename turns every flow into a documented refusal rather than a misfire.
"""

from __future__ import annotations

from typing import Any

import asyncio
import logging
import re

from patchright.async_api import ElementHandle, TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.scraping.contracts import (
    POST_ACTION_INTERRUPTED_WARNING,
    post_action_result,
)
from linkedin_mcp_server.scraping.identifiers import normalize_post_reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

# LinkedIn's reaction flyout, in the order it renders. Selection is by index
# because the only other discriminator is the label, which is the one thing
# this project never reads. The order has been stable for years and is the
# same in every locale, since it is the product's own ranking rather than
# anything translated.
#
# The exact-count guard below is what makes the assumption safe to hold: if
# LinkedIn adds a seventh reaction or drops one, the count stops being six and
# every specific-reaction request refuses instead of silently landing one
# position off. A wrong reaction is public and attributed, so refusing is the
# cheaper failure.
_REACTION_ORDER = ("like", "celebrate", "support", "love", "insightful", "funny")

# The legacy menu renders immediate repost first; the current popover renders
# commentary first. Layout and count are both re-verified at click time because
# getting this position wrong publishes immediately to the actor's own feed.
_REPOST_MENU_ITEMS = 2
_REPOST_INDEXES = {
    "menu": {"immediate": 0, "commentary": 1},
    "popover": {"immediate": 1, "commentary": 0},
}

# The band a social action bar's button count falls in. The bar holds react,
# comment, repost and send, so three is the floor once a layout drops one and
# eight is loose enough for an overflow control. The band exists to stop the
# ancestor walk from climbing out of the bar and into the comments container,
# which also holds many buttons and one `aria-pressed` toggle per comment.
_BAR_BUTTONS_MIN = 3
_BAR_BUTTONS_MAX = 8

# How long a control gets to appear after the interaction that reveals it.
_FLYOUT_TIMEOUT = 4000
_EDITOR_TIMEOUT = 5000
# How long a submitted comment or repost has to show up in the DOM. Longer
# than the flyout waits because this one covers a round trip to LinkedIn.
_CONFIRM_TIMEOUT = 12000
_CONFIRM_POLL = 0.25

# Per-character delay while typing into an editor, and how long the submit
# control gets to render once the text is in. The delay is not politeness: the
# editor's submit button is drawn by a handler reacting to input, and that
# handler is the thing being waited for here.
_TYPE_DELAY = 12
_SUBMIT_TIMEOUT = 4000

_EDITOR_SELECTOR = '[role="textbox"][contenteditable="true"]'
_DIALOG_SELECTOR = 'dialog[open], [role="dialog"]'
_DIALOG_EDITOR_SELECTOR = (
    'dialog[open] [role="textbox"][contenteditable="true"], '
    '[role="dialog"] [role="textbox"][contenteditable="true"]'
)

# The numeric entity id inside either permalink shape. Both are produced by
# `normalize_post_reference`, so this reads its output rather than a caller's
# input and can be strict about the shape.
_URN_ID = re.compile(r"/feed/update/urn:li:(?:ugcPost|share|activity):([0-9]+)/")
_SLUG_ID = re.compile(r"/posts/[A-Za-z0-9_-]*?-(?:ugcPost|activity|share)-([0-9]+)-")

# Attributes LinkedIn has been observed to hang a post URN on. Presence and
# value-contains only; no class names, per the Scraping Rules. This list is
# the module's single point of DOM dependence and the thing to check first
# when every action starts answering `post_not_found`.
_URN_ATTRIBUTES = (
    "data-urn",
    "data-id",
    "data-activity-urn",
    "data-entity-urn",
    "data-chameleon-result-urn",
    "data-testid",
)

_VISIBLE_FN_JS = r"""
function visible(element) {
  if (!(element instanceof Element) || !element.isConnected) return false;
  const style = window.getComputedStyle(element);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  if (element.getAttribute('aria-hidden') === 'true') return false;
  return element.getClientRects().length > 0;
}
"""

# Locate the one post container the caller named, by entity id.
#
# The id is matched at the *end* of a post URN and nowhere else, which is not
# fussiness. A substring search finds the post's id inside its own comments:
# a comment URN is `urn:li:comment:(urn:li:ugcPost:<postId>,<commentId>)` and a
# social-detail URN wraps the post the same way, so `includes(':' + postId)`
# makes every comment on the post a candidate root. Anchoring on the kind and
# the end of the value leaves only URNs that *are* the post.
#
# `outermost` handles the other direction: LinkedIn hangs the same URN on a
# container and again on something inside it, so several elements legitimately
# name one post. Two *outermost* matches mean the id appears in two
# independent places, which is what a reshare of the post produces, and
# nothing in the id says which one the caller meant. That refuses.
#
# One post has both a `ugcPost` and an `activity` URN, with different numbers.
# The permalink may carry the former while LinkedIn's container names only the
# latter. When the exact match is absent, the fallback below accepts one
# outermost bare post URN only if it owns a complete action bar. Requiring
# exactly one keeps a page with another post or independent reshare ambiguous.
#
# An SDUI detail page can put the exact facepile identity in the comment-list
# subtree rather than around the post controls. In that shape the page root is
# accepted only when it owns exactly one action bar; comment bars have three
# buttons and therefore cannot satisfy the four-button SDUI shape below.
_FIND_POST_ROOT_FN_JS = (
    r"""
function findPostRoot(postId) {
  const main = document.querySelector('main');
  if (!main) return null;
  const digits = String(postId).replace(/[^0-9]/g, '');
  if (!digits) return null;
  const pattern = new RegExp('urn:li:(?:ugcPost|share|activity):' + digits + '$');
  const barePostUrn = /^urn:li:(?:ugcPost|share|activity):[0-9]+$/;
  const attributes = """
    + repr(list(_URN_ATTRIBUTES)).replace("'", '"')
    + r""";
  const selector = attributes.map(name => '[' + name + ']').join(',');
  const matches = [];
  for (const element of main.querySelectorAll(selector)) {
    for (const name of attributes) {
      const value = element.getAttribute(name);
      if (value && pattern.test(value.trim())) {
        matches.push(element);
        break;
      }
    }
  }
  const owners = [];
  for (const match of matches) {
    const isDirectRoot = attributes.some(name => {
      const value = match.getAttribute(name);
      return value && barePostUrn.test(value.trim());
    });
    if (isDirectRoot) {
      owners.push(match);
      continue;
    }
    let element = match;
    while (element && main.contains(element)) {
      if (visible(element) && findActionBar(element) !== null) {
        owners.push(element);
        break;
      }
      element = element.parentElement;
    }
  }
  const uniqueOwners = Array.from(new Set(owners));
  const outermost = uniqueOwners.filter(
    element => !uniqueOwners.some(
      other => other !== element && other.contains(element)
    )
  );
  if (outermost.length === 1) return outermost[0];
  if (outermost.length > 1) return null;
  if (matches.length > 0 && findActionBar(main) !== null) return main;
  const slugPattern = new RegExp(
    '-(?:ugcPost|share|activity)-' + digits + '(?:-|/|$)'
  );
  if (
    window.location.pathname.startsWith('/posts/') &&
    slugPattern.test(window.location.pathname) &&
    currentPermalinkMarkers(main).length === 1 &&
    findActionBar(main) !== null
  ) return main;

  const structural = [];
  for (const element of main.querySelectorAll(selector)) {
    for (const name of attributes) {
      const value = element.getAttribute(name);
      if (value && barePostUrn.test(value.trim())) {
        structural.push(element);
        break;
      }
    }
  }
  const structuralOutermost = structural.filter(
    element => !structural.some(
      other => other !== element && other.contains(element)
    )
  );
  const valid = structuralOutermost.filter(
    element => visible(element) && findActionBar(element) !== null
  );
  return valid.length === 1 ? valid[0] : null;
}
"""
)

# Locate the root post's own social action bar inside its container.
#
# Every visible `aria-pressed` button is tried in DOM order. The SDUI post page
# does not put `aria-pressed` on an untouched reaction control, so a labelled
# non-expanding button is also a candidate only when its compact bar has exactly
# four buttons and two expanding controls. The label's value is never read.
# Those structural guards distinguish the SDUI post bar from three-button
# comment bars, while the original one-expander shape still requires
# `aria-pressed`.
#
# LinkedIn also puts `aria-pressed` on the author's Follow control, so assuming
# the first one is the reaction toggle makes every real permalink refuse. For
# each candidate, the walk climbs to the smallest ancestor that looks like a
# bar. A candidate whose walk reaches a container wider than a bar is abandoned;
# later toggles still get their own walk.
#
# A permalink that reshares another post nests that original's bar inside the
# named root. Taking the first structurally valid bar would act on the nested
# post. Any toggle that sits inside a descendant bare post URN is skipped so
# the named root's own bar is the only one that can qualify; two remaining
# bars or none is a refusal.
_FIND_ACTION_BAR_FN_JS = (
    r"""
function insideNestedPost(root, node) {
  const attributes = """
    + repr(list(_URN_ATTRIBUTES)).replace("'", '"')
    + r""";
  const barePostUrn = /^urn:li:(?:ugcPost|share|activity):[0-9]+$/;
  let element = node.parentElement;
  while (element && element !== root && root.contains(element)) {
    for (const name of attributes) {
      const value = element.getAttribute(name);
      if (value && barePostUrn.test(value.trim())) return true;
    }
    element = element.parentElement;
  }
  return false;
}
function currentPermalinkMarkers(root) {
  const current = window.location.pathname.replace(/\/+$/, '');
  return Array.from(root.querySelectorAll(
    '[data-testid^="ReactionFacepileCollection-urn:li:"]'
  )).filter(marker => {
    if (!visible(marker)) return false;
    const anchor = marker.closest('a[href]');
    if (!anchor) return false;
    try {
      const target = new URL(anchor.href, window.location.href);
      return target.origin === window.location.origin &&
        target.pathname.replace(/\/+$/, '') === current;
    } catch {
      return false;
    }
  });
}
function findActionBar(root) {
  const toggles = Array.from(root.querySelectorAll(
    'button[aria-pressed], button[aria-label]:not([aria-expanded])'
  ))
    .filter(visible);
  if (toggles.length === 0) return null;
  const found = [];
  for (const toggle of toggles) {
    if (insideNestedPost(root, toggle)) continue;
    let element = toggle.parentElement;
    while (element && root.contains(element)) {
      const buttons = element.querySelectorAll('button');
      if (buttons.length >= """
    + str(_BAR_BUTTONS_MIN)
    + r""") {
        const expanders = element.querySelectorAll('button[aria-expanded]');
        const oldShape = toggle.hasAttribute('aria-pressed') &&
          expanders.length === 1;
        const sduiShape = !toggle.hasAttribute('aria-pressed') &&
          toggle.hasAttribute('aria-label') &&
          buttons.length === 4 &&
          expanders.length === 2;
        if (
          buttons.length <= """
    + str(_BAR_BUTTONS_MAX)
    + r""" &&
          !element.querySelector('[role="textbox"][contenteditable="true"]') &&
          (oldShape || sduiShape)
        ) {
          found.push({bar: element, toggle});
        }
        break;
      }
      element = element.parentElement;
    }
  }
  if (found.length === 1) return found[0];
  if (found.length > 1) {
    const markers = currentPermalinkMarkers(root);
    if (markers.length === 1) {
      const preceding = found.filter(
        item => item.bar.compareDocumentPosition(markers[0]) &
          Node.DOCUMENT_POSITION_FOLLOWING
      );
      if (preceding.length === 1) return preceding[0];
    }
  }
  return null;
}
"""
)

# Everything a flow needs to decide what to do, read in one pass.
#
# `counts` is every visible control's text *in the action bar*, carried as
# opaque strings that are only ever compared to the same list read earlier
# for inequality. Nothing parses them, and that is the point: a reaction or
# repost count renders with locale digit grouping and an abbreviation suffix,
# so reading a number out of one would be the text dependency the Scraping
# Rules forbid, while noticing that the list is no longer identical is not.
# The bar is the bound because a permalink also holds author links, follow
# controls and comment-row buttons; those changing during the confirm window
# is not evidence a reshare landed. Residual: another member reacting or
# commenting can still change a bar button's text. `barText` is carried for
# diagnostics only and no decision reads it.
POST_ACTION_SIGNALS_JS = (
    r"""
((postId) => {
"""
    + _VISIBLE_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return {hasMain: false};
  const root = findPostRoot(postId);
  if (!root) return {hasMain: true, hasRoot: false};
  const found = findActionBar(root);
  const editors = Array.from(root.querySelectorAll(
    '[role="textbox"][contenteditable="true"]'
  )).filter(visible);
  const counts = found
    ? Array.from(found.bar.querySelectorAll('button, a'))
        .filter(visible)
        .map(element => (element.innerText || '').trim())
    : [];
  return {
    hasMain: true,
    hasRoot: true,
    hasBar: !!found,
    barButtonCount: found ? found.bar.querySelectorAll('button').length : 0,
    reactPressedPresent: found
      ? found.toggle.hasAttribute('aria-pressed')
      : false,
    reactPressed: found
      ? found.toggle.hasAttribute('aria-pressed')
        ? (found.toggle.getAttribute('aria-pressed') || '').toLowerCase() === 'true'
        : null
      : null,
    reactDisabled: found
      ? found.toggle.disabled ||
        (found.toggle.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
      : null,
    hasRepostOpener: found
      ? [1, 2].includes(found.bar.querySelectorAll('button[aria-expanded]').length)
      : false,
    editorCount: editors.length,
    barText: found ? (found.bar.innerText || '') : '',
    counts,
  };
})
"""
)

# A reaction flyout is portal-mounted, so ownership cannot be proved by DOM
# containment. Return only its minimal six-entry structural candidates; the
# pin records every candidate that existed before hover, and the reader accepts
# only one that appeared afterwards.
_REACTION_FLYOUTS_FN_JS = r"""
function reactionFlyouts(expected) {
  const controls = Array.from(document.querySelectorAll(
    'button[aria-label], [role="menuitem"][aria-label]'
  )).filter(visible);
  const containers = new Set();
  for (const control of controls) {
    let element = control.parentElement;
    while (element) {
      containers.add(element);
      element = element.parentElement;
    }
  }
  const matching = Array.from(containers).filter(container => {
    const owned = controls.filter(control => container.contains(control));
    const children = Array.from(container.children);
    return owned.length === expected &&
      children.length === expected &&
      children.every(child =>
        controls.filter(control => child === control || child.contains(control))
          .length === 1
      );
  });
  return matching.filter(
    container => !matching.some(
      other => other !== container && container.contains(other)
    )
  );
}
"""

# Pin the root post and its controls on the node itself, so every later step
# is scoped to one subtree that cannot drift. Same technique as the message
# composer's `__linkedinMcpComposer`, and for the same reason: a re-query
# between steps can land on a different post after the feed rerenders.
PIN_POST_ROOT_JS = (
    r"""
((postId) => {
"""
    + _VISIBLE_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + _REACTION_FLYOUTS_FN_JS
    + r"""
  const root = findPostRoot(postId);
  if (!root) return null;
  const found = findActionBar(root);
  if (!found) return null;
  const openers = Array.from(
    found.bar.querySelectorAll('button[aria-expanded]')
  ).filter(visible);
  root.__linkedinMcpPost = {
    postId: String(postId),
    bar: found.bar,
    toggle: found.toggle,
    opener: openers.length === 1
      ? openers[0]
      : openers.length === 2
        ? openers[1]
        : null,
    reactionFlyoutBaseline: new Set(reactionFlyouts(6)),
    reactionFlyout: null,
  };
  return root;
})
"""
)

# Click the pinned reaction toggle. Re-verifies the pin and the pressed state
# inside the same tick as the click: a toggle already pressed would *remove*
# the reaction, which is the one way this flow could undo something the
# account meant to keep.
CLICK_REACT_TOGGLE_JS = r"""
((arg) => {
  const pinned = arg.root?.__linkedinMcpPost;
  if (!pinned || !arg.root.isConnected) return 'unpinned';
  const toggle = pinned.toggle;
  if (!toggle.isConnected || !arg.root.contains(toggle)) return 'unpinned';
  if (
    toggle.disabled ||
    (toggle.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
  ) {
    return 'disabled';
  }
  if (!toggle.hasAttribute('aria-pressed')) return 'unsupported_state';
  if ((toggle.getAttribute('aria-pressed') || '').toLowerCase() === 'true') {
    return 'already_pressed';
  }
  toggle.click();
  return 'clicked';
})
"""

# Read the reaction flyout that hovering the toggle opens.
#
# Searched document-wide because the flyout is portal-mounted outside the post
# container. Ownership comes from being the sole structural candidate that was
# not present when this post was pinned, before its toggle was hovered.
READ_REACTION_FLYOUT_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + _REACTION_FLYOUTS_FN_JS
    + r"""
  const pinned = arg.root?.__linkedinMcpPost;
  if (!pinned || !arg.root.isConnected) return {count: 0};
  const candidates = reactionFlyouts(arg.expected).filter(
    container => !pinned.reactionFlyoutBaseline.has(container)
  );
  if (candidates.length === 0) return {count: 0};
  if (candidates.length !== 1) return {count: -1};
  pinned.reactionFlyout = candidates[0];
  const controls = Array.from(candidates[0].querySelectorAll(
    'button[aria-label], [role="menuitem"][aria-label]'
  )).filter(visible);
  return {
    count: controls.length,
    labels: controls.map(control => !!control.getAttribute('aria-label')),
  };
})
"""
)

# Click one reaction by index, re-verifying the owned flyout in the same tick.
# See _REACTION_ORDER for why the count is the whole safety argument here.
CLICK_REACTION_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + _REACTION_FLYOUTS_FN_JS
    + r"""
  const pinned = arg.root?.__linkedinMcpPost;
  if (!pinned || !arg.root.isConnected) return false;
  const flyout = pinned.reactionFlyout;
  if (
    !flyout ||
    !flyout.isConnected ||
    !reactionFlyouts(arg.expected).includes(flyout)
  ) return false;
  const owned = Array.from(flyout.querySelectorAll(
    'button[aria-label], [role="menuitem"][aria-label]'
  )).filter(visible);
  if (owned.length !== arg.expected) return false;
  const target = owned[arg.index];
  if (!target || !target.isConnected) return false;
  target.click();
  return true;
})
"""
)

# Open the repost menu from the pinned `aria-expanded` opener.
OPEN_REPOST_MENU_JS = r"""
((arg) => {
  const pinned = arg.root?.__linkedinMcpPost;
  if (!pinned || !arg.root.isConnected) return 'unpinned';
  const opener = pinned.opener;
  if (!opener || !opener.isConnected || !arg.root.contains(opener)) return 'unpinned';
  if (
    opener.disabled ||
    (opener.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
  ) {
    return 'disabled';
  }
  opener.click();
  // The opener is the ownership tie for a portal-mounted menu. If this
  // control did not expand, a visible menu elsewhere is not ours.
  if ((opener.getAttribute('aria-expanded') || '').toLowerCase() !== 'true') {
    return 'not_expanded';
  }
  return 'clicked';
})
"""

# Read, then click, one item of the open repost menu. The menu is portal
# mounted, so it is found by role rather than by containment in the post.
READ_REPOST_MENU_JS = (
    r"""
(() => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const candidates = [];
  for (const menu of Array.from(
    document.querySelectorAll('[role="menu"]')
  ).filter(visible)) {
    const items = Array.from(menu.querySelectorAll(
      '[role="menuitem"], button'
    )).filter(visible);
    if (items.length > 0) candidates.push({container: menu, items, layout: 'menu'});
  }
  for (const popover of Array.from(
    document.querySelectorAll('[popover="manual"]')
  ).filter(visible)) {
    const items = Array.from(
      popover.querySelectorAll('[role="button"]')
    ).filter(visible);
    if (items.length > 0) {
      candidates.push({container: popover, items, layout: 'popover'});
    }
  }
  if (candidates.length !== 1) return {menus: candidates.length, items: 0};
  const items = candidates[0].items;
  return {menus: 1, items: items.length, layout: candidates[0].layout};
})
"""
)

CLICK_REPOST_MENU_ITEM_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const opener = arg.root && arg.root.__linkedinMcpPost
    ? arg.root.__linkedinMcpPost.opener
    : null;
  if (
    arg.root &&
    (
      !opener ||
      (opener.getAttribute('aria-expanded') || '').toLowerCase() !== 'true'
    )
  ) {
    return false;
  }
  const candidates = [];
  for (const menu of Array.from(
    document.querySelectorAll('[role="menu"]')
  ).filter(visible)) {
    const items = Array.from(menu.querySelectorAll(
      '[role="menuitem"], button'
    )).filter(visible);
    if (items.length > 0) candidates.push({container: menu, items, layout: 'menu'});
  }
  for (const popover of Array.from(
    document.querySelectorAll('[popover="manual"]')
  ).filter(visible)) {
    const items = Array.from(
      popover.querySelectorAll('[role="button"]')
    ).filter(visible);
    if (items.length > 0) {
      candidates.push({container: popover, items, layout: 'popover'});
    }
  }
  if (candidates.length !== 1) return false;
  if (candidates[0].layout !== arg.layout) return false;
  const items = candidates[0].items;
  if (items.length !== arg.expected) return false;
  const target = items[arg.index];
  if (!target || !target.isConnected) return false;
  target.click();
  return true;
})
"""
)

# Pin the one empty editor in scope and hand it back for the caller to type
# into. Finding it is all this does: the text arrives through real key events
# in `_type_text`, not from here.
#
# Nothing is written from JavaScript, and that is a measured requirement rather
# than a preference. `document.execCommand('insertText')` does put the
# characters in the element and does leave `innerText` reading back exactly
# right, so every check available from inside the page passes — while
# LinkedIn's own editor state never updates and its submit button is never
# drawn. A comment "typed" that way is a draft the page does not know about.
# The message composer accepts `execCommand`; the comment box does not, so the
# two paths differ on purpose.
# The commentary composer is portal-mounted. Pinning it by the same rule as
# the comment editor — exactly one visible dialog, or refuse — is what keeps
# a permalink page's in-post comment box from being typed into instead.
PIN_VISIBLE_DIALOG_JS = (
    r"""
(() => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const dialogs = Array.from(
    document.querySelectorAll('dialog[open], [role="dialog"]')
  ).filter(visible);
  if (dialogs.length !== 1) return null;
  return dialogs[0];
})
"""
)

PIN_EDITOR_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const scope = arg.scope || document;
  const editors = Array.from(
    scope.querySelectorAll('[role="textbox"][contenteditable="true"]')
  ).filter(visible);
  if (editors.length !== 1) return {status: 'ambiguous_editor', editor: null};
  const editor = editors[0];
  if ((editor.innerText || '').trim()) return {status: 'draft_present', editor: null};
  const scopeButtons = scope instanceof Element
    ? Array.from(scope.querySelectorAll('button')).filter(visible)
    : [];
  let controls = editor.parentElement;
  while (controls && scope.contains(controls)) {
    const buttons = Array.from(controls.querySelectorAll('button')).filter(visible);
    if (buttons.length > 0) {
      editor.__linkedinMcpInitialControls = {
        count: buttons.length,
        scopeElements: scopeButtons,
        labelledSvg: buttons.filter(
          button => button.hasAttribute('aria-label') && button.querySelector('svg')
        ).length,
        expanders: buttons.filter(
          button => button.hasAttribute('aria-expanded')
        ).length,
      };
      break;
    }
    controls = controls.parentElement;
  }
  return {status: 'pinned', editor: editor};
})
"""
)

# Record that this editor holds text this server typed, so the submit step can
# refuse an editor that changed underneath it.
OWN_EDITOR_JS = r"""
((arg) => {
  arg.editor.__linkedinMcpOwnedText = arg.text;
  return true;
})
"""

# Empty an editor this server typed into, used when the typed text did not come
# back verbatim. Leaving a half-written draft in a live comment box is a visible
# side effect of a refusal, so a refusal cleans up after itself.
CLEAR_EDITOR_JS = r"""
((arg) => {
  const editor = arg.editor;
  if (!editor || !editor.isConnected) return false;
  editor.focus();
  const range = document.createRange();
  range.selectNodeContents(editor);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  document.execCommand('delete', false, null);
  return (editor.innerText || '').trim() === '';
})
"""

# Submit the pinned editor by clicking one structurally identified control.
#
# The original composer exposes exactly one enabled `type="submit"`. The SDUI
# composer instead starts with three labelled SVG controls, then appends one
# unlabeled, non-SVG `type="button"` after real key events. That exact 3-to-4
# transition identifies the new submit control without reading a label. The
# pinned repost dialog has one enabled unlabeled, non-SVG `type="button"`; its
# other controls are labelled or expanding. Neither rule is relaxed to "the
# only enabled button": the untouched photo control is labelled, contains an
# SVG, and exists in the recorded baseline.
#
# The submit control is also absent until the editor holds text LinkedIn
# believes a human entered, which is why `_type_text` uses real key events.
# Zero candidates is reported apart from two, because the two failures have
# nothing in common: zero means the editor never registered the text, while
# two means the form holds a control this rule cannot tell from the submit.
SUBMIT_EDITOR_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const scope = arg.scope || document;
  const editors = Array.from(
    scope.querySelectorAll('[role="textbox"][contenteditable="true"]')
  ).filter(visible);
  if (editors.length !== 1) return 'ambiguous_editor';
  const editor = editors[0];
  if (editor.__linkedinMcpOwnedText !== arg.text) return 'not_owned';
  let owner = editor.parentElement;
  while (owner && !owner.matches('form, dialog, [role="dialog"]')) {
    owner = owner.parentElement;
  }
  owner = owner || scope;
  const buttons = Array.from(
    owner.querySelectorAll('button[type="submit"], button')
  ).filter(button =>
    visible(button) &&
    !button.disabled &&
    (button.getAttribute('aria-disabled') || '').toLowerCase() !== 'true' &&
    !button.hasAttribute('aria-expanded') &&
    !button.hasAttribute('aria-pressed')
  );
  const candidates = buttons.filter(button => button.type === 'submit');
  if (candidates.length > 1) return 'ambiguous_submit';
  if (candidates.length === 1) {
    candidates[0].click();
    return 'submitted';
  }
  if (
    scope instanceof Element &&
    scope.matches('dialog[open], [role="dialog"]')
  ) {
    const baseline = editor.__linkedinMcpInitialControls;
    const dialogCandidates = Array.from(
      scope.querySelectorAll('button[type="button"]')
    ).filter(button =>
      visible(button) &&
      !button.disabled &&
      (button.getAttribute('aria-disabled') || '').toLowerCase() !== 'true' &&
      !button.hasAttribute('aria-label') &&
      !button.hasAttribute('aria-expanded') &&
      !button.hasAttribute('aria-pressed') &&
      !button.querySelector('svg') &&
      baseline &&
      !baseline.scopeElements.includes(button)
    );
    if (dialogCandidates.length > 1) return 'ambiguous_submit';
    if (dialogCandidates.length === 1) {
      dialogCandidates[0].click();
      return 'submitted';
    }
  }
  const baseline = editor.__linkedinMcpInitialControls;
  let controls = editor.parentElement;
  while (controls && scope.contains(controls)) {
    const compact = Array.from(controls.querySelectorAll('button')).filter(visible);
    if (compact.length > 0) {
      const generated = compact.filter(button =>
        !button.disabled &&
        (button.getAttribute('aria-disabled') || '').toLowerCase() !== 'true' &&
        button.type === 'button' &&
        !button.hasAttribute('aria-label') &&
        !button.hasAttribute('aria-expanded') &&
        !button.hasAttribute('aria-pressed') &&
        !button.querySelector('svg')
      );
      const labelledSvg = compact.filter(
        button => button.hasAttribute('aria-label') && button.querySelector('svg')
      );
      const expanders = compact.filter(
        button => button.hasAttribute('aria-expanded')
      );
      if (
        baseline &&
        baseline.count === 3 &&
        baseline.labelledSvg === 3 &&
        baseline.expanders === 2 &&
        compact.length === 4 &&
        labelledSvg.length === 3 &&
        expanders.length === 2 &&
        generated.length === 1
      ) {
        generated[0].click();
        return 'submitted';
      }
      break;
    }
    controls = controls.parentElement;
  }
  return 'no_submit_control';
})
"""
)

# Whether the submitted text is now rendered inside the post as a unit that
# was not there before. `baseline` is the count of matching units taken just
# before the submit, so an identical earlier comment cannot be mistaken for
# this one.
#
# Editable subtrees are excluded, and that exclusion is the whole difference
# between a reading and a tautology. The editor holding the draft is a
# descendant of the post, and its own `innerText` is exactly the text that was
# typed into it, so a count that includes it rises from 0 to 1 on the
# insertion alone — before any submit, and just as high when the submit
# clicked the wrong control or LinkedIn refused the comment outright. Measured
# against a live post: the count reached 1 with nothing published and no
# comment node in the DOM. Only text LinkedIn rendered back is evidence.
#
# Ancestors of an editor are excluded for the same reason and not the same way,
# which is why `closest` alone was not enough. A composer whose other controls
# are icons contributes no text of its own, so the wrapping form's `innerText`
# *is* the draft, and dropping only the editor promotes the form to the match
# the editor used to be. Caught by the empty-aria fixture, where the button
# labels carry no text; in a locale whose buttons are worded, the same markup
# hides it. Nothing is lost by the wider rule: a comment LinkedIn rendered is a
# sibling of the composer, never an ancestor of one.
COUNT_TEXT_UNITS_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + r"""
  const root = arg.root;
  if (!root || !root.isConnected) return -1;
  const EDITABLE = '[contenteditable=""], [contenteditable="true"]';
  const editable = element =>
    element.closest(EDITABLE) !== null || element.querySelector(EDITABLE) !== null;
  const elements = Array.from(root.querySelectorAll('*')).filter(
    element => visible(element) && !editable(element)
  );
  const matches = elements.filter(
    element => (element.innerText || '').trim() === arg.text
  );
  const smallest = matches.filter(
    element => !matches.some(other => other !== element && element.contains(other))
  );
  return smallest.length;
})
"""
)


def _entity_id(permalink: str) -> str | None:
    """The numeric post id carried by a canonical permalink."""
    for pattern in (_URN_ID, _SLUG_ID):
        if match := pattern.search(permalink):
            return match.group(1)
    return None


class PostActions:
    """React to, comment on and repost one LinkedIn post."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def _open_post(
        self, post: str
    ) -> tuple[str, str, dict[str, Any]] | dict[str, Any]:
        """Navigate to a post permalink and read its action signals.

        Returns ``(permalink, post_id, signals)`` when the page resolved to
        exactly one post with a usable action bar, or a refusal result. The two
        are told apart by ``isinstance(..., dict)`` at each call site, which is
        unambiguous because the success case is a tuple.
        """
        permalink = normalize_post_reference(post)
        post_id = _entity_id(permalink)
        if post_id is None:
            # Unreachable through `normalize_post_reference`, which only
            # returns the two shapes both patterns read. Kept because the
            # alternative to a refusal here is a DOM search for `:None`.
            return post_action_result(
                permalink,
                "post_not_found",
                "Could not read a post id from that permalink.",
            )

        await self._navigator._navigate_to_page(permalink)
        await self._session.check_rate_limit()

        signals = await self._read_signals(post_id)
        if not signals.get("hasMain"):
            return post_action_result(
                permalink,
                "post_unavailable",
                "That permalink did not load a post page.",
            )
        if not signals.get("hasRoot"):
            return post_action_result(
                permalink,
                "post_not_found",
                "Could not find exactly one post matching that permalink on the "
                "page. The post may be deleted, restricted to an audience this "
                "account is not in, or rendered twice as a reshare.",
            )
        if not signals.get("hasBar"):
            return post_action_result(
                permalink,
                "actions_unavailable",
                "That post rendered without a usable action bar, so this "
                "account may not be allowed to engage with it.",
            )
        return permalink, post_id, signals

    async def _read_signals(self, post_id: str) -> dict[str, Any]:
        """Read the locale-independent structural signals for one post."""
        data = await self._session.page.evaluate(POST_ACTION_SIGNALS_JS, post_id)
        return data if isinstance(data, dict) else {"hasMain": False}

    async def _pin_root(self, post_id: str) -> ElementHandle | None:
        """Pin the root post node and its controls, or ``None``.

        ``as_element`` is what distinguishes a pinned node from the program
        answering null, and it is also what makes the result an
        ``ElementHandle``, so the reaction path can hover a control inside it
        without going back through a selector that could match a comment.
        """
        handle = await self._session.page.evaluate_handle(PIN_POST_ROOT_JS, arg=post_id)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            return None
        return element

    async def react_to_post(
        self,
        post: str,
        *,
        reaction: str = "like",
    ) -> dict[str, Any]:
        """Add a reaction to a post, without ever removing an existing one."""
        if reaction not in _REACTION_ORDER:
            return post_action_result(
                "",
                "invalid_reaction",
                f"reaction must be one of {', '.join(_REACTION_ORDER)}.",
                reaction=reaction,
            )

        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, signals = opened

        if signals.get("reactPressed"):
            # Clicking a pressed toggle retracts the reaction. A caller asking
            # for a reaction never means that, so this is a success-shaped
            # no-op rather than a toggle.
            return post_action_result(
                permalink,
                "already_reacted",
                "This account has already reacted to that post. Reacting again "
                "would remove the reaction, so nothing was clicked.",
                reaction=reaction,
            )
        if not signals.get("reactPressedPresent"):
            return post_action_result(
                permalink,
                "actions_unavailable",
                "LinkedIn does not expose a locale-independent current reaction "
                "state on that post, so clicking could remove an existing reaction.",
                reaction=reaction,
            )
        if signals.get("reactDisabled"):
            return post_action_result(
                permalink,
                "actions_unavailable",
                "The reaction control is disabled on that post.",
                reaction=reaction,
            )

        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being read; nothing was clicked.",
                reaction=reaction,
            )
        try:
            if reaction == "like":
                return await self._react_default(root, permalink, post_id, reaction)
            return await self._react_specific(root, permalink, post_id, reaction)
        finally:
            await root.dispose()

    async def _react_default(
        self,
        root: ElementHandle,
        permalink: str,
        post_id: str,
        reaction: str,
    ) -> dict[str, Any]:
        """Click the reaction toggle itself, which is the default reaction."""
        outcome = await self._session.page.evaluate(
            CLICK_REACT_TOGGLE_JS, {"root": root}
        )
        if outcome != "clicked":
            return post_action_result(
                permalink,
                "already_reacted" if outcome == "already_pressed" else "react_failed",
                {
                    "already_pressed": "This account has already reacted to that post.",
                    "disabled": "The reaction control is disabled on that post.",
                    "unsupported_state": "LinkedIn does not expose the current "
                    "reaction state on that post.",
                    "unpinned": "The post changed while it was being acted on.",
                }.get(str(outcome), "Could not click the reaction control."),
                reaction=reaction,
            )
        try:
            return await self._confirm_reaction(permalink, post_id, reaction)
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _react_specific(
        self,
        root: ElementHandle,
        permalink: str,
        post_id: str,
        reaction: str,
    ) -> dict[str, Any]:
        """Open the reaction flyout and pick one reaction by index."""
        # Hovered through the pinned handle rather than a fresh selector. A
        # `main button[aria-pressed]` query would also match every comment's
        # own toggle, and the first one in the document is only the post's
        # while the post happens to render above its comments.
        toggle = (
            await root.evaluate_handle("node => node.__linkedinMcpPost.toggle")
        ).as_element()
        if toggle is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being acted on.",
                reaction=reaction,
            )
        try:
            await toggle.hover(timeout=_FLYOUT_TIMEOUT)
        except Exception:
            logger.debug("Reaction flyout hover failed", exc_info=True)
            return post_action_result(
                permalink,
                "reaction_picker_unavailable",
                "Could not open the reaction picker.",
                reaction=reaction,
            )
        finally:
            await toggle.dispose()

        flyout = await self._wait_for_flyout(root)
        if flyout is None:
            return post_action_result(
                permalink,
                "reaction_picker_unavailable",
                "The reaction picker did not open, so no reaction was set. Ask "
                'for "like" instead to click the default control directly.',
                reaction=reaction,
            )
        if flyout != len(_REACTION_ORDER):
            return post_action_result(
                permalink,
                "reaction_picker_changed",
                f"The reaction picker offered {flyout} controls rather than "
                f"{len(_REACTION_ORDER)}, so the position of "
                f'"{reaction}" cannot be trusted and nothing was clicked.',
                reaction=reaction,
            )

        clicked = await self._session.page.evaluate(
            CLICK_REACTION_JS,
            {
                "expected": len(_REACTION_ORDER),
                "index": _REACTION_ORDER.index(reaction),
                "root": root,
            },
        )
        if not clicked:
            return post_action_result(
                permalink,
                "react_failed",
                "The reaction picker changed before the reaction was clicked.",
                reaction=reaction,
            )
        return await self._confirm_reaction(permalink, post_id, reaction)

    async def _wait_for_flyout(self, root: ElementHandle) -> int | None:
        """The control count of the reaction flyout once it renders."""
        deadline = _FLYOUT_TIMEOUT / 1000
        waited = 0.0
        last: int | None = None
        while waited < deadline:
            data = await self._session.page.evaluate(
                READ_REACTION_FLYOUT_JS,
                {"expected": len(_REACTION_ORDER), "root": root},
            )
            if isinstance(data, dict):
                count = int(data.get("count") or 0)
                if count == len(_REACTION_ORDER):
                    return count
                last = count if count else last
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return last

    async def _confirm_reaction(
        self,
        permalink: str,
        post_id: str,
        reaction: str,
    ) -> dict[str, Any]:
        """Confirm a reaction by the toggle's own pressed state."""
        try:
            return await self._poll_reaction(permalink, post_id, reaction)
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _poll_reaction(
        self,
        permalink: str,
        post_id: str,
        reaction: str,
    ) -> dict[str, Any]:
        deadline = _CONFIRM_TIMEOUT / 1000
        waited = 0.0
        while waited < deadline:
            signals = await self._read_signals(post_id)
            if signals.get("reactPressed"):
                return post_action_result(
                    permalink,
                    "reacted",
                    f'Reacted with "{reaction}".',
                    acted=True,
                    retry_safe=False,
                    reaction=reaction,
                )
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return post_action_result(
            permalink,
            "react_unconfirmed",
            "The reaction was clicked but the control never reported itself as "
            "pressed. Check the post before retrying: a retry may remove a "
            "reaction that did land.",
            retry_safe=False,
            reaction=reaction,
        )

    async def comment_on_post(
        self,
        post: str,
        comment: str,
        *,
        confirm_comment: bool,
    ) -> dict[str, Any]:
        """Publish a comment on a post, gated on explicit confirmation."""
        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, signals = opened

        editor = await self._wait_for_editor(post_id)
        if editor != 1:
            return post_action_result(
                permalink,
                "comment_box_unavailable" if editor == 0 else "comment_box_ambiguous",
                "No single comment editor is available on that post. Comments "
                "may be turned off, or restricted to the author's connections."
                if editor == 0
                else f"Found {editor} comment editors on that post, so none was used.",
            )

        if not confirm_comment:
            return post_action_result(
                permalink,
                "confirmation_required",
                "Set confirm_comment=true to publish this comment. The post was "
                "loaded and a comment editor was found; nothing was typed.",
            )

        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being read; nothing was typed.",
            )
        try:
            return await self._write_and_submit(
                root,
                permalink,
                comment,
                scoped_to_root=True,
                success_status="commented",
                unconfirmed_status="comment_unconfirmed",
                noun="comment",
            )
        finally:
            await root.dispose()

    async def _wait_for_editor(self, post_id: str) -> int:
        """How many comment editors the post shows, once it has settled."""
        deadline = _EDITOR_TIMEOUT / 1000
        waited = 0.0
        count = 0
        while waited < deadline:
            signals = await self._read_signals(post_id)
            count = int(signals.get("editorCount") or 0)
            if count == 1:
                return 1
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return count

    async def repost_post(
        self,
        post: str,
        *,
        confirm_repost: bool,
        commentary: str | None = None,
    ) -> dict[str, Any]:
        """Repost a post, with or without commentary, gated on confirmation."""
        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, signals = opened

        if not signals.get("hasRepostOpener"):
            return post_action_result(
                permalink,
                "repost_unavailable",
                "That post has no repost control, so this account may not be "
                "allowed to reshare it.",
            )

        if not confirm_repost:
            return post_action_result(
                permalink,
                "confirmation_required",
                "Set confirm_repost=true to publish this repost. The post was "
                "loaded and a repost control was found; no menu was opened.",
            )

        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being read; nothing was clicked.",
            )
        try:
            opened_menu = await self._session.page.evaluate(
                OPEN_REPOST_MENU_JS, {"root": root}
            )
            if opened_menu != "clicked":
                return post_action_result(
                    permalink,
                    "repost_unavailable",
                    "Could not open the repost menu."
                    if opened_menu != "disabled"
                    else "The repost control is disabled on that post.",
                )

            items, layout = await self._wait_for_repost_menu()
            if items != _REPOST_MENU_ITEMS or layout not in _REPOST_INDEXES:
                await self._dismiss_overlay()
                return post_action_result(
                    permalink,
                    "repost_menu_changed",
                    f"The repost menu offered {items} items rather than "
                    f"{_REPOST_MENU_ITEMS}, so which one reposts immediately "
                    "cannot be trusted and nothing was clicked.",
                )

            baseline = await self._bar_counts(post_id)
            action = "commentary" if commentary is not None else "immediate"
            index = _REPOST_INDEXES[layout][action]
            clicked = await self._session.page.evaluate(
                CLICK_REPOST_MENU_ITEM_JS,
                {
                    "expected": _REPOST_MENU_ITEMS,
                    "index": index,
                    "layout": layout,
                    "root": root,
                },
            )
            if not clicked:
                await self._dismiss_overlay()
                return post_action_result(
                    permalink,
                    "repost_failed",
                    "The repost menu changed before an item was clicked.",
                )

            if commentary is None:
                return await self._confirm_repost(permalink, post_id, baseline)
            return await self._write_and_submit(
                root,
                permalink,
                commentary,
                scoped_to_root=False,
                success_status="reposted",
                unconfirmed_status="repost_unconfirmed",
                noun="repost",
                post_id=post_id,
                repost_baseline=baseline,
            )
        finally:
            await root.dispose()

    async def _wait_for_repost_menu(self) -> tuple[int, str | None]:
        """The item count and structural layout of the open repost menu."""
        deadline = _FLYOUT_TIMEOUT / 1000
        waited = 0.0
        items = 0
        layout: str | None = None
        while waited < deadline:
            data = await self._session.page.evaluate(READ_REPOST_MENU_JS)
            if isinstance(data, dict) and data.get("menus") == 1:
                items = int(data.get("items") or 0)
                candidate_layout = data.get("layout")
                layout = candidate_layout if isinstance(candidate_layout, str) else None
                if items == _REPOST_MENU_ITEMS and layout in _REPOST_INDEXES:
                    return items, layout
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return items, layout

    async def _write_and_submit(
        self,
        root: ElementHandle,
        permalink: str,
        text: str,
        *,
        scoped_to_root: bool,
        success_status: str,
        unconfirmed_status: str,
        noun: str,
        post_id: str | None = None,
        repost_baseline: list[str] | None = None,
    ) -> dict[str, Any]:
        """Type text into the one available editor and submit it.

        ``scoped_to_root`` is the difference between a comment, whose editor
        lives inside the post, and repost commentary, whose editor lives in a
        portal-mounted dialog outside it. Commentary is confirmed the same way
        a bare repost is: the source post's own count strings change.
        Matching text inside that post is not evidence — the commentary
        publishes to the actor's feed, and the permalink page still has a
        comment box that would satisfy a text count without a reshare.
        """
        page = self._session.page
        dialog: Any = None
        if not scoped_to_root:
            try:
                await page.locator(_DIALOG_EDITOR_SELECTOR).first.wait_for(
                    state="visible", timeout=_EDITOR_TIMEOUT
                )
            except Exception:
                return post_action_result(
                    permalink,
                    "repost_composer_unavailable",
                    "The repost composer did not open, so nothing was typed.",
                )
            dialog = await page.evaluate_handle(PIN_VISIBLE_DIALOG_JS)
            if dialog.as_element() is None:
                await dialog.dispose()
                dialog = None
                return post_action_result(
                    permalink,
                    "repost_composer_unavailable",
                    "Could not find exactly one visible composer dialog, so "
                    "nothing was typed.",
                )

        try:
            scope: Any = root if scoped_to_root else dialog
            baseline = 0
            if scoped_to_root:
                counted = await page.evaluate(
                    COUNT_TEXT_UNITS_JS, {"root": root, "text": text.strip()}
                )
                baseline = int(counted) if isinstance(counted, int) else 0

            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg={"scope": scope})
            status = str(await (await pinned.get_property("status")).json_value())
            editor = (await pinned.get_property("editor")).as_element()
            # The wrapper object is finished with once its two properties are read;
            # the element handle read out of it survives its disposal.
            await pinned.dispose()
            if status != "pinned" or editor is None:
                if not scoped_to_root:
                    await self._dismiss_overlay()
                return post_action_result(
                    permalink,
                    "draft_present" if status == "draft_present" else "write_failed",
                    {
                        "ambiguous_editor": f"Could not find exactly one {noun} editor.",
                        "draft_present": f"The {noun} editor already holds a draft, "
                        "which was left untouched.",
                    }.get(status, f"Could not write the {noun}."),
                )

            typed = await self._type_text(editor, text)
            if typed != "typed":
                if not scoped_to_root:
                    await self._dismiss_overlay()
                return post_action_result(
                    permalink,
                    "write_failed",
                    {
                        "not_focusable": f"The {noun} editor could not take focus.",
                        "text_mismatch": f"The {noun} editor did not hold the exact "
                        "text after typing, so it was cleared and nothing was "
                        "submitted.",
                    }.get(typed, f"Could not write the {noun}."),
                )

            if scoped_to_root:
                submitted = await self._submit_editor(scope, text)
            else:
                baseline_counts = repost_baseline or []
                submitted = await self._submit_editor(scope, text)
            if submitted != "submitted":
                # Nothing was clicked on either of these two, so the typed text is
                # this server's to take back, and taking it back is what keeps the
                # `retry_safe` below true: a draft left behind would meet the next
                # attempt as `draft_present` and refuse it. The other statuses
                # describe an editor that is no longer identifiable as the one that
                # was typed into, and clearing something unidentified is worse than
                # leaving it.
                if submitted in ("no_submit_control", "ambiguous_submit"):
                    await page.evaluate(CLEAR_EDITOR_JS, {"editor": editor})
                if not scoped_to_root:
                    await self._dismiss_overlay()
                return post_action_result(
                    permalink,
                    "submit_unavailable",
                    {
                        "ambiguous_editor": f"Could not find exactly one {noun} editor "
                        "at submit time.",
                        "not_owned": f"The {noun} editor no longer held this text.",
                        "ambiguous_submit": "Found more than one enabled submit "
                        f"control for the {noun}, so none was clicked.",
                        "no_submit_control": f"The {noun} text was typed but no submit "
                        "control ever appeared, so nothing was clicked and the text "
                        "was removed from the editor.",
                    }.get(str(submitted), f"Could not submit the {noun}."),
                    retry_safe=True,
                )

            if scoped_to_root:
                return await self._confirm_text(
                    root,
                    permalink,
                    text,
                    baseline=baseline,
                    success_status=success_status,
                    unconfirmed_status=unconfirmed_status,
                    noun=noun,
                )
            return await self._confirm_repost(permalink, str(post_id), baseline_counts)
        finally:
            if dialog is not None:
                await dialog.dispose()

    async def _type_text(self, editor: ElementHandle, text: str) -> str:
        """Type into a pinned editor with real key events.

        The click is a real mouse click rather than a scripted ``focus()``
        because the editor is activated by the event, and the characters arrive
        as key events because the submit control is drawn by a handler listening
        for them. A newline is sent as ``Shift+Enter``: a bare ``Enter`` in a
        comment box is a submit on some layouts, which would publish partial
        text mid-typing and defeat every guard after this point.

        The text is read back and compared before anything can be submitted,
        which is what catches an autocomplete popup turning a typed ``@name``
        into a mention or eating the keystrokes that follow it.
        """
        page = self._session.page
        await editor.click()
        if not await editor.evaluate("element => element === document.activeElement"):
            return "not_focusable"

        for index, line in enumerate(text.split("\n")):
            if index:
                await page.keyboard.press("Shift+Enter")
            if line:
                await page.keyboard.type(line, delay=_TYPE_DELAY)

        actual = str(await editor.evaluate("element => element.innerText || ''"))
        if actual.replace("\r\n", "\n").strip() != text.strip():
            await page.evaluate(CLEAR_EDITOR_JS, {"editor": editor})
            return "text_mismatch"

        await page.evaluate(OWN_EDITOR_JS, {"editor": editor, "text": text})
        return "typed"

    async def _submit_editor(self, scope: Any, text: str) -> str:
        """Click the editor's submit control once it exists.

        The control is absent until LinkedIn has processed the typed text, so a
        single read would report ``no_submit_control`` for a comment that is
        about to become submittable. Only that one status is retried: an
        ambiguous editor or a lost ownership marker will not improve by waiting,
        and re-reading them would hide a page that changed underneath.
        """
        deadline = _SUBMIT_TIMEOUT / 1000
        waited = 0.0
        result = "no_submit_control"
        while waited < deadline:
            result = str(
                await self._session.page.evaluate(
                    SUBMIT_EDITOR_JS, {"scope": scope, "text": text}
                )
            )
            if result != "no_submit_control":
                return result
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return result

    async def _confirm_text(
        self,
        root: ElementHandle,
        permalink: str,
        text: str,
        *,
        baseline: int,
        success_status: str,
        unconfirmed_status: str,
        noun: str,
    ) -> dict[str, Any]:
        """Confirm submitted text by a new matching unit inside the post."""
        try:
            return await self._poll_text(
                root,
                permalink,
                text,
                baseline=baseline,
                success_status=success_status,
                unconfirmed_status=unconfirmed_status,
                noun=noun,
            )
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _poll_text(
        self,
        root: ElementHandle,
        permalink: str,
        text: str,
        *,
        baseline: int,
        success_status: str,
        unconfirmed_status: str,
        noun: str,
    ) -> dict[str, Any]:
        deadline = _CONFIRM_TIMEOUT / 1000
        waited = 0.0
        while waited < deadline:
            count = await self._session.page.evaluate(
                COUNT_TEXT_UNITS_JS, {"root": root, "text": text.strip()}
            )
            if isinstance(count, int) and count > baseline:
                return post_action_result(
                    permalink,
                    success_status,
                    f"The {noun} was published and is rendered on the post.",
                    acted=True,
                    retry_safe=False,
                )
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return post_action_result(
            permalink,
            unconfirmed_status,
            f"The {noun} was submitted but never appeared on the post. Check the "
            f"post before retrying, as a retry may publish it twice.",
            retry_safe=False,
        )

    async def _bar_counts(self, post_id: str) -> list[str]:
        """The action-bar control texts, captured immediately before a write."""
        return list((await self._read_signals(post_id)).get("counts") or [])

    async def _confirm_repost(
        self,
        permalink: str,
        post_id: str,
        baseline: list[str],
    ) -> dict[str, Any]:
        """Confirm a repost by the post's own control text changing.

        Used for both the immediate reshare and the commentary composer, because
        the published commentary lives on the actor's feed rather than inside
        the source post.

        The comparison is string inequality against the strings read before
        the click, never a parsed number: a count renders with locale digit
        grouping and an abbreviation suffix, so reading a value out of one
        would be the text dependency this project refuses, while noticing
        that it is no longer the same string is not.
        """
        try:
            return await self._poll_repost(permalink, post_id, baseline)
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _poll_repost(
        self,
        permalink: str,
        post_id: str,
        baseline: list[str],
    ) -> dict[str, Any]:
        deadline = _CONFIRM_TIMEOUT / 1000
        waited = 0.0
        while waited < deadline:
            signals = await self._read_signals(post_id)
            counts = signals.get("counts")
            if counts and counts != baseline:
                return post_action_result(
                    permalink,
                    "reposted",
                    "The repost was published and the post's own counts changed.",
                    acted=True,
                    retry_safe=False,
                )
            await asyncio.sleep(_CONFIRM_POLL)
            waited += _CONFIRM_POLL
        return post_action_result(
            permalink,
            "repost_unconfirmed",
            "The repost was clicked but the post's counts never changed. Check "
            "your own activity before retrying, as a retry may repost twice.",
            retry_safe=False,
        )

    async def _dismiss_overlay(self) -> None:
        """Close any open menu or dialog with Escape."""
        try:
            await self._session.page.keyboard.press("Escape")
            await self._session.page.wait_for_selector(
                _DIALOG_SELECTOR, state="hidden", timeout=2000
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Overlay dismissal failed", exc_info=True)
