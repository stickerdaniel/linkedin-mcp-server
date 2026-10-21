"""A bounded rendered reply segment, independent of its editor's lifetime."""

REPLY_RANGE_JS = r"""
function replyContextComments(nodes) {
  return nodes.flatMap(node => [node, ...node.querySelectorAll('[id^="replaceableComment_"]')])
    .filter(node => node.matches('[id^="replaceableComment_"]'));
}
function replyContextSnapshot(node) {
  const id = node.id;
  const urn = id.slice('replaceableComment_'.length);
  const key = node.getAttribute('componentkey');
  if (!/^urn:li:comment:\((?:activity|ugcPost|share):[0-9]+,[0-9]+\)$/.test(urn) ||
      (key && key !== id)) return null;
  const bodies = Array.from(node.querySelectorAll('[componentkey]')).filter(child =>
    child.getAttribute('componentkey') === 'CommentComponentReference_' + urn);
  if (bodies.length !== 1) return null;
  const body = bodies[0];
  const regions = body.querySelectorAll('[data-testid="expandable-text-box"]');
  if (regions.length !== 1) return null;
  const authors = Array.from(body.querySelectorAll('a[href]')).filter(anchor =>
    !anchor.closest('[data-testid="expandable-text-box"]')).map(anchor =>
      [anchor.href, anchor.innerText, Array.from(anchor.querySelectorAll('img')).map(image => [image.src, image.alt])]);
  return {node, id, key, body, text: regions[0].innerText, authors: JSON.stringify(authors)};
}
function replyContextUnchanged(nodes, range) {
  const children = nodes.filter(node => range.beforeChildren.has(node));
  if (children.length !== range.contextChildren.length ||
      children.some((node, index) => node !== range.contextChildren[index])) return false;
  const comments = replyContextComments(nodes);
  return comments.length === range.contextComments.length && comments.every((node, index) => {
    const old = range.contextComments[index];
    const now = replyContextSnapshot(node);
    return old && now && old.node === node && old.id === now.id && old.key === now.key &&
      old.body === now.body && old.text === now.text && old.authors === now.authors;
  });
}
function replyRangeNodes(pin, beforeDispatch) {
  const range = pin?.replyRange;
  if (!range || pin.route !== location.href || !range.column.isConnected ||
      range.start.parentElement !== range.column || range.end.parentElement !== range.column ||
      !range.parent.isConnected || !range.start.contains(range.parent) ||
      range.parent.id !== range.parentId ||
      (range.parent.getAttribute('componentkey') && range.parent.getAttribute('componentkey') !== range.parentId) ||
      !range.body.isConnected || !range.parent.contains(range.body)) return null;
  const nodes = [];
  let node = range.start.nextElementSibling;
  while (node && node !== range.end) { nodes.push(node); node = node.nextElementSibling; }
  if (node !== range.end) return null;
  if (beforeDispatch) {
    if (!nodes.includes(range.editorBlock) || !range.editorBlock.isConnected) return null;
    // Existing flat rows are context, not proof of parentage. Only their exact
    // pre-click nodes, order, bodies and authors may survive into dispatch.
    if (!replyContextUnchanged(nodes, range)) return null;
  }
  return nodes;
}
"""
