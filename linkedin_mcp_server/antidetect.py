"""Anti-detection: timing, fingerprints, stealth JS."""

import asyncio
import logging
import random
import time

from patchright.async_api import Page

logger = logging.getLogger(__name__)

# Behavioral modes: (weight, min, max, distribution)
# 70% quick scan, 20% reading, 10% distracted
_MODES = [(0.70, 1.2, 2.5), (0.90, 2.5, 5.0), (1.0, 5.0, 12.0)]

# Time-of-day pace: morning focus, lunch drift, late-night drowse
_PACE = {range(9, 12): 0.85, range(12, 14): 1.1, range(22, 24): 1.3, range(7): 1.3}


def _think_time(short: bool = False) -> float:
    """Sample from a mixture model that mimics human browsing rhythms."""
    roll = random.random()  # noqa: S311
    for threshold, lo, hi in _MODES:
        if roll < threshold:
            base = max(lo, min(hi, random.lognormvariate((lo + hi) / 4, 0.25)))
            break
    else:
        base = 2.0
    if short:
        base = min(base, 6.0) * 0.7
    hour = time.localtime().tm_hour
    pace = next((v for r, v in _PACE.items() if hour in r), 1.0)
    return base * pace


async def human_delay(short: bool = False) -> None:
    """Pause like a human. short=True for between-section navigation."""
    delay = _think_time(short)
    logger.debug("anti-detect: %.2fs", delay)
    await asyncio.sleep(delay)


async def nav_jitter() -> None:
    """Short jitter between section navigations."""
    await human_delay(short=True)


_VIEWPORTS = [
    (1280, 720),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1680, 1050),
    (1920, 1080),
    (2560, 1440),
]


def random_viewport() -> dict[str, int]:
    """Common desktop viewport with slight jitter."""
    w, h = random.choice(_VIEWPORTS)  # noqa: S311
    return {
        "width": w + random.randint(-12, 12),  # noqa: S311
        "height": h + random.randint(-12, 12),  # noqa: S311
    }


# (user_agent, weight) — weighted by real desktop market share
_USER_AGENTS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",  # noqa: E501
        28,
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",  # noqa: E501
        8,
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",  # noqa: E501
        12,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",  # noqa: E501
        25,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",  # noqa: E501
        8,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",  # noqa: E501
        10,
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",  # noqa: E501
        5,
    ),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0", 4),
]
_UA_STRINGS, _UA_WEIGHTS = zip(*_USER_AGENTS, strict=True)


def random_user_agent() -> str:
    """Pick a user-agent weighted by market share."""
    return random.choices(_UA_STRINGS, weights=_UA_WEIGHTS, k=1)[0]  # noqa: S311


