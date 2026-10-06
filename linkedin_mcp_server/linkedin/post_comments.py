"""Exact rendered comment identities and reply-composer ownership.

The observed SDUI comments expose component URNs, not comment permalinks.
A reply editor can be a sibling of its parent component. Ownership therefore
requires the one new editor created by that parent's Reply control, inside its
bounded following sibling segment, plus the parent's automatic mention and actor
avatar. No index or text-substring lookup selects a parent.
"""

import json

from linkedin_mcp_server.core.destination import (
    LINKEDIN_HOST_PATTERN,
    LINKEDIN_LANDING_JS,
)
from linkedin_mcp_server.linkedin.post_actors import ACTOR_HELPERS_JS

_ORIGIN_GUARD_JS = (
    f"const onLinkedIn = {LINKEDIN_LANDING_JS};"
    f"if (!onLinkedIn(location.href, {json.dumps(LINKEDIN_HOST_PATTERN)})) return null;"
)

COMMENT_HELPERS_JS = (
    ACTOR_HELPERS_JS
    + r"""
const COMMENT_PATTERN = /^urn:li:comment:\((?:activity|ugcPost|share):[0-9]+,[0-9]+\)$/;
function commentIdentity(node) {
  const id = node.getAttribute('id') || '';
  if (!id.startsWith('replaceableComment_')) return null;
  const urn = id.slice('replaceableComment_'.length);
  const key = node.getAttribute('componentkey');
  return COMMENT_PATTERN.test(urn) && (!key || key === id) ? urn : null;
}
function commentComponents(column) {
  return Array.from(column.querySelectorAll('[id^="replaceableComment_"]')).filter(node =>
    actorVisible(node) && commentIdentity(node)
  );
}
function commentBody(node, urn) {
  const matches = Array.from(node.querySelectorAll('[componentkey]')).filter(child =>
    actorVisible(child) && child.getAttribute('componentkey') === 'CommentComponentReference_' + urn
  );
  return matches.length === 1 ? matches[0] : null;
}
function commentAuthor(body) {
  const anchors = Array.from(body.querySelectorAll('a[href]')).filter(node => actorVisible(node) &&
    !node.closest('[data-testid="expandable-text-box"]'));
  const paths = [...new Set(anchors.map(entityPath).filter(Boolean))];
  const identities = paths.map(path => {
    const linked = linkedIdentity(anchors, path);
    const same = anchors.filter(node => entityPath(node) === path);
    // English avatar labels supply the undecorated name. Require that exact
    // name as a separate rendered name text node as well; never strip a suffix
    // from an arbitrary member name (which can itself contain "You").
    const patterns = {en: [/^View (.+)[’']s profile$/, /^(.+), graphic\.$/, /^View company: (.+)$/]};
    const locale = patterns[document.documentElement.lang.toLowerCase().split('-')[0]];
    const names = locale ? [...new Set(same.flatMap(anchor => Array.from(anchor.querySelectorAll('img[alt]'))
      .flatMap(image => locale.map(pattern => image.alt.match(pattern)?.[1]?.trim()).filter(Boolean))))] : [];
    const keys = [...new Set(same.flatMap(avatarKeys))];
    if (names.length === 1 && keys.length === 1 && same.some(anchor => {
      const walker = document.createTreeWalker(anchor, NodeFilter.SHOW_TEXT);
      let node; while ((node = walker.nextNode())) if (node.textContent.trim() === names[0]) return true;
      return false;
    })) return {path, name: names[0], avatar: keys[0]};
    return linked;
  }).filter(Boolean);
  return identities.length === 1 ? identities[0] : null;
}
function commentText(body) {
  // The observed SDUI body has a dedicated expandable text region. Whole-row
  // innerText includes the author biography and actions and is not a comment.
  const regions = Array.from(body.querySelectorAll('[data-testid="expandable-text-box"]')).filter(actorVisible);
  return regions.length === 1 ? regions[0].innerText.trim() : null;
}
function commentColumn(post) {
  const pin = post?.__linkedinMcpPost;
  const column = pin?.confirmationScope;
  return post?.isConnected && pin.route === location.href && column?.isConnected && column.contains(post) ? column : null;
}
function commentThread(component, column) {
  let child = component;
  while (child.parentElement && child.parentElement !== column) child = child.parentElement;
  return child !== column && child.parentElement === column ? child : null;
}
function parentStillMatches(parent) {
  const pin = parent?.__linkedinMcpReply;
  return Boolean(pin && parent.isConnected && commentIdentity(parent) === pin.urn &&
    pin.body.isConnected && parent.contains(pin.body) && commentBody(parent, pin.urn) === pin.body &&
    commentColumn(pin.post) === pin.column && pin.post.__linkedinMcpPost === pin.postPin &&
    pin.thread.isConnected && pin.thread.contains(parent));
}
"""
)

