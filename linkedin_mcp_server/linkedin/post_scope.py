"""A bounded rendered reply segment, independent of its editor's lifetime."""

REPLY_RANGE_JS = r"""
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
    // Another comment inserted before dispatch invalidates the causal segment.
    if (nodes.some(item => item.matches('[id^="replaceableComment_"]') ||
        item.querySelector('[id^="replaceableComment_"]'))) return null;
  }
  return nodes;
}
"""
