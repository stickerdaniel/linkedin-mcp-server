"""Like and comment on a verified post through its rendered controls.

Adapted from upstream PR #1027 under the repository's Apache-2.0 license.
See docs/engagement-provenance.md for the source revision and integration changes.
Synthetic DOM tests verify the algorithm; availability depends on the page layout.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import asyncio
import logging
import json
import re
import anyio

from patchright.async_api import ElementHandle

from linkedin_mcp_server.core.destination import (
    LINKEDIN_LANDING_JS,
    LINKEDIN_HOST_PATTERN,
)

from linkedin_mcp_server.linkedin.contracts import (
    POST_ACTION_INTERRUPTED_WARNING,
    before_the_reply_deadline,
    post_action_result,
    refuse_invalid_post_text,
)
from linkedin_mcp_server.linkedin.identifiers import (
    normalize_post_reference,
    normalize_actor_reference,
    normalize_comment_reference,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.post_actors import (
    ACTOR_HELPERS_JS,
    READ_ACTOR_IDENTITY_JS,
    OPEN_ACTOR_PICKER_JS,
    SELECT_ACTOR_JS,
    SAVE_ACTOR_JS,
    PIN_ACTOR_JS,
)
from linkedin_mcp_server.linkedin.post_mentions import (
    READ_POST_AUTHOR_JS,
    SELECT_AUTHOR_MENTION_JS,
    PIN_AUTHOR_MENTION_JS,
    MENTION_STILL_MATCHES_JS,
)
from linkedin_mcp_server.linkedin.post_comments import (
    COMMENT_HELPERS_JS,
    CLEAR_PREPARED_REPLY_JS as _CLEAR_PREPARED_REPLY_JS,
    PARENT_READINESS_JS,
    READ_COMMENTS_JS,
    PIN_PARENT_COMMENT_JS,
    OPEN_REPLY_EDITOR_JS,
    PIN_REPLY_CONTEXT_JS,
)
from linkedin_mcp_server.linkedin.session import PageSession

logger = logging.getLogger(__name__)

_HANDLE_ORIGIN_GUARD_JS = (
    f"const onLinkedIn = {LINKEDIN_LANDING_JS};\n"
    f"if (!onLinkedIn(location.href, {json.dumps(LINKEDIN_HOST_PATTERN)})) return null;\n"
)

# The band a social action bar's button count falls in. The bar holds react,
# comment, repost and send, so three is the floor once a layout drops one and
# eight is loose enough for an overflow control. The band exists to stop the
# ancestor walk from climbing out of the bar and into the comments container,
# which also holds many buttons and one `aria-pressed` toggle per comment.
_BAR_BUTTONS_MIN = 3
_BAR_BUTTONS_MAX = 8

# The SDUI shell precedes its action row. Poll only reads during this window;
# a missing or ambiguous target never turns into a publication retry.
_POST_READY_TIMEOUT = 10.0
_EDITOR_TIMEOUT = 5000
_EDITOR_CLEANUP_TIMEOUT_SECONDS = 1.0
# How long a submitted comment has to show up in the DOM.
_CONFIRM_TIMEOUT = 12000
_CONFIRM_POLL = 0.25

# Per-character delay while typing into an editor, and how long the submit
# control gets to render once the text is in. The delay is not politeness: the
# editor's submit button is drawn by a handler reacting to input, and that
# handler is the thing being waited for here.
_TYPE_DELAY = 12
_SUBMIT_TIMEOUT = 4000

_EDITOR_SELECTOR = '[role="textbox"][contenteditable="true"]'

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

  const detailPath = new RegExp(
    '^/feed/update/urn:li:(?:ugcPost|share|activity):' + digits + '/?$'
  );
  if (
    detailPath.test(window.location.pathname) &&
    document.querySelector('[data-sdui-screen="com.linkedin.sdui.flagshipnav.feed.UpdateDetail"]') &&
    findActionBar(main) !== null
  ) {
    const row = findActionBar(main).bar.closest('[role="listitem"]');
    return row && main.contains(row) ? row : null;
  }

  return null;
}
"""
)

# SDUI exposes reaction state only through an English aria-label on the
# observed 2026-10-06 detail page. Other locales require aria-pressed; unknown
# labels refuse rather than toggle an existing reaction off.
_REACTION_STATE_FN_JS = r"""
function reactionState(toggle) {
  if (!toggle) return null;
  if (toggle.hasAttribute('aria-pressed')) {
    const state = toggle.getAttribute('aria-pressed');
    return state === 'true' ? true : state === 'false' ? false : null;
  }
  const locale = document.documentElement.lang.toLowerCase().split('-')[0];
  const labels = {
    en: {
      none: 'Reaction button state: no reaction',
      active: /^Reaction button state: (?:like|celebrate|support|love|insightful|funny)$/i
    }
  }[locale];
  if (!labels) return null;
  const label = toggle.getAttribute('aria-label') || '';
  if (label === labels.none) return false;
  return labels.active.test(label) ? true : null;
}
"""

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
        const sduiShape = buttons[0] === toggle && !toggle.hasAttribute('aria-pressed') &&
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
_POST_IDENTITY_FN_JS = (
    r"""
function postIdentityMatches(root) {
  const pin = root?.__linkedinMcpPost;
  if (!root?.isConnected || !pin || pin.route !== location.href ||
      findPostRoot(pin.postId) !== root ||
      !pin.rootIdentity?.every(([name, value]) => root.getAttribute(name) === value)) return false;
  const attributes = """
    + repr(list(_URN_ATTRIBUTES)).replace("'", '"')
    + r""";
  return !attributes.some(name => {
    const value = root.getAttribute(name)?.trim();
    return value && /^urn:li:(?:ugcPost|share|activity):[0-9]+$/.test(value) &&
      !value.endsWith(':' + pin.postId);
  });
}
"""
)