READ_COMMENTS_JS = (
    "({post, limit}) => {"
    + COMMENT_HELPERS_JS
    + r"""
  const column = commentColumn(post);
  if (!column) return null;
  const nodes = commentComponents(column);
  const rows = [];
  for (const node of nodes) {
    const urn = commentIdentity(node);
    if (nodes.filter(other => commentIdentity(other) === urn).length !== 1) continue;
    const body = commentBody(node, urn);
    if (!body) continue;
    const parent = nodes.find(other => other !== node && other.contains(node) &&
      !nodes.some(inner => inner !== node && inner !== other && other.contains(inner) && inner.contains(node)));
    const author = commentAuthor(body);
    const text = commentText(body);
    if (text === null) continue;
    rows.push({reference: urn, text,
      author: author ? {url: author.path, name: author.name} : null,
      parent_reference: parent ? commentIdentity(parent) : null,
      parent_relationship: parent ? 'rendered_ancestor' : 'not_exposed'});
    if (rows.length >= limit) break;
  }
  return rows;
}
"""
)

PIN_PARENT_COMMENT_JS = (
    "({post, reference}) => {"
    + _ORIGIN_GUARD_JS
    + COMMENT_HELPERS_JS
    + r"""
  const column = commentColumn(post);
  if (!column || !COMMENT_PATTERN.test(reference) || !actorStillMatches(post)) return null;
  const matches = commentComponents(column).filter(node => commentIdentity(node) === reference);
  if (matches.length !== 1) return null;
  const parent = matches[0];
  const body = commentBody(parent, reference);
  const thread = commentThread(parent, column);
  const author = body && commentAuthor(body);
  if (!body || !thread || !author || thread === post || thread.contains(post)) return null;
  parent.__linkedinMcpReply = {urn: reference, post, postPin: post.__linkedinMcpPost,
    column, thread, body, author, beforeEditors: null};
  return parent;
}
"""
)

OPEN_REPLY_EDITOR_JS = (
    "(parent) => {"
    + COMMENT_HELPERS_JS
    + r"""
  if (!parentStillMatches(parent) || !actorStillMatches(parent.__linkedinMcpReply.post)) return 'changed';
  const pin = parent.__linkedinMcpReply;
  const selector = '[role="textbox"][contenteditable="true"]';
  if (Array.from(pin.thread.querySelectorAll(selector)).some(actorVisible)) return 'draft_present';
  const labels = {en: 'Reply'};
  const label = labels[document.documentElement.lang.toLowerCase().split('-')[0]];
  if (!label) return 'unavailable';
  const buttons = Array.from(pin.body.querySelectorAll('button')).filter(button =>
    actorVisible(button) && button.getAttribute('aria-label') === label && !button.disabled &&
    button.getAttribute('aria-disabled') !== 'true');
  if (buttons.length !== 1) return 'unavailable';
  pin.beforeEditors = new Set(pin.column.querySelectorAll(selector));
  pin.beforeChildren = new Set(pin.column.children);
  buttons[0].click();
  return 'opened';
}
"""
)

