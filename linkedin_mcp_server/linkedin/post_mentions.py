"""Bind a rendered post author to an actual rich-text mention suggestion."""

from linkedin_mcp_server.linkedin.post_actors import ACTOR_HELPERS_JS

READ_POST_AUTHOR_JS = (
    "(root) => {"
    + ACTOR_HELPERS_JS
    + r"""
  const bar = root?.__linkedinMcpPost?.bar;
  if (!bar || !root.contains(bar)) return null;
  const anchors = Array.from(root.querySelectorAll('a[href]')).filter(node =>
    actorVisible(node) && (node.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING)
  );
  const authors = [];
  for (const anchor of anchors) {
    const path = entityPath(anchor);
    const keys = avatarKeys(anchor);
    if (!path || keys.length !== 1) continue;
    const author = linkedIdentity(anchors, path);
    if (author) authors.push(author);
  }
  const unique = [...new Map(authors.map(author => [JSON.stringify(author), author])).values()];
  return unique.length === 1 ? unique[0] : null;
}
"""
)

SELECT_AUTHOR_MENTION_JS = (
    "({editor, author}) => {"
    + ACTOR_HELPERS_JS
    + r"""
  if (!editor?.isConnected || document.activeElement !== editor) return 'unavailable';
  const labels = {en: 'Mention suggestions'};
  const label = labels[document.documentElement.lang.toLowerCase().split('-')[0]];
  if (!label) return 'unavailable';
  const lists = Array.from(document.querySelectorAll('[role="listbox"]')).filter(node =>
    actorVisible(node) && node.getAttribute('aria-label') === label
  );
  if (lists.length === 0) return 'pending';
  if (lists.length !== 1) return 'unavailable';
  const options = Array.from(lists[0].querySelectorAll('[role="option"]')).filter(node => {
    const keys = avatarKeys(node);
    return actorVisible(node) && node.querySelector('p')?.innerText.trim() === author.name &&
      keys.length === 1 && keys[0] === author.avatar;
  });
  if (options.length !== 1) return 'unavailable';
  const controls = Array.from(options[0].querySelectorAll('[role="button"]')).filter(actorVisible);
  if (controls.length !== 1) return 'unavailable';
  controls[0].click();
  return 'selected';
}
"""
)

PIN_AUTHOR_MENTION_JS = r"""({editor, author}) => {
  if (!editor?.isConnected) return false;
  const tokens = Array.from(editor.querySelectorAll('[data-type="mention"][contenteditable="false"]'));
  if (tokens.length !== 1 || tokens[0].innerText.trim() !== author.name) return false;
  if (editor.innerText.trim() !== author.name) return false;
  editor.__linkedinMcpMention = {token: tokens[0], name: author.name};
  editor.focus();
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
} """

MENTION_STILL_MATCHES_JS = r"""
function mentionStillMatches(editor) {
  const pin = editor.__linkedinMcpMention;
  if (!pin) return true;
  return pin.token.isConnected && editor.contains(pin.token) &&
    pin.token.getAttribute('data-type') === 'mention' &&
    pin.token.getAttribute('contenteditable') === 'false' &&
    pin.token.innerText.trim() === pin.name &&
    editor.querySelectorAll('[data-type="mention"]').length === 1;
}
"""