# Reply editors are separate rows; their original post, not the editor row,
# owns the publication. Receipts may outlive the editor but never that post.
_WRITE_SCOPE_IDENTITY_FN_JS = (
    _VISIBLE_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _POST_IDENTITY_FN_JS
    + r"""
function writeScopeIdentityMatches(scope) {
  const pin = scope?.__linkedinMcpPost;
  const post = pin?.replyRange ? pin.actorRoot : scope;
  return Boolean(pin && post && (!pin.replyRange || post.__linkedinMcpPost === pin.actorPin) &&
    postIdentityMatches(post));
}
"""
)

# Clearing LinkedIn's automatic reply mention also modifies a draft.
CLEAR_PREPARED_REPLY_JS = (
    "scope => {"
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + "if (!writeScopeIdentityMatches(scope)) return false;"
    + f"return ({_CLEAR_PREPARED_REPLY_JS})(scope);"
    + "}"
)

POST_ACTION_SIGNALS_JS = (
    r"""
((arg) => {
"""
    + _VISIBLE_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + _REACTION_STATE_FN_JS
    + _POST_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + r"""
  const postId = typeof arg === 'string' ? arg : arg.postId;
  const main = document.querySelector('main');
  if (!main) return {hasMain: false};
  const root = typeof arg === 'string' ? findPostRoot(postId) : arg.root;
  if (!root) return {hasMain: true, hasRoot: false};
  let found;
  if (typeof arg !== 'string') {
    // After dispatch, a fresh query can observe another actor or remounted
    // post. A reaction receipt belongs only to this pinned actor and toggle.
    const pin = root.__linkedinMcpPost;
    if (!postIdentityMatches(root) || !actorStillMatches(root) || !pin?.bar?.isConnected ||
        !pin.toggle?.isConnected || !root.contains(pin.bar) || !pin.bar.contains(pin.toggle)) return {hasMain: true, hasRoot: false};
    found = {bar: pin.bar, toggle: pin.toggle};
  } else found = findActionBar(root);
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
    reactPressedPresent: found ? reactionState(found.toggle) !== null : false,
    reactPressed: found ? reactionState(found.toggle) : null,
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

# Pin the root post and its controls on the node itself, so every later step
# is scoped to one subtree that cannot drift. Same technique as the message
# composer's `__linkedinMcpComposer`, and for the same reason: a re-query
# between steps can land on a different post after the feed rerenders.
PIN_POST_ROOT_JS = (
    r"""
((postId) => {
"""
    + _HANDLE_ORIGIN_GUARD_JS
    + _VISIBLE_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + r"""
  const root = findPostRoot(postId);
  if (!root) return null;
  const found = findActionBar(root);
  if (!found) return null;
  const column = root.closest('[data-component-type="LazyColumn"][data-testid*="commentList"]');
  const confirmationScope = column && findActionBar(column)?.bar === found.bar ? column : root;
  const openers = Array.from(
    found.bar.querySelectorAll('button[aria-expanded]')
  ).filter(visible);
  root.__linkedinMcpPost = {
    postId: String(postId),
    rootIdentity: """
    + repr(list(_URN_ATTRIBUTES)).replace("'", '"')
    + r""".map(name => [name, root.getAttribute(name)]).filter(([, value]) =>
      value && /^urn:li:(?:ugcPost|share|activity):[0-9]+$/.test(value.trim())),
    route: window.location.href,
    bar: found.bar,
    toggle: found.toggle,
    confirmationScope,
    opener: openers.length === 1
      ? openers[0]
      : openers.length === 2
        ? openers[1]
        : null,
  };
  return root;
})
"""
)

# Saving the acting identity can replace the entire post row. Reacquire only
# that original post in that original document, then verify its selected actor.
REFRESH_POST_ROOT_JS = (
    "({previous}) => {"
    + _HANDLE_ORIGIN_GUARD_JS
    + "const pin = previous?.__linkedinMcpPost;"
    + "if (!pin || pin.route !== location.href) return null;"
    + f"return ({PIN_POST_ROOT_JS})(pin.postId);"
    + "}"
)

# Actor Save can replace just the bar after identity verification. Before a
# write, rebind controls only inside the same connected post and acting identity.
# Never use this after dispatch: receipts must retain the clicked toggle.
REFRESH_REACTION_CONTROLS_JS = (
    "root => {"
    + _HANDLE_ORIGIN_GUARD_JS
    + _VISIBLE_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _POST_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + r"""
  const pin = root?.__linkedinMcpPost;
  if (!postIdentityMatches(root) || !actorStillMatches(root)) return false;
  if (pin.bar?.isConnected && pin.toggle?.isConnected &&
      root.contains(pin.bar) && pin.bar.contains(pin.toggle)) return true;
  const found = findActionBar(root);
  if (!found) return false;
  pin.bar = found.bar;
  pin.toggle = found.toggle;
  return true;
}
"""
)

# Click the pinned reaction toggle. Re-verifies the pin and the pressed state
# inside the same tick as the click: a toggle already pressed would *remove*
# the reaction, which is the one way this flow could undo something the
# account meant to keep.
CLICK_REACT_TOGGLE_JS = (
    r"""