PIN_REPLY_CONTEXT_JS = (
    "(parent) => {"
    + _ORIGIN_GUARD_JS
    + COMMENT_HELPERS_JS
    + r"""
  if (!parentStillMatches(parent)) return null;
  const pin = parent.__linkedinMcpReply;
  if (!pin.beforeEditors || !actorStillMatches(pin.post)) return null;
  const editors = Array.from(pin.column.querySelectorAll('[role="textbox"][contenteditable="true"]'))
    .filter(editor => actorVisible(editor) && !pin.beforeEditors.has(editor));
  if (editors.length !== 1) return null;
  const editor = editors[0];
  const editorBlock = commentThread(editor, pin.column);
  if (!editorBlock || editorBlock === pin.thread) return null;
  let cursor = pin.thread.nextElementSibling;
  while (cursor && cursor !== editorBlock) {
    if (commentComponents(cursor).length || commentIdentity(cursor)) return null;
    cursor = cursor.nextElementSibling;
  }
  if (cursor !== editorBlock) return null;
  let end = editorBlock.nextElementSibling;
  while (end && !pin.beforeChildren.has(end)) end = end.nextElementSibling;
  if (!end) return null;
  const tokens = editor.querySelectorAll('[data-type="mention"][contenteditable="false"]');
  if (tokens.length !== 1 || tokens[0].innerText.trim() !== pin.author.name ||
      editor.innerText.trim() !== pin.author.name) return null;
  let container = editor.parentElement;
  let avatar = null;
  while (container && editorBlock.contains(container)) {
    const images = Array.from(container.querySelectorAll('img[src]')).filter(image => actorVisible(image) && avatarKey(image.src));
    if (images.length) {
      if (images.length !== 1 || avatarKey(images[0].src) !== pin.postPin.actor.avatar) return null;
      avatar = images[0];
      break;
    }
    container = container.parentElement;
  }
  if (!avatar) return null;
  editorBlock.__linkedinMcpPost = {postId: pin.postPin.postId, route: pin.postPin.route,
    actor: pin.postPin.actor, actorRoot: pin.post, actorPin: pin.postPin,
    replyRange: {column: pin.column, start: pin.thread, end, parent, parentId: parent.id, body: pin.body, editorBlock},
    replyAvatar: avatar, confirmationScope: editorBlock, replyEditor: editor, replyAuthor: pin.author};
  editor.__linkedinMcpScope = editorBlock;
  editor.__linkedinMcpPreparedMention = {token: tokens[0], name: pin.author.name};
  return actorStillMatches(editorBlock) ? editorBlock : null;
}
"""
)

CLEAR_PREPARED_REPLY_JS = (
    "(scope) => {"
    + ACTOR_HELPERS_JS
    + r"""
  const editor = scope?.__linkedinMcpPost?.replyEditor;
  const prepared = editor?.__linkedinMcpPreparedMention;
  if (!actorStillMatches(scope) || !scope.contains(editor) || !prepared?.token.isConnected ||
      !editor.contains(prepared.token) || prepared.token.getAttribute('data-type') !== 'mention' ||
      prepared.token.getAttribute('contenteditable') !== 'false' ||
      prepared.token.innerText.trim() !== prepared.name || editor.innerText.trim() !== prepared.name) return false;
  editor.focus();
  const range = document.createRange(); range.selectNodeContents(editor);
  const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
  document.execCommand('delete', false, null);
  return editor.innerText.trim() === '';
}
"""
)

PARENT_READINESS_JS = (
    "({post, reference}) => {"
    + COMMENT_HELPERS_JS
    + r"""
  const column = commentColumn(post);
  if (!column || !actorStillMatches(post)) return 'changed';
  const matches = commentComponents(column).filter(node => commentIdentity(node) === reference);
  return matches.length === 0 ? 'missing' : matches.length === 1 ? 'present' : 'ambiguous';
}
"""
)