async def simulate_human_behavior(page: Page) -> None:
    """Bezier mouse movement + scroll after navigation."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
        sx, sy = random.randint(100, vp["width"] // 2), random.randint(100, vp["height"] // 2)  # noqa: S311
        ex, ey = (
            random.randint(vp["width"] // 3, vp["width"] - 100),
            random.randint(vp["height"] // 4, vp["height"] - 100),
        )

        for i in range(random.randint(5, 12)):  # noqa: S311
            t = (i / 10) ** 2 * (3 - 2 * i / 10)  # ease-in-out
            await page.mouse.move(
                sx + (ex - sx) * t + random.randint(-3, 3),  # noqa: S311
                sy + (ey - sy) * t + random.randint(-3, 3),  # noqa: S311
            )
            await asyncio.sleep(random.uniform(0.01, 0.04))  # noqa: S311

        scroll = random.randint(150, 500)  # noqa: S311
        await page.mouse.wheel(0, scroll)
        await asyncio.sleep(random.uniform(0.2, 0.6))  # noqa: S311
        if random.random() < 0.7:  # noqa: S311
            await page.mouse.wheel(0, -random.randint(50, scroll // 2))  # noqa: S311
    except Exception:
        logger.debug("anti-detect: mouse sim skipped", exc_info=True)


_STEALTH_JS = """\
() => {
    // WebDriver
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
    try { delete navigator.__proto__.webdriver; } catch(e) {}

    // Chrome runtime
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime)
        window.chrome.runtime = {connect(){}, sendMessage(){}, id: undefined};

    // Plugins
    const mkP = (n,f,d) => {
        const p = {name:n, filename:f, description:d, length:1};
        p[0] = {type:'application/pdf', suffixes:'pdf', description:''};
        return p;
    };
    Object.defineProperty(navigator, 'plugins', {get: () => {
        const l = [mkP('Chrome PDF Plugin','internal-pdf-viewer','Portable Document Format'),
                    mkP('Chrome PDF Viewer','mhjfbmdgcfjbbpaeojofohoefgiehjai',''),
                    mkP('Native Client','internal-nacl-plugin','')];
        l.item = i => l[i]||null; l.namedItem = n => l.find(p=>p.name===n)||null;
        l.refresh = ()=>{}; return l;
    }});
    Object.defineProperty(navigator, 'mimeTypes', {get: () => {
        const l = [{type:'application/pdf', suffixes:'pdf', description:'', enabledPlugin:null}];
        l.item = i => l[i]||null; l.namedItem = n => l.find(m=>m.type===n)||null; return l;
    }});

    // Fingerprint consistency (pinned across login and server browsers)
    Object.defineProperty(navigator, 'languages', {get: () => Object.freeze(['en-US','en'])});
    const cores = __CORES__;
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => cores});
    const mem = __MEM__;
    Object.defineProperty(navigator, 'deviceMemory', {get: () => mem});
    const ua = navigator.userAgent;
    const plat = ua.includes('Mac') ? 'MacIntel' : ua.includes('Linux') ? 'Linux x86_64' : 'Win32';
    Object.defineProperty(navigator, 'platform', {get: () => plat});

    // Permissions
    const oQ = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = p => p.name==='notifications'
        ? Promise.resolve({state: Notification.permission}) : oQ(p);

    // Canvas noise
    const oTDU = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {
        const c = this.getContext('2d');
        if (c) { const s=c.fillStyle; c.fillStyle='rgba(0,0,'+~~(Math.random()*2)+',0.01)';
                  c.fillRect(0,0,1,1); c.fillStyle=s; }
        return oTDU.apply(this, arguments);
    };

    // WebGL
    const gP = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p===37445) return 'Google Inc. (Apple)';
        if (p===37446) return 'ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Max, Unspecified Version)';
        return gP.apply(this, arguments);
    };

    // Headless traps
    if (!window.outerWidth) {
        Object.defineProperty(window, 'outerWidth', {get:()=>innerWidth+15});
        Object.defineProperty(window, 'outerHeight', {get:()=>innerHeight+85});
    }
    if (navigator.connection?.rtt===0)
        Object.defineProperty(navigator.connection, 'rtt', {get:()=>50});
}
"""


_DEFAULT_FINGERPRINT: dict[str, int] = {"hardwareConcurrency": 8, "deviceMemory": 8}


def _build_stealth_js(fingerprint: dict[str, int] | None = None) -> str:
    """Compile stealth JS with pinned fingerprint values."""
    fp = fingerprint or _DEFAULT_FINGERPRINT
    return _STEALTH_JS.replace("__CORES__", str(fp["hardwareConcurrency"])).replace(
        "__MEM__", str(fp["deviceMemory"])
    )


async def inject_stealth_page(page: Page, fingerprint: dict[str, int] | None = None) -> None:
    """Inject stealth JS. Safe to call on every navigation."""
    try:
        await page.evaluate(_build_stealth_js(fingerprint))
    except Exception:
        logger.debug("anti-detect: stealth skipped", exc_info=True)


async def apply_antidetect(page: Page, fingerprint: dict[str, int] | None = None) -> None:
    """All anti-detection measures. Call once after browser start."""
    await inject_stealth_page(page, fingerprint)
    await simulate_human_behavior(page)
    logger.info("anti-detect: applied (fingerprint=%s)", fingerprint)