((arg) => {
"""
    + _REACTION_STATE_FN_JS
    + _VISIBLE_FN_JS
    + _FIND_ACTION_BAR_FN_JS
    + _FIND_POST_ROOT_FN_JS
    + _POST_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + r"""
  const pinned = arg.root?.__linkedinMcpPost;
  if (!postIdentityMatches(arg.root)) return 'unpinned';
  if (window.location.href !== pinned.route) return 'unpinned';
  if (!actorStillMatches(arg.root)) return 'actor_changed';
  const toggle = pinned.toggle;
  if (!toggle.isConnected || !arg.root.contains(toggle)) return 'unpinned';
  if (
    toggle.disabled ||
    (toggle.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
  ) {
    return 'disabled';
  }
  if (reactionState(toggle) === null) return 'unsupported_state';
  if (reactionState(toggle) === true) {
    return 'already_pressed';
  }
  toggle.click();
  return 'clicked';
})
"""
)

# Pin an empty editor inside the selected post. Its initial controls identify
# the submit button added by LinkedIn after keyboard input, without reading labels.
PIN_EDITOR_JS = (
    r"""
((arg) => {
"""
    + _HANDLE_ORIGIN_GUARD_JS
    + _VISIBLE_FN_JS
    + r"""
  const scope = arg.scope || document;
  const editors = Array.from(
    scope.querySelectorAll('[role="textbox"][contenteditable="true"]')
  ).filter(visible);
  if (editors.length !== 1) return {status: 'ambiguous_editor', editor: null};
  const editor = editors[0];
  editor.__linkedinMcpScope = scope;
  const prepared = editor.__linkedinMcpPreparedMention;
  const ownedMention = arg.allowPreparedMention && scope.__linkedinMcpPost?.replyEditor === editor &&
    prepared?.token.isConnected && editor.contains(prepared.token) &&
    prepared.token.getAttribute('data-type') === 'mention' &&
    prepared.token.getAttribute('contenteditable') === 'false' &&
    prepared.token.innerText.trim() === prepared.name && editor.innerText.trim() === prepared.name;
  if ((editor.innerText || '').trim() && !ownedMention) return {status: 'draft_present', editor: null};
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
CAN_TYPE_EDITOR_JS = (
    "(editor) => {"
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + r"""
    const root = editor?.__linkedinMcpScope;
    return Boolean(editor?.isConnected && root?.contains(editor) &&
      writeScopeIdentityMatches(root) && actorStillMatches(root));
    }
"""
)

# Mention selection and caret pinning modify the draft too. Keep their identity
# check in the same browser evaluation as the mutation.
GUARDED_SELECT_AUTHOR_MENTION_JS = (
    "arg => {"
    + f"if (!({CAN_TYPE_EDITOR_JS})(arg.editor)) return 'unavailable';"
    + f"return ({SELECT_AUTHOR_MENTION_JS})(arg);"
    + "}"
)
GUARDED_PIN_AUTHOR_MENTION_JS = (
    "arg => {"
    + f"if (!({CAN_TYPE_EDITOR_JS})(arg.editor)) return false;"
    + f"return ({PIN_AUTHOR_MENTION_JS})(arg);"
    + "}"
)

# SDUI can render a separator image and trailing BR after the rich mention.
# Accept that layout newline only while the exact pinned draft still belongs to us.
CHECK_MENTION_PREFIX_JS = (
    "({editor, expected}) => {"
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + MENTION_STILL_MATCHES_JS
    + r"""
  const root = editor?.__linkedinMcpScope;
  if (!editor?.isConnected || !root?.contains(editor) || !writeScopeIdentityMatches(root) || !actorStillMatches(root) ||
      !editor.__linkedinMcpMention || !mentionStillMatches(editor) ||
      editor.innerText !== expected) return false;
  return true;
}
"""
)

OWN_EDITOR_JS = r"""
((arg) => {
  arg.editor.__linkedinMcpOwnedText = arg.text;
  return true;
})
"""

# Clear only unchanged, owned text after a refused submit. Unexpected edits
# belong to the caller and must survive a refusal.
CLEAR_EDITOR_JS = (
    "((arg) => {"
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + r"""
  const editor = arg.editor;
  const scope = editor?.__linkedinMcpScope;
  const owned = editor?.__linkedinMcpOwnedText;
  if (!editor?.isConnected || !scope?.contains(editor) || !writeScopeIdentityMatches(scope) || !actorStillMatches(scope) ||
      typeof owned !== 'string' ||
      editor.innerText.replace(/\r\n/g, '\n').replace(/\u00a0/g, ' ').trim() !== owned.trim()) return false;
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
)

# Submit the pinned editor by clicking one structurally identified control.
#
# The original composer exposes exactly one enabled `type="submit"`. The SDUI
# composer instead starts with three labelled SVG controls, then appends one
# unlabeled, non-SVG `type="button"` after real key events. That exact 3-to-4
# transition identifies the new submit control without reading a label. The
# submit rule is never relaxed to "the
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
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + ACTOR_HELPERS_JS
    + MENTION_STILL_MATCHES_JS
    + r"""
  const scope = arg.scope || document;
  const pin = scope.__linkedinMcpPost;
  if (!pin || !scope.isConnected || !writeScopeIdentityMatches(scope)) return 'not_owned';
  if (!actorStillMatches(scope)) return 'actor_changed';
  const editors = Array.from(
    scope.querySelectorAll('[role="textbox"][contenteditable="true"]')
  ).filter(visible);
  if (editors.length !== 1) return 'ambiguous_editor';
  const editor = editors[0];
  if (!mentionStillMatches(editor)) return 'mention_changed';
  if (editor.__linkedinMcpOwnedText !== arg.text ||
      (editor.innerText || '').replace(/\r\n/g, '\n').replace(/\u00a0/g, ' ').trim() !== arg.text.trim()) return 'not_owned';
  if (pin.replyRange) {
    // The reply editor arrives with an automatic mention and an existing
    // submit control, so the post composer's 3-to-4 transition cannot identify
    // it. This observed English label is scoped to the verified new composer.
    const labels = {en: 'Reply'};
    const label = labels[document.documentElement.lang.toLowerCase().split('-')[0]];
    if (!label) return 'no_submit_control';
    const replies = Array.from(scope.querySelectorAll('button')).filter(button =>
      visible(button) && button.innerText.trim() === label && !button.disabled &&
      button.getAttribute('aria-disabled') !== 'true');
    if (replies.length !== 1) return replies.length ? 'ambiguous_submit' : 'no_submit_control';
    replies[0].click();
    return 'submitted';
  }
  let owner = editor.parentElement;
  while (owner && !owner.matches('form, dialog, [role="dialog"]')) {
    owner = owner.parentElement;
  }
  owner = owner && scope.contains(owner) ? owner : scope;
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

# A receipt needs a unique rendered comment URN, the exact body, and the
# selected actor's linked identity. Arbitrary matching text (including a
# different member's reply) is not acknowledgment. Pin all observed URNs before
# typing so edits to existing comments cannot confirm a new publication.
COUNT_TEXT_UNITS_JS = (
    r"""
((arg) => {
"""
    + _WRITE_SCOPE_IDENTITY_FN_JS
    + COMMENT_HELPERS_JS
    + r"""
  const pin = arg.root?.__linkedinMcpPost;
  if (!writeScopeIdentityMatches(arg.root)) return -1;
  const root = pin?.confirmationScope;
  const replyNodes = pin?.replyRange ? replyRangeNodes(pin, false) : null;
  if (pin?.replyRange && !replyNodes) return -1;
  if (!pin?.replyRange && (!arg.root?.isConnected || !root?.isConnected || !root.contains(arg.root) ||
      pin.route !== location.href)) return -1;
  const EDITABLE = '[contenteditable=""], [contenteditable="true"]';
  const editable = element =>
    element.closest(EDITABLE) !== null || element.querySelector(EDITABLE) !== null;
  if (!pin.actor) return -1;
  const elements = replyNodes ? replyNodes.flatMap(node => [node, ...node.querySelectorAll('*')]) : Array.from(root.querySelectorAll('*'));
  const components = elements.filter(node => visible(node) && !editable(node) && commentIdentity(node));
  if (arg.captureBaseline) {
    pin.receiptBaseline = new Set(components.map(commentIdentity));
    return 0;
  }
  return components.filter(node => {
    const urn = commentIdentity(node);
    if (pin.receiptBaseline?.has(urn) || components.filter(other => commentIdentity(other) === urn).length !== 1) return false;
    const body = commentBody(node, urn);
    const author = body && commentAuthor(body);
    const text = body && commentText(body);
    return author?.path === pin.actor.path.replace(/\/+$/, '') &&
      author.avatar === pin.actor.avatar && typeof text === 'string' &&
      text.replace(/\r\n/g, '\n').replace(/\u00a0/g, ' ').trim() === arg.text;
  }).length;
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
    """React to and comment on one LinkedIn post."""

    def __init__(self, session: PageSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def _resolve_actor(self, actor: str) -> dict[str, str] | None:
        """Bind the requested URL to its rendered name and unique avatar."""
        url = "https://www.linkedin.com" + actor
        if actor.startswith("/company/"):
            url += "?skipRedirect=true"
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        try:
            await self._session.page.wait_for_selector(
                "main h1, main h2", timeout=_EDITOR_TIMEOUT
            )
        except Exception:
            return None
        identity = await self._session.run_on_linkedin(READ_ACTOR_IDENTITY_JS, actor)
        return identity if isinstance(identity, dict) else None

    async def _select_actor(
        self, root: ElementHandle, actor: dict[str, str]
    ) -> ElementHandle | None:
        """Select a uniquely mapped actor and verify the resulting control."""
        if not await self._session.run_on_linkedin(OPEN_ACTOR_PICKER_JS, root):
            return None
        saved = False
        outcome = "pending"
        for _ in range(20):
            outcome = await self._session.run_on_linkedin(
                SELECT_ACTOR_JS, {"actor": actor}
            )
            if outcome != "pending":
                break
            await asyncio.sleep(_CONFIRM_POLL)
        if outcome == "save_required":
            for _ in range(20):
                if await self._session.run_on_linkedin(SAVE_ACTOR_JS, {"actor": actor}):
                    outcome = "selected"
                    saved = True
                    break
                await asyncio.sleep(_CONFIRM_POLL)
        if outcome != "selected":
            return None
        for _ in range(20):
            if await self._session.run_on_linkedin(
                PIN_ACTOR_JS, {"root": root, "actor": actor}
            ):
                return root
            if saved:
                refreshed = await self._session.page.evaluate_handle(
                    REFRESH_POST_ROOT_JS, arg={"previous": root}
                )
                selected = refreshed.as_element()
                if selected is not None and await self._session.run_on_linkedin(
                    PIN_ACTOR_JS, {"root": selected, "actor": actor}
                ):
                    await root.dispose()
                    return selected
                await refreshed.dispose()
            await asyncio.sleep(_CONFIRM_POLL)
        return None

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
        deadline = self._session.monotonic() + _POST_READY_TIMEOUT
        while not all(signals.get(key) for key in ("hasMain", "hasRoot", "hasBar")):
            remaining = deadline - self._session.monotonic()
            if remaining <= 0:
                break
            await self._session.delay(min(_CONFIRM_POLL, remaining))
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

    async def _read_signals(
        self, post_id: str, *, root: ElementHandle | None = None
    ) -> dict[str, Any]:
        """Read structural signals and the supported reaction-state label."""
        argument: Any = post_id if root is None else {"postId": post_id, "root": root}
        data = await self._session.run_on_linkedin(POST_ACTION_SIGNALS_JS, argument)
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
        actor: str,
        reaction: str = "like",
        confirm_reaction: bool = False,
    ) -> dict[str, Any]:
        """Add a reaction to a post, without ever removing an existing one."""
        actor = normalize_actor_reference(actor)
        post = normalize_post_reference(post)
        if reaction != "like":
            return post_action_result(
                "",
                "invalid_reaction",
                "Only the like reaction is supported.",
                reaction=reaction,
            )

        identity = await self._resolve_actor(actor)
        if identity is None:
            return post_action_result(
                "",
                "actor_unavailable",
                "Could not verify that actor's profile and avatar. Nothing was published.",
            )

        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, signals = opened

        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being read; nothing was clicked.",
                reaction=reaction,
            )
        try:
            selected = await self._select_actor(root, identity)
            if selected is None:
                return post_action_result(
                    permalink,
                    "actor_unavailable",
                    "The requested actor could not be selected and verified. Nothing was published.",
                )
            root = selected
            signals = await self._read_signals(post_id)
            if not await self._session.run_on_linkedin(
                REFRESH_REACTION_CONTROLS_JS, root
            ):
                return post_action_result(
                    permalink,
                    "react_failed",
                    "The post or actor changed before dispatch. Nothing was clicked.",
                    reaction=reaction,
                )
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

            if not confirm_reaction:
                return post_action_result(
                    permalink,
                    "confirmation_required",
                    "Set confirm_reaction=true to add this reaction. No reaction was published.",
                    reaction=reaction,
                )

            return await self._react_default(root, permalink, post_id, reaction)
        except Exception:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING, exc_info=True)
            return post_action_result(
                permalink,
                "react_unconfirmed",
                "The reaction may have been applied. Check the post before retrying.",
                retry_safe=False,
                reaction=reaction,
            )
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise
        finally:
            try:
                await root.dispose()
            except Exception:
                logger.debug("Could not release post handle", exc_info=True)

    async def _react_default(
        self,
        root: ElementHandle,
        permalink: str,
        post_id: str,
        reaction: str,
    ) -> dict[str, Any]:
        """Click the reaction toggle itself, which is the default reaction."""
        outcome = await self._session.run_on_linkedin(
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
            return await self._confirm_reaction(permalink, post_id, reaction, root=root)
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _confirm_reaction(
        self,
        permalink: str,
        post_id: str,
        reaction: str,
        *,
        root: ElementHandle,
    ) -> dict[str, Any]:
        """Confirm a reaction by the toggle's own pressed state."""
        try:
            return await self._poll_reaction(permalink, post_id, reaction, root=root)
        except BaseException:
            logger.warning(POST_ACTION_INTERRUPTED_WARNING)
            raise

    async def _poll_reaction(
        self,
        permalink: str,
        post_id: str,
        reaction: str,
        *,
        root: ElementHandle,
    ) -> dict[str, Any]:
        deadline = _CONFIRM_TIMEOUT / 1000
        waited = 0.0
        while waited < deadline:
            signals = await self._read_signals(post_id, root=root)
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
        actor: str,
        confirm_comment: bool = False,
        mention_author: bool = False,
    ) -> dict[str, Any]:
        """Publish a comment on a post, gated on explicit confirmation."""
        actor = normalize_actor_reference(actor)
        post = normalize_post_reference(post)
        refusal = refuse_invalid_post_text(
            normalize_post_reference(post), comment, field="comment"
        )
        if refusal is not None:
            return refusal
        identity = await self._resolve_actor(actor)
        if identity is None:
            return post_action_result(
                "",
                "actor_unavailable",
                "Could not verify that actor's profile and avatar. Nothing was published.",
            )
        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, signals = opened

        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink,
                "post_not_found",
                "The post changed while it was being read; nothing was typed.",
            )
        try:
            selected = await self._select_actor(root, identity)
            if selected is None:
                return post_action_result(
                    permalink,
                    "actor_unavailable",
                    "The requested actor could not be selected and verified. Nothing was published.",
                )
            root = selected
            editor = await self._wait_for_editor(post_id)
            if editor != 1:
                return post_action_result(
                    permalink,
                    "comment_box_unavailable"
                    if editor == 0
                    else "comment_box_ambiguous",
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

            return await self._write_and_submit(
                root,
                permalink,
                comment,
                success_status="commented",
                unconfirmed_status="comment_unconfirmed",
                noun="comment",
                mention_author=mention_author,
            )
        finally:
            try:
                await root.dispose()
            except Exception:
                logger.debug("Could not release post handle", exc_info=True)

    async def get_post_comments(
        self, post: str, *, max_comments: int = 20
    ) -> dict[str, Any]:
        """Read currently rendered comments and their exact component URNs."""
        post = normalize_post_reference(post)
        if (
            isinstance(max_comments, bool)
            or not isinstance(max_comments, int)
            or not 1 <= max_comments <= 50
        ):
            raise ValueError("max_comments must be an integer from 1 to 50")
        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return {**opened, "sections": {"comments": ""}, "comments": []}
        permalink, post_id, _signals = opened
        root = await self._pin_root(post_id)
        if root is None:
            return {"url": permalink, "sections": {"comments": ""}, "comments": []}
        try:
            deadline = self._session.monotonic() + _EDITOR_TIMEOUT / 1000
            while True:
                rows = await self._session.run_on_linkedin(
                    READ_COMMENTS_JS, {"post": root, "limit": max_comments}
                )
                if rows or rows is None or self._session.monotonic() >= deadline:
                    break
                await asyncio.sleep(_CONFIRM_POLL)
            rows = rows if isinstance(rows, list) else []
            return {
                "url": permalink,
                "sections": {"comments": "\n\n".join(row["text"] for row in rows)},
                "comments": rows,
                "references": {
                    "comments": [
                        {
                            "kind": "comment",
                            "url": urlsplit(permalink).path,
                            "value": row["reference"],
                            "text": row["text"],
                            "context": "rendered comment URN; URL identifies the post",
                        }
                        for row in rows
                    ]
                },
                "coverage": "currently rendered comments only",
            }
        finally:
            await root.dispose()

    async def reply_to_comment(
        self,
        post: str,
        comment_reference: str,
        reply: str,
        *,
        actor: str,
        confirm_reply: bool = False,
        mention_parent_author: bool = False,
    ) -> dict[str, Any]:
        """Reply to an exact rendered comment after actor and editor verification."""
        post = normalize_post_reference(post)
        reference = normalize_comment_reference(comment_reference)
        actor = normalize_actor_reference(actor)
        refusal = refuse_invalid_post_text(post, reply, field="reply")
        if refusal is not None:
            return refusal
        identity = await self._resolve_actor(actor)
        if identity is None:
            return post_action_result(
                post, "actor_unavailable", "The requested actor could not be verified."
            )
        opened = await self._open_post(post)
        if isinstance(opened, dict):
            return opened
        permalink, post_id, _signals = opened
        root = await self._pin_root(post_id)
        if root is None:
            return post_action_result(
                permalink, "post_not_found", "The requested post could not be pinned."
            )
        parent = scope = None
        try:
            selected = await self._select_actor(root, identity)
            if selected is None:
                return post_action_result(
                    permalink,
                    "actor_unavailable",
                    "The requested actor could not be selected and verified.",
                )
            root = selected
            deadline = self._session.monotonic() + _POST_READY_TIMEOUT
            while True:
                readiness = await self._session.run_on_linkedin(
                    PARENT_READINESS_JS, {"post": root, "reference": reference}
                )
                if readiness != "missing" or self._session.monotonic() >= deadline:
                    break
                await asyncio.sleep(_CONFIRM_POLL)
            handle = await self._session.page.evaluate_handle(
                PIN_PARENT_COMMENT_JS, arg={"post": root, "reference": reference}
            )
            parent = handle.as_element()
            if parent is None:
                await handle.dispose()
                return post_action_result(
                    permalink,
                    "comment_not_found",
                    "No unique rendered comment and author matched that exact reference.",
                )
            opened_reply = await self._session.run_on_linkedin(
                OPEN_REPLY_EDITOR_JS, parent
            )
            if opened_reply != "opened":
                return post_action_result(
                    permalink,
                    "reply_editor_unavailable",
                    "The parent comment could not open a uniquely owned reply editor.",
                )
            deadline = self._session.monotonic() + _EDITOR_TIMEOUT / 1000
            while self._session.monotonic() < deadline:
                handle = await self._session.page.evaluate_handle(
                    PIN_REPLY_CONTEXT_JS, arg=parent
                )
                scope = handle.as_element()
                if scope is not None:
                    break
                await handle.dispose()
                await asyncio.sleep(_CONFIRM_POLL)
            if scope is None:
                return post_action_result(
                    permalink,
                    "reply_editor_unavailable",
                    "The new editor could not be bound to the exact parent, automatic mention, and selected actor.",
                )
            if not confirm_reply:
                return post_action_result(
                    permalink,
                    "confirmation_required",
                    "Set confirm_reply=true to publish. The exact parent and actor were verified; only LinkedIn's automatic mention draft was opened.",
                )
            author = None
            if mention_parent_author:
                author = await self._session.run_on_linkedin(
                    "scope => scope.__linkedinMcpPost.replyAuthor", scope
                )
            elif not await self._session.run_on_linkedin(
                CLEAR_PREPARED_REPLY_JS, scope
            ):
                return post_action_result(
                    permalink,
                    "draft_present",
                    "The automatic parent mention changed; nothing was typed or submitted.",
                )
            result = await self._write_and_submit(
                scope,
                permalink,
                reply,
                success_status="replied",
                unconfirmed_status="reply_unconfirmed",
                noun="reply",
                mention_author=mention_parent_author,
                author_override=author,
                preserve_mention=mention_parent_author,
            )
            return {**result, "parent_comment_reference": reference}
        finally:
            for element in (scope, parent, root):
                if element is not None:
                    try:
                        await element.dispose()
                    except Exception:
                        logger.debug("Could not release reply handle", exc_info=True)

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

    async def _write_and_submit(
        self,
        root: ElementHandle,
        permalink: str,
        text: str,
        *,
        success_status: str,
        unconfirmed_status: str,
        noun: str,
        mention_author: bool = False,
        author_override: dict[str, str] | None = None,
        preserve_mention: bool = False,
    ) -> dict[str, Any]:
        """Write in the pinned post, then preserve ambiguity after dispatch."""
        page = self._session.page
        author = author_override if mention_author else None
        if mention_author and author is None:
            author = await self._session.run_on_linkedin(READ_POST_AUTHOR_JS, root)
        if mention_author and not isinstance(author, dict):
            return post_action_result(
                permalink,
                "mention_unavailable",
                "The post author could not be identified unambiguously. Nothing was submitted.",
            )
        final_text = author["name"] + " " + text if author else text
        counted = await self._session.run_on_linkedin(
            COUNT_TEXT_UNITS_JS,
            {"root": root, "text": final_text.strip(), "captureBaseline": True},
        )
        if counted == -1:
            return post_action_result(
                permalink,
                "write_failed",
                "The receipt scope changed; nothing was typed.",
            )
        baseline = int(counted) if isinstance(counted, int) else 0
        editor_arguments: dict[str, Any] = {"scope": root}
        if preserve_mention:
            editor_arguments["allowPreparedMention"] = True
        editor = None
        submission_attempted = False
        try:
            pinned = await page.evaluate_handle(PIN_EDITOR_JS, arg=editor_arguments)
            status = str(await (await pinned.get_property("status")).json_value())
            editor = (await pinned.get_property("editor")).as_element()
            await pinned.dispose()
            if status != "pinned" or editor is None:
                return post_action_result(
                    permalink,
                    "draft_present" if status == "draft_present" else "write_failed",
                    "The editor contains a draft or cannot be identified; nothing was typed.",
                )
            typed = await self._type_text(
                editor, text, author=author, preserve_mention=preserve_mention
            )
            if typed != "typed":
                return post_action_result(
                    permalink,
                    "mention_unavailable"
                    if typed == "mention_unavailable"
                    else "write_failed",
                    "The editor could not hold the exact text; nothing was submitted. "
                    "Any changed draft was preserved for inspection.",
                )
            text = final_text
            # An evaluate can dispatch its click before its response is lost.
            # From this point exceptions cannot safely be retried as failed writes.
            submission_attempted = True
            try:
                submitted = await self._submit_editor(root, text)
                if submitted != "submitted":
                    if submitted in ("no_submit_control", "ambiguous_submit"):
                        await self._session.run_on_linkedin(
                            CLEAR_EDITOR_JS, {"editor": editor}
                        )
                    return post_action_result(
                        permalink,
                        "submit_unavailable",
                        "The owned editor had no unambiguous submit control; nothing was submitted.",
                    )
                return await self._confirm_text(
                    root,
                    permalink,
                    text,
                    baseline=baseline,
                    success_status=success_status,
                    unconfirmed_status=unconfirmed_status,
                    noun=noun,
                )
            except Exception:
                logger.warning(POST_ACTION_INTERRUPTED_WARNING, exc_info=True)
                return post_action_result(
                    permalink,
                    unconfirmed_status,
                    f"The {noun} may have been published. Check the thread before retrying.",
                    retry_safe=False,
                )
            except BaseException:
                logger.warning(POST_ACTION_INTERRUPTED_WARNING)
                raise
        finally:
            if editor is not None:
                try:
                    with before_the_reply_deadline(
                        _EDITOR_CLEANUP_TIMEOUT_SECONDS, shield=True
                    ) as cleanup:
                        try:
                            await editor.dispose()
                        except Exception:
                            logger.debug(
                                "Could not release post editor handle", exc_info=True
                            )
                    if cleanup.cancel_called:
                        logger.warning("Timed out releasing post editor handle")
                    await anyio.lowlevel.checkpoint()
                except BaseException:
                    if submission_attempted:
                        logger.warning(POST_ACTION_INTERRUPTED_WARNING)
                    raise

    async def _type_text(
        self,
        editor: ElementHandle,
        text: str,
        *,
        author: dict[str, str] | None = None,
        preserve_mention: bool = False,
    ) -> str:
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
        if not await self._session.run_on_linkedin(CAN_TYPE_EDITOR_JS, editor):
            return "not_owned"
        await editor.click()
        if not await editor.evaluate("element => element === document.activeElement"):
            return "not_focusable"
        if not await self._session.run_on_linkedin(CAN_TYPE_EDITOR_JS, editor):
            return "not_owned"

        expected = text
        if author is not None:
            if preserve_mention:
                await editor.press("ControlOrMeta+End")
            query = "@" + author["name"]
            selected = "selected" if preserve_mention else "pending"
            if not preserve_mention:
                await editor.type(query, delay=_TYPE_DELAY)
                for _ in range(20):
                    selected = await self._session.run_on_linkedin(
                        GUARDED_SELECT_AUTHOR_MENTION_JS,
                        {"editor": editor, "author": author},
                    )
                    if selected != "pending":
                        break
                    await asyncio.sleep(_CONFIRM_POLL)
            pinned = False
            if selected == "selected":
                for _ in range(20):
                    pinned = await self._session.run_on_linkedin(
                        GUARDED_PIN_AUTHOR_MENTION_JS,
                        {"editor": editor, "author": author},
                    )
                    if pinned:
                        break
                    await asyncio.sleep(_CONFIRM_POLL)
            if not pinned:
                return "mention_unavailable"
            expected = author["name"] + " " + text
            current = str(await editor.evaluate("element => element.innerText || ''"))
            trailing = current[len(author["name"]) :]
            if trailing == "\n":
                unchanged = await self._session.run_on_linkedin(
                    CHECK_MENTION_PREFIX_JS, {"editor": editor, "expected": current}
                )
                if not unchanged:
                    return "mention_unavailable"
                trailing = ""
            if trailing not in ("", " ", "\u00a0"):
                return "mention_unavailable"
            text = (" " if not trailing else "") + text

        for index, line in enumerate(text.split("\n")):
            if not await self._session.run_on_linkedin(CAN_TYPE_EDITOR_JS, editor):
                return "not_owned"
            if index:
                await editor.press("Shift+Enter")
            if line:
                await editor.type(line, delay=_TYPE_DELAY)

        actual = str(await editor.evaluate("element => element.innerText || ''"))
        if (
            actual.replace("\r\n", "\n").replace("\u00a0", " ").strip()
            != expected.strip()
        ):
            # Unexpected content can include a concurrent edit. Preserve it;
            # only exact owned text may be cleared after a refused submit.
            return "text_mismatch"

        await self._session.run_on_linkedin(
            OWN_EDITOR_JS, {"editor": editor, "text": expected}
        )
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
                await self._session.run_on_linkedin(
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
            count = await self._session.run_on_linkedin(
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
