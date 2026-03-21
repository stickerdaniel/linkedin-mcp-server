"""Stealth/anti-detection utilities for Patchright browser."""

import asyncio
import logging
import random

from patchright.async_api import Page

logger = logging.getLogger(__name__)

_WEBDRIVER_SCRIPT = """\
if (navigator.webdriver !== undefined) {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
}
"""

_PLUGINS_SCRIPT = """\
if (navigator.plugins.length === 0) {
  const pluginData = [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
  ];
  const plugins = pluginData.map(p => {
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperties(plugin, {
      name: { get: () => p.name },
      filename: { get: () => p.filename },
      description: { get: () => p.description },
      length: { get: () => 0 },
    });
    return plugin;
  });
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const list = Object.create(PluginArray.prototype);
      plugins.forEach((p, i) => { list[i] = p; });
      Object.defineProperty(list, 'length', { get: () => plugins.length });
      return list;
    },
  });
}
"""

_WEBGL_SCRIPT = """\
(function() {
  const target = 'Google SwiftShader';
  const getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 0x9245) return 'Google Inc. (Google)';  // UNMASKED_VENDOR_WEBGL
    if (param === 0x9246) return target;                  // UNMASKED_RENDERER_WEBGL
    return getParam.call(this, param);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 0x9245) return 'Google Inc. (Google)';
      if (param === 0x9246) return target;
      return getParam2.call(this, param);
    };
  }
})();
"""

_PERFORMANCE_MEMORY_SCRIPT = """\
if (!performance.memory) {
  Object.defineProperty(performance, 'memory', {
    get: () => ({
      jsHeapSizeLimit: 2172649472,
      totalJSHeapSize: 48234567,
      usedJSHeapSize: 35678901,
    }),
  });
}
"""


def get_stealth_init_scripts() -> list[str]:
    """Return JavaScript init scripts for fingerprint hardening.

    Each script checks the current value before patching so it is a no-op
    when the property is already set correctly.
    """
    return [
        _WEBDRIVER_SCRIPT,
        _PLUGINS_SCRIPT,
        _WEBGL_SCRIPT,
        _PERFORMANCE_MEMORY_SCRIPT,
    ]


async def random_mouse_move(page: Page, count: int = 3) -> None:
    """Move cursor to *count* random viewport positions with small delays."""
    try:
        size = page.viewport_size
        if not size:
            logger.debug("random_mouse_move: no viewport size available, skipping")
            return

        width, height = size["width"], size["height"]
        for _ in range(count):
            x = random.randint(0, width - 1)
            y = random.randint(0, height - 1)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.05, 0.25))
    except Exception:
        logger.debug("random_mouse_move: error during mouse movement", exc_info=True)


async def hover_random_links(page: Page, max_links: int = 2) -> None:
    """Hover over up to *max_links* random visible ``<a>`` elements."""
    try:
        links = await page.query_selector_all("a:visible")
        if not links:
            logger.debug("hover_random_links: no visible links found")
            return

        targets = random.sample(links, min(max_links, len(links)))
        for link in targets:
            try:
                await link.hover(timeout=2000)
                await asyncio.sleep(random.uniform(0.2, 0.8))
            except Exception:
                logger.debug(
                    "hover_random_links: failed to hover a link", exc_info=True
                )
    except Exception:
        logger.debug("hover_random_links: error finding links", exc_info=True)
