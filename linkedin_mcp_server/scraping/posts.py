"""
Scraping logic for LinkedIn posts and comments (my feed, post comments, unreplied).

Uses page.evaluate and DOM traversal to extract structured data. Best-effort:
post_id/urn, created_at, comment_permalink may be missing if not present in DOM.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from patchright.async_api import Page

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    humanized_delay,
    rate_limit_state,
    scroll_to_bottom,
    wait_for_cooldown,
)
from linkedin_mcp_server.scraping.cache import scraping_cache
from linkedin_mcp_server.scraping.extractor import (
    LinkedInExtractor,
    _RATE_LIMITED_ERROR,
    _RATE_LIMITED_MSG,
)

logger = logging.getLogger(__name__)
_FEED_URL = "https://www.linkedin.com/feed/"
_MY_POSTS_URL = "https://www.linkedin.com/in/me/detail/recent-activity/shares/"
_NOTIFICATIONS_URL = "https://www.linkedin.com/notifications/"
_ACTIVITY_URN_PATTERN = re.compile(r"urn:li:activity:(\d+)")
_BASE_POST_URL = "https://www.linkedin.com/feed/update/"

# ---------------------------------------------------------------------------
# JS evaluate strings — extracted as constants so integration tests can
# import and run them against HTML fixtures via patchright.
# ---------------------------------------------------------------------------

_JS_EXTRACT_MY_POSTS = """(limit) => {
    const out = [];
    const seen = new Set();
    const root = document.querySelector('main') || document.body;
    if (!root) return { items: out, scrollHeight: document.body.scrollHeight };
    const cards = root.querySelectorAll('article, div[data-urn], div.feed-shared-update-v2');
    for (const card of cards) {
        let urn = '';
        const dataUrn = (card.getAttribute('data-urn') || '').trim();
        const m1 = dataUrn.match(/urn:li:activity:\\d+/);
        if (m1) urn = m1[0];
        if (!urn) {
            const a = card.querySelector('a[href*="/feed/update/"]');
            const href = (a?.getAttribute('href') || '').trim();
            const m2 = href.match(/feed\\/update\\/(urn:li:activity:\\d+)/);
            if (m2) urn = m2[1];
        }
        if (!urn || seen.has(urn)) continue;
        seen.add(urn);
        let text = '';
        const dirEls = card.querySelectorAll('[dir="ltr"]');
        let best = null, bestLen = 0;
        for (const el of dirEls) {
            const t = (el.innerText || '').trim();
            if (t.length > bestLen) { best = el; bestLen = t.length; }
        }
        if (best && bestLen > 30) {
            text = best.innerText.trim();
        } else {
            const any = card.querySelector('span[dir]') || card;
            text = (any?.innerText || '').trim();
        }
        text = (text || '').trim().slice(0, 500);
        let createdAt = null;
        const timeEl = card.querySelector('time');
        if (timeEl) {
            createdAt = (timeEl.getAttribute('datetime') || timeEl.innerText || '').trim() || null;
        }
        const idMatch = urn.match(/urn:li:activity:(\\d+)/);
        const id = idMatch ? idMatch[1] : null;
        const postUrl = id ? ('https://www.linkedin.com/feed/update/' + urn + '/') : null;
        if (postUrl) {
            out.push({ post_url: postUrl, post_id: urn, text_preview: text, created_at: createdAt });
            if (out.length >= limit) break;
        }
    }
    return { items: out, scrollHeight: document.body.scrollHeight };
}"""

_JS_EXTRACT_FEED_POSTS = """(limit) => {
    const out = [];
    const seen = new Set();
    const root = document.querySelector('main') || document.body;
    if (!root) return { items: out, scrollHeight: document.body.scrollHeight };
    const cards = root.querySelectorAll(
        'div.feed-shared-update-v2, div[data-urn*="activity"], article'
    );
    for (const card of cards) {
        let urn = '';
        const dataUrn = (card.getAttribute('data-urn') || '').trim();
        const m1 = dataUrn.match(/urn:li:activity:\\d+/);
        if (m1) urn = m1[0];
        if (!urn) {
            const a = card.querySelector('a[href*="/feed/update/"]');
            const href = (a?.getAttribute('href') || '').trim();
            const m2 = href.match(/feed\\/update\\/(urn:li:activity:\\d+)/);
            if (m2) urn = m2[1];
        }
        if (!urn || seen.has(urn)) continue;
        seen.add(urn);
        let authorName = null;
        let authorUrl = null;
        const authorLink = card.querySelector(
            'a.update-components-actor__meta-link, ' +
            'a[data-tracking-control-name*="actor"], ' +
            'a[href*="/in/"]'
        );
        if (authorLink) {
            authorName = (authorLink.innerText || '').trim().split('\\n')[0].trim();
            const href = (authorLink.getAttribute('href') || '').trim();
            authorUrl = href.startsWith('http') ? href :
                'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
        }
        let text = '';
        const dirEls = card.querySelectorAll('[dir="ltr"]');
        let best = null, bestLen = 0;
        for (const el of dirEls) {
            const t = (el.innerText || '').trim();
            if (t.length > bestLen) { best = el; bestLen = t.length; }
        }
        if (best && bestLen > 30) {
            text = best.innerText.trim();
        } else {
            const any = card.querySelector('span[dir]') || card;
            text = (any?.innerText || '').trim();
        }
        text = (text || '').trim().slice(0, 500);
        let createdAt = null;
        const timeEl = card.querySelector('time');
        if (timeEl) {
            createdAt = (timeEl.getAttribute('datetime') || timeEl.innerText || '').trim() || null;
        }
        const idMatch = urn.match(/urn:li:activity:(\\d+)/);
        const postUrl = idMatch
            ? 'https://www.linkedin.com/feed/update/' + urn + '/'
            : null;
        if (postUrl) {
            out.push({
                post_url: postUrl, post_id: urn, text_preview: text,
                author_name: authorName, author_url: authorUrl, created_at: createdAt
            });
            if (out.length >= limit) break;
        }
    }
    return { items: out, scrollHeight: document.body.scrollHeight };
}"""

_JS_EXTRACT_COMMENTS = """(postAuthorName) => {
    const comments = [];
    const main = document.querySelector('main');
    if (!main) return comments;
    const nameToCheck = (postAuthorName || '').trim().toLowerCase();
    const allCommentEls = main.querySelectorAll('[data-urn*="urn:li:comment"]');
    const topLevel = [];
    for (const el of allCommentEls) {
        const parent = el.parentElement;
        if (parent && parent.closest('[data-urn*="urn:li:comment"]')) continue;
        topLevel.push(el);
    }
    for (const block of topLevel) {
        const commentUrn = (block.getAttribute('data-urn') || '').trim();
        const authorLink = block.querySelector('a[href*="/in/"]');
        if (!authorLink) continue;
        const authorUrl = (authorLink.getAttribute('href') || '').trim();
        if (authorUrl.includes('/in/me') || !authorUrl) continue;
        const authorName = (authorLink.innerText || authorLink.getAttribute('aria-label') || '').trim();
        const textEl = block.querySelector('[dir="ltr"]') || block.querySelector('span');
        const text = (textEl ? textEl.innerText : block.innerText) || '';
        const cleanText = text.replace(authorName, '').trim().slice(0, 2000);
        let permalink = null;
        const permLink = block.querySelector('a[href*="commentUrn"]');
        if (permLink) permalink = (permLink.getAttribute('href') || '').trim();
        const commentId = commentUrn || (permalink ? (permalink.match(/commentUrn=([^&]+)/) || [])[1] : null);
        let hasReplyFromAuthor = false;
        if (nameToCheck) {
            const replyEls = block.querySelectorAll('[data-urn*="urn:li:comment"]');
            for (const r of replyEls) {
                if (r === block) continue;
                const replyLink = r.querySelector('a[href*="/in/"]');
                if (!replyLink) continue;
                const replyName = (replyLink.innerText || '').trim().toLowerCase();
                if (replyName && replyName.indexOf(nameToCheck) !== -1) { hasReplyFromAuthor = true; break; }
            }
        }
        const fullAuthorUrl = authorUrl.startsWith('http') ? authorUrl : 'https://www.linkedin.com' + (authorUrl.startsWith('/') ? authorUrl : '/' + authorUrl);
        comments.push({
            comment_id: commentId, author_name: authorName || null, author_url: fullAuthorUrl,
            text: cleanText, created_at: null, comment_permalink: permalink,
            has_reply_from_author: hasReplyFromAuthor
        });
    }
    if (comments.length === 0) {
        const blocks = main.querySelectorAll('[class*="comment"], [data-id*="comment"]');
        for (const block of blocks) {
            const authorLink = block.querySelector('a[href*="/in/"]');
            if (!authorLink) continue;
            const authorUrl = (authorLink.getAttribute('href') || '').trim();
            if (authorUrl.includes('/in/me') || !authorUrl) continue;
            const authorName = (authorLink.innerText || authorLink.getAttribute('aria-label') || '').trim();
            const textEl = block.querySelector('[dir="ltr"]') || block.querySelector('span');
            const text = (textEl ? textEl.innerText : block.innerText) || '';
            const cleanText = text.replace(authorName, '').trim().slice(0, 2000);
            const permLink = block.querySelector('a[href*="commentUrn"]');
            const commentId = permLink ? ((permLink.getAttribute('href') || '').match(/commentUrn=([^&]+)/) || [])[1] : null;
            let hasReplyFromAuthor = false;
            if (nameToCheck) {
                const replyBlocks = block.querySelectorAll('[class*="reply"], [class*="comment"]');
                for (const r of replyBlocks) {
                    if (r === block) continue;
                    const replyLink = r.querySelector('a[href*="/in/"]');
                    if (replyLink && (replyLink.innerText || '').trim().toLowerCase().indexOf(nameToCheck) !== -1) { hasReplyFromAuthor = true; break; }
                }
            }
            comments.push({
                comment_id: commentId, author_name: authorName || null,
                author_url: authorUrl.startsWith('http') ? authorUrl : 'https://www.linkedin.com' + (authorUrl.startsWith('/') ? authorUrl : '/' + authorUrl),
                text: cleanText, created_at: null,
                comment_permalink: permLink ? (permLink.getAttribute('href') || '').trim() : null,
                has_reply_from_author: hasReplyFromAuthor
            });
        }
    }
    return comments;
}"""

_JS_EXTRACT_NOTIFICATIONS = """(maxItems) => {
    const items = [];
    const main = document.querySelector('main');
    if (!main) return items;
    const typeMap = [
        { type: 'reaction', terms: ['reacted', 'like', 'liked', 'love', 'celebrate', 'support', 'insightful', 'funny', 'curtiu', 'reagiu', 'reação'] },
        { type: 'comment', terms: ['comment', 'commented', 'comentou', 'comentário', 'reply', 'replied', 'respondeu', 'resposta'] },
        { type: 'connection', terms: ['connect', 'connection', 'accepted', 'invitation', 'convite', 'conexão', 'aceito'] },
        { type: 'mention', terms: ['mention', 'mentioned', 'mencionou', 'menção', 'tagged', 'marcou'] },
        { type: 'endorsement', terms: ['endorse', 'endorsed', 'skill', 'competência', 'recomend'] },
        { type: 'job', terms: ['job', 'hiring', 'vaga', 'emprego', 'position', 'career', 'carreira', 'recruiter'] },
        { type: 'post', terms: ['post', 'posted', 'shared', 'publicou', 'compartilhou', 'article', 'artigo'] },
        { type: 'birthday', terms: ['birthday', 'aniversário', 'born'] },
        { type: 'work_anniversary', terms: ['anniversary', 'work anniversary', 'aniversário de trabalho'] },
        { type: 'view', terms: ['view', 'viewed', 'appeared', 'visualiz', 'perfil'] },
    ];
    function detectType(text) {
        const lower = text.toLowerCase();
        for (const entry of typeMap) {
            if (entry.terms.some(t => lower.includes(t))) return entry.type;
        }
        return 'other';
    }
    let cards = main.querySelectorAll(
        'div.nt-card, div[data-urn*="notification"], article'
    );
    if (cards.length === 0) {
        cards = main.querySelectorAll('section li');
    }
    if (cards.length === 0) {
        const anchors = main.querySelectorAll(
            'a[href*="/feed/update/"], a[href*="/notifications/"], a[href*="/jobs/view/"], a[href*="/in/"]'
        );
        const cardSet = new Set();
        for (const a of anchors) {
            let container = a.parentElement;
            while (container && container !== main && container.innerText.trim().length < 20) {
                container = container.parentElement;
            }
            if (container && container !== main) cardSet.add(container);
        }
        cards = Array.from(cardSet);
    }
    const seen = new Set();
    for (const card of cards) {
        let text = (card.innerText || '').trim();
        text = text.replace(/^Status is \\w+\\n?/i, '').trim();
        if (!text || text.length < 10) continue;
        let link = null;
        const a = card.querySelector('a[href*="/feed/"], a[href*="/in/"], a[href*="/jobs/"], a[href*="/notifications/"]');
        if (a) {
            const href = (a.getAttribute('href') || '').trim();
            link = href.startsWith('http') ? href : 'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
        }
        const dedup = link || text.slice(0, 120);
        if (seen.has(dedup)) continue;
        seen.add(dedup);
        let createdAt = null;
        const timeEl = card.querySelector('time');
        if (timeEl) {
            createdAt = (timeEl.getAttribute('datetime') || timeEl.innerText || '').trim() || null;
        }
        if (!createdAt) {
            const relMatch = text.match(/\\b(\\d+[smhdw]|\\d+ (?:second|minute|hour|day|week|month|year|segundo|minuto|hora|dia|semana|mês|ano)s? ago)\\b/i);
            if (relMatch) createdAt = relMatch[0];
        }
        const type = detectType(text);
        const snippet = text.slice(0, 300);
        items.push({ text: snippet, link: link, type: type, created_at: createdAt });
        if (items.length >= maxItems) break;
    }
    return items;
}"""


def _normalize_post_url(post_url_or_id: str) -> str:
    """Return canonical post URL from post_url or post_id."""
    s = post_url_or_id.strip()
    if s.startswith("http"):
        return s.rstrip("/") + "/"
    # Allow raw numeric id or urn
    match = _ACTIVITY_URN_PATTERN.search(s)
    if match:
        return f"{_BASE_POST_URL}urn:li:activity:{match.group(1)}/"
    if s.isdigit():
        return f"{_BASE_POST_URL}urn:li:activity:{s}/"
    return f"{_BASE_POST_URL}{s}/" if not s.endswith("/") else f"{_BASE_POST_URL}{s}"


async def _extract_engagement_metrics(page: Page) -> dict[str, Any]:
    """Extract engagement metrics (reactions, comments, reposts) from a post page.

    Best-effort extraction from the social counts bar visible on post pages.
    Returns dict with reactions, comments_count, reposts_count (all int or None).
    """
    try:
        raw = await page.evaluate(
            """() => {
            const metrics = { reactions: null, comments_count: null, reposts_count: null };
            const main = document.querySelector('main') || document.body;
            if (!main) return metrics;

            // Helper: parse "1,234" or "1.234" or "1K" or "1.2K" or "1,2K" to int
            function parseCount(s) {
                if (!s) return null;
                s = s.trim();
                // Normalize comma to dot for K/M decimal (e.g. "1,2K" → "1.2K")
                const norm = s.replace(/,/g, '.');
                const kMatch = norm.match(/([\\d.]+)\\s*[kK]/);
                if (kMatch) return Math.round(parseFloat(kMatch[1]) * 1000);
                const mMatch = norm.match(/([\\d.]+)\\s*[mM]/);
                if (mMatch) return Math.round(parseFloat(mMatch[1]) * 1000000);
                // Plain integer: strip all separators
                const num = parseInt(s.replace(/[.,]/g, '').replace(/\\D/g, ''), 10);
                return isNaN(num) ? null : num;
            }

            // Reactions count: usually in a button/span with "reactions" or "reações"
            const reactionsEl = main.querySelector(
                'button[aria-label*="reaction"], button[aria-label*="reação"], ' +
                'span[aria-label*="reaction"], span[aria-label*="reação"], ' +
                'button.social-details-social-counts__reactions-count, ' +
                'span.social-details-social-counts__reactions-count'
            );
            if (reactionsEl) {
                const label = reactionsEl.getAttribute('aria-label') || reactionsEl.innerText || '';
                metrics.reactions = parseCount(label);
            }

            // Comments count
            const commentsEl = main.querySelector(
                'button[aria-label*="comment"], button[aria-label*="comentário"], ' +
                'span[aria-label*="comment"], span[aria-label*="comentário"]'
            );
            if (commentsEl) {
                const label = commentsEl.getAttribute('aria-label') || commentsEl.innerText || '';
                metrics.comments_count = parseCount(label);
            }

            // Reposts count
            const repostsEl = main.querySelector(
                'button[aria-label*="repost"], button[aria-label*="republicaç"], ' +
                'span[aria-label*="repost"], span[aria-label*="republicaç"]'
            );
            if (repostsEl) {
                const label = repostsEl.getAttribute('aria-label') || repostsEl.innerText || '';
                metrics.reposts_count = parseCount(label);
            }

            // Fallback: scan all social counts text
            if (metrics.reactions === null && metrics.comments_count === null) {
                const countsBar = main.querySelector(
                    '.social-details-social-counts, ' +
                    '[class*="social-counts"], ' +
                    '[class*="social-details"]'
                );
                if (countsBar) {
                    const text = countsBar.innerText || '';
                    // Pattern: "42 reactions · 12 comments · 3 reposts"
                    const rxn = text.match(/(\\d[\\d,.kKmM]*)\\s*(?:reaction|reação|like|curtida)/i);
                    if (rxn) metrics.reactions = parseCount(rxn[1]);
                    const cmt = text.match(/(\\d[\\d,.kKmM]*)\\s*(?:comment|comentário)/i);
                    if (cmt) metrics.comments_count = parseCount(cmt[1]);
                    const rp = text.match(/(\\d[\\d,.kKmM]*)\\s*(?:repost|republicaç|compartilh)/i);
                    if (rp) metrics.reposts_count = parseCount(rp[1]);
                }
            }

            return metrics;
        }"""
        )
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.debug("Could not extract engagement metrics: %s", e)
        return {}


async def _detect_post_type(page: Page) -> str:
    """Detect the post format type from the post page (best-effort).

    Returns one of: text, carousel, video, image, poll, newsletter, article, unknown.
    """
    try:
        result = await page.evaluate(
            """() => {
            const main = document.querySelector('main') || document.body;
            if (!main) return 'unknown';

            // Video
            if (main.querySelector('video, [class*="video-player"], [data-urn*="video"]'))
                return 'video';

            // Carousel / Document
            if (main.querySelector(
                '[class*="document"], [class*="carousel"], ' +
                '[aria-label*="carousel"], [aria-label*="document"], ' +
                '[aria-label*="carrossel"], [aria-label*="documento"]'
            )) return 'carousel';

            // Poll
            if (main.querySelector('[class*="poll"], [data-urn*="poll"]'))
                return 'poll';

            // Newsletter / Article
            if (main.querySelector('[class*="newsletter"], [data-urn*="newsletter"]'))
                return 'newsletter';
            if (main.querySelector('[class*="article"], a[href*="/pulse/"]'))
                return 'article';

            // Image
            if (main.querySelector(
                'img[class*="feed-shared-image"], ' +
                'div[class*="feed-shared-image"], ' +
                'img[class*="update-components-image"]'
            )) return 'image';

            return 'text';
        }"""
        )
        return result if isinstance(result, str) else "unknown"
    except Exception as e:
        logger.debug("Could not detect post type: %s", e)
        return "unknown"


async def _extract_author_info(page: Page) -> dict[str, Any]:
    """Extract author name, headline, and profile URL from a post page (best-effort)."""
    try:
        raw = await page.evaluate(
            """() => {
            const main = document.querySelector('main') || document.body;
            if (!main) return { name: null, headline: null, profile_url: null };

            const authorLink = main.querySelector(
                'a.update-components-actor__meta-link, ' +
                'a[data-tracking-control-name*="actor"], ' +
                'a.app-aware-link[href*="/in/"]'
            );
            let name = null, profileUrl = null, headline = null;
            if (authorLink) {
                name = (authorLink.innerText || '').trim().split('\\n')[0].trim();
                const href = (authorLink.getAttribute('href') || '').trim();
                profileUrl = href.startsWith('http') ? href :
                    'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
            }

            // Headline is usually a sibling/child near the author name
            const headlineEl = main.querySelector(
                'span.update-components-actor__description, ' +
                'span.feed-shared-actor__description, ' +
                '[class*="actor__sub-description"], ' +
                '[class*="actor__description"]'
            );
            if (headlineEl) {
                headline = (headlineEl.innerText || '').trim().split('\\n')[0].trim();
            }

            return { name: name, headline: headline, profile_url: profileUrl };
        }"""
        )
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.debug("Could not extract author info: %s", e)
        return {}


async def get_post_content(
    page: Page,
    post_url_or_id: str,
) -> dict[str, Any]:
    """
    Get the text content, engagement metrics, and metadata of a LinkedIn post.

    Navigates to the post URL, scrolls to load content, and extracts the
    innerText using the standard LinkedInExtractor pipeline (handles caching,
    rate limits, noise stripping, and retries). Also extracts engagement
    metrics (reactions, comments, reposts), post type, and author info.

    Args:
        page: Patchright page instance.
        post_url_or_id: Post URL, URN (urn:li:activity:123), or numeric ID.

    Returns:
        Dict with url, sections: {"post_content": text}, pages_visited,
        sections_requested, engagement, post_type, author.
    """
    url = _normalize_post_url(post_url_or_id)
    extractor = LinkedInExtractor(page)
    extracted = await extractor.extract_page(url, section_name="post_content")

    sections: dict[str, str] = {}
    references: dict[str, list] = {}
    section_errors: dict[str, dict[str, Any]] = {}
    if extracted.text and extracted.text != _RATE_LIMITED_MSG:
        sections["post_content"] = extracted.text
        if extracted.references:
            references["post_content"] = extracted.references
    elif extracted.text == _RATE_LIMITED_MSG:
        section_errors["post_content"] = _RATE_LIMITED_ERROR
    elif extracted.error:
        section_errors["post_content"] = extracted.error

    # Extract engagement metrics, post type, and author from the loaded page
    engagement = await _extract_engagement_metrics(page)
    post_type = await _detect_post_type(page)
    author = await _extract_author_info(page)

    result: dict[str, Any] = {
        "url": url,
        "sections": sections,
        "pages_visited": [url],
        "sections_requested": ["post_content"],
        "engagement": engagement,
        "post_type": post_type,
        "author": author,
    }
    if references:
        result["references"] = references
    if section_errors:
        result["section_errors"] = section_errors
    return result


async def _get_current_user_name(page: Page) -> str | None:
    """Try to get current user display name from nav (for reply detection).

    Tries in order: avatar img alt, Me/Eu menu aria-label, nav profile link.
    """
    try:
        name = await page.evaluate(
            """() => {
            const trim = (s) => (s || '').trim();
            // 1) Avatar in nav often has alt="Member Name"
            const navImg = document.querySelector('nav img[alt]');
            if (navImg) {
                const alt = trim(navImg.getAttribute('alt'));
                if (alt && alt.length > 1 && !/^(photo|foto|image|imagem|profile|perfil)$/i.test(alt)) return alt;
            }
            // 2) Me / Eu menu button or link
            const meSelectors = [
                'button[aria-label*="Me"]', 'button[aria-label*="Eu"]',
                'a[aria-label*="Me "]', 'a[aria-label*="Eu "]',
                '[aria-label*="Profile"]', '[aria-label*="Perfil"]'
            ];
            for (const sel of meSelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const label = trim(el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent);
                    if (label && label.length > 1) return label;
                }
            }
            // 3) Fallback: first nav link to /in/ that is not /in/me
            const a = document.querySelector('nav a[href*="/in/"]:not([href*="/in/me"])');
            if (a) return trim(a.getAttribute('aria-label') || a.textContent) || null;
            return null;
        }"""
        )
        return name if isinstance(name, str) and name else None
    except Exception as e:
        logger.debug("Could not get current user name: %s", e)
        return None


async def _expand_comments_section(page: Page, max_clicks: int = 5) -> int:
    """Click 'Load more' / 'Ver mais' (comments/replies) to expand the discussion.

    Returns the number of clicks performed.
    """
    from patchright.async_api import TimeoutError as PlaywrightTimeoutError

    click_selectors = [
        'button:has-text("Load more")',
        'button:has-text("Ver mais")',
        'button:has-text("View more")',
        'span:has-text("Load more comments")',
        'span:has-text("Ver mais comentários")',
        'span:has-text("View more replies")',
        'span:has-text("Ver mais respostas")',
        '[aria-label*="more"]',
        '[aria-label*="mais"]',
    ]
    clicks = 0
    for _ in range(max_clicks):
        clicked = False
        for sel in click_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=500):
                    await loc.click(timeout=2000)
                    await asyncio.sleep(0.8)
                    clicks += 1
                    clicked = True
                    break
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass
        if not clicked:
            break
    if clicks:
        logger.debug("Expanded comments section with %d click(s)", clicks)
    return clicks


async def get_my_recent_posts(
    page: Page,
    limit: int = 20,
    since_days: int | None = None,
    max_scrolls: int = 20,
) -> list[dict[str, Any]]:
    """
    Scrape recent posts from the logged-in user's own activity (best-effort).

    Why this exists:
    - The main feed is not a reliable source of *your* full posting history.
    - LinkedIn uses infinite scroll and can drop older DOM nodes.

    Strategy:
    - Prefer the user's own activity shares page (/in/me/detail/recent-activity/shares/)
    - Incrementally scroll and collect unique activity URNs.

    Returns list of dicts with post_url, post_id, text_preview, created_at (best-effort).
    """
    posts: list[dict[str, Any]] = []
    seen_urns: set[str] = set()
    prev_height: int | None = None
    try:
        await wait_for_cooldown()
        await page.goto(_MY_POSTS_URL, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)

        # Incremental scroll + collect. Stop when no new items and scrollHeight stable.
        for _ in range(max_scrolls):
            await detect_rate_limit(page)
            await handle_modal_close(page)

            prev_count = len(posts)

            raw = await page.evaluate(
                _JS_EXTRACT_MY_POSTS,
                limit,
            )

            if isinstance(raw, dict):
                items = raw.get("items") if isinstance(raw.get("items"), list) else []
                new_height = (
                    raw.get("scrollHeight")
                    if isinstance(raw.get("scrollHeight"), (int, float))
                    else None
                )
            else:
                items = raw if isinstance(raw, list) else []
                new_height = None

            for p in items if isinstance(items, list) else []:
                if not isinstance(p, dict):
                    continue
                pid = p.get("post_id")
                url = p.get("post_url")
                if not url or not pid or pid in seen_urns:
                    continue
                seen_urns.add(pid)
                posts.append(
                    {
                        "post_url": url,
                        "post_id": pid,
                        "text_preview": (p.get("text_preview") or "")[:500],
                        "created_at": p.get("created_at"),
                    }
                )
                if len(posts) >= limit:
                    break

            # Stop when no new items and scroll height stable (no more content loading)
            if len(posts) >= limit:
                break
            if (
                len(posts) == prev_count
                and new_height is not None
                and prev_height is not None
                and new_height == prev_height
            ):
                break
            # Legacy single-shot evaluate (returns list): do not loop
            if not isinstance(raw, dict):
                break
            prev_height = new_height

            await scroll_to_bottom(page, pause_time=0.6, max_scrolls=1)
            await asyncio.sleep(0.4)

    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning("get_my_recent_posts extraction failed: %s", e)

    # Filter by since_days when provided
    if since_days is not None and posts:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
        filtered: list[dict[str, Any]] = []
        for p in posts:
            created = p.get("created_at")
            if not created or not isinstance(created, str):
                # Keep posts with unknown dates (best-effort)
                filtered.append(p)
                continue
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if dt >= cutoff:
                    filtered.append(p)
            except (ValueError, TypeError):
                # Unparseable date — keep the post
                filtered.append(p)
        posts = filtered

    return posts


async def get_profile_recent_posts(
    page: Page, username: str, limit: int = 20
) -> list[dict[str, Any]]:
    """
    Scrape recent posts visible on a profile page (e.g. /in/andre-martins-fintech/).

    Navigates to the profile URL, scrolls to load activity, extracts post cards
    that link to /feed/update/urn:li:activity:. Requires being logged in.
    """
    profile_url = f"https://www.linkedin.com/in/{username.strip().strip('/')}/"
    posts: list[dict[str, Any]] = []
    try:
        await wait_for_cooldown()
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.8, max_scrolls=5)
        await asyncio.sleep(0.5)

        raw = await page.evaluate(
            """(limit) => {
            const posts = [];
            const seen = new Set();
            const main = document.querySelector('main');
            if (!main) return posts;
            const links = main.querySelectorAll('a[href*="/feed/update/"]');
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/feed\\/update\\/(urn:li:activity:\\d+)/);
                if (!m || seen.has(m[1])) continue;
                seen.add(m[1]);
                let text = '';
                let card = a.closest('article') || a.closest('[data-urn]') || a.closest('div[class*="feed"]');
                if (card) {
                    const inner = card.querySelector('[dir="ltr"]') || card;
                    text = (inner.innerText || '').trim().slice(0, 500);
                }
                if (!text) text = (a.innerText || '').trim().slice(0, 300);
                const fullUrl = href.startsWith('http') ? href : 'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
                posts.push({ post_url: fullUrl, post_id: m[1], text_preview: text, created_at: null });
                if (posts.length >= limit) break;
            }
            return posts;
        }""",
            limit,
        )

        if isinstance(raw, list):
            for p in raw:
                if isinstance(p, dict) and p.get("post_url"):
                    posts.append(
                        {
                            "post_url": p.get("post_url", ""),
                            "post_id": p.get("post_id"),
                            "text_preview": (p.get("text_preview") or "")[:500],
                            "created_at": p.get("created_at"),
                        }
                    )
    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning(
            "get_profile_recent_posts extraction failed for %s: %s", profile_url, e
        )
    return posts


async def get_feed_posts(
    page: Page,
    limit: int = 20,
    max_scrolls: int = 15,
) -> list[dict[str, Any]]:
    """
    Scrape posts from the logged-in user's LinkedIn home feed.

    Navigates to the main feed, incrementally scrolls, and extracts post cards
    with author info, text previews, and engagement counts (best-effort).

    Args:
        page: Patchright page instance.
        limit: Maximum number of posts to return (default 20).
        max_scrolls: Maximum scroll iterations.

    Returns:
        List of dicts with post_url, post_id, text_preview, author_name,
        author_url, created_at (all best-effort).
    """
    posts: list[dict[str, Any]] = []
    seen_urns: set[str] = set()
    prev_height: int | None = None
    try:
        await wait_for_cooldown()
        await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)

        for _ in range(max_scrolls):
            await detect_rate_limit(page)
            await handle_modal_close(page)

            prev_count = len(posts)

            raw = await page.evaluate(
                _JS_EXTRACT_FEED_POSTS,
                limit,
            )

            if isinstance(raw, dict):
                items = raw.get("items") if isinstance(raw.get("items"), list) else []
                new_height = (
                    raw.get("scrollHeight")
                    if isinstance(raw.get("scrollHeight"), (int, float))
                    else None
                )
            else:
                items = raw if isinstance(raw, list) else []
                new_height = None

            for p in items if isinstance(items, list) else []:
                if not isinstance(p, dict):
                    continue
                pid = p.get("post_id")
                url = p.get("post_url")
                if not url or not pid or pid in seen_urns:
                    continue
                seen_urns.add(pid)
                posts.append(
                    {
                        "post_url": url,
                        "post_id": pid,
                        "text_preview": (p.get("text_preview") or "")[:500],
                        "author_name": p.get("author_name"),
                        "author_url": p.get("author_url"),
                        "created_at": p.get("created_at"),
                    }
                )
                if len(posts) >= limit:
                    break

            if len(posts) >= limit:
                break
            if (
                len(posts) == prev_count
                and new_height is not None
                and prev_height is not None
                and new_height == prev_height
            ):
                break
            if not isinstance(raw, dict):
                break
            prev_height = new_height

            await scroll_to_bottom(page, pause_time=0.6, max_scrolls=1)
            await asyncio.sleep(0.4)

    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning("get_feed_posts extraction failed: %s", e)

    return posts


async def get_post_comments(
    page: Page,
    post_url_or_id: str,
    current_user_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    Scrape top-level comments from a single post.

    Returns list of dicts with comment_id, author_name, author_url, text,
    created_at, comment_permalink, has_reply_from_author (if current_user_name given).
    """
    url = _normalize_post_url(post_url_or_id)
    user_tag = current_user_name or ""
    cache_key = f"comments:{url}:user={user_tag}"
    cached = scraping_cache.get(cache_key)
    if cached is not None:
        return cached

    comments: list[dict[str, Any]] = []
    try:
        await wait_for_cooldown()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=3)
        await asyncio.sleep(1)

        await _expand_comments_section(page, max_clicks=5)

        raw = await page.evaluate(
            _JS_EXTRACT_COMMENTS,
            current_user_name or "",
        )

        if isinstance(raw, list):
            seen_ids: set[str] = set()
            seen_keys: set[tuple[str, str]] = set()
            for c in raw:
                if isinstance(c, dict) and (c.get("author_name") or c.get("text")):
                    cid = c.get("comment_id")
                    if cid:
                        if cid in seen_ids:
                            continue
                        seen_ids.add(cid)
                    else:
                        key = (
                            c.get("author_url", ""),
                            (c.get("text") or "").strip()[:200],
                        )
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                    # Filter ghost entries (text is just the author name)
                    comment_text = (c.get("text") or "").strip()
                    author_name = (c.get("author_name") or "").strip()
                    clean_author = re.sub(r"^View\s+", "", author_name)
                    clean_author = re.sub(
                        "['\\u2019]s\\s+graphic link$", "", clean_author
                    ).strip()
                    if clean_author and comment_text:
                        text_sans_author = comment_text.replace(
                            clean_author, ""
                        ).strip()
                        if len(text_sans_author) < 3:
                            continue
                    out: dict[str, Any] = {
                        "comment_id": c.get("comment_id"),
                        "author_name": c.get("author_name"),
                        "author_url": c.get("author_url"),
                        "text": (c.get("text") or "").strip(),
                        "created_at": c.get("created_at"),
                        "comment_permalink": c.get("comment_permalink"),
                    }
                    if "has_reply_from_author" in c:
                        out["has_reply_from_author"] = c.get("has_reply_from_author")
                    comments.append(out)
    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning("get_post_comments extraction failed for %s: %s", url, e)
    scraping_cache.put(cache_key, comments, ttl=60.0 if not comments else None)
    return comments


async def get_notifications(
    page: Page,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Scrape the LinkedIn notifications page and return structured items.

    Navigates to /notifications/, scrolls to load lazy content, then extracts
    each notification card's text, link, and best-effort type/timestamp.

    Args:
        page: Patchright page instance.
        limit: Maximum number of notifications to return (default 20).

    Returns:
        List of dicts with text, link, type (best-effort category), created_at.
    """
    notifications: list[dict[str, Any]] = []
    try:
        await wait_for_cooldown()
        await page.goto(
            _NOTIFICATIONS_URL, wait_until="domcontentloaded", timeout=30000
        )
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=6)
        await asyncio.sleep(1)

        raw = await page.evaluate(
            _JS_EXTRACT_NOTIFICATIONS,
            limit,
        )

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("text"):
                    notifications.append(
                        {
                            "text": re.sub(
                                r"^Status is \w+\n?",
                                "",
                                (item.get("text") or ""),
                                flags=re.IGNORECASE,
                            ).strip(),
                            "link": item.get("link"),
                            "type": item.get("type", "other"),
                            "created_at": item.get("created_at"),
                        }
                    )
    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning("get_notifications extraction failed: %s", e)
    return notifications


async def _unreplied_via_notifications(
    page: Page, since_days: int, max_posts: int
) -> list[dict[str, Any]] | None:
    """
    Try to get unreplied comments from notifications page.
    Returns list of unreplied comment items or None if notifications path failed.

    Note: since_days is accepted for interface consistency but is NOT enforced
    in the notifications fast path. LinkedIn notifications lack reliable
    machine-readable timestamps, so filtering by age is best-effort only.
    The fallback path (scanning posts) does honor since_days.
    """
    try:
        await wait_for_cooldown()
        await page.goto(
            _NOTIFICATIONS_URL, wait_until="domcontentloaded", timeout=30000
        )
        await detect_rate_limit(page)
        rate_limit_state.record_success()
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=6)
        await asyncio.sleep(1)

        raw = await page.evaluate(
            """(maxItems) => {
            const items = [];
            const main = document.querySelector('main');
            if (!main) return { items: items, hasContent: false };
            // Positive: someone commented/replied on your post
            const commentVerbs = ['commented', 'comentou', 'replied', 'respondeu'];
            // Negative: reactions, likes, mentions — not actionable comments
            const excludeTerms = ['reacted', 'reagiu', 'liked', 'curtiu', 'endorsed', 'recomendou', 'mentioned', 'mencionou', 'reshared', 'compartilhou', 'celebrated', 'comemorou'];
            const cards = main.querySelectorAll('div.nt-card, article, section > div > div');
            const seen = new Set();
            for (const card of cards) {
                const a = card.querySelector('a[href*="commentUrn"]') || card.querySelector('a[href*="/feed/update/"]');
                if (!a) continue;
                const href = (a.getAttribute('href') || '').trim();
                const fullUrl = href.startsWith('http') ? href : 'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
                if (seen.has(fullUrl)) continue;
                seen.add(fullUrl);
                let text = (card.innerText || '').trim();
                // Strip LinkedIn UI artefacts (e.g. "Status is reachable")
                text = text.replace(/^Status is \\w+\\n?/i, '').trim();
                const textLower = text.toLowerCase();
                // Exclude reactions, likes, endorsements, mentions
                const isExcluded = excludeTerms.some(t => textLower.includes(t));
                if (isExcluded) continue;
                // Must contain an explicit comment/reply verb
                const isComment = commentVerbs.some(t => textLower.includes(t));
                if (!isComment) continue;
                items.push({ link: fullUrl, snippet: text.slice(0, 200) });
                if (items.length >= maxItems) break;
            }
            return { items: items, hasContent: main.innerText.trim().length > 100 };
        }""",
            max_posts * 3,
        )

        if isinstance(raw, dict):
            items = raw.get("items", [])
            has_content = raw.get("hasContent", False)
            if isinstance(items, list) and len(items) > 0:
                return [
                    {
                        "comment_permalink": r.get("link"),
                        "post_url": r.get("link").split("?")[0]
                        if r.get("link")
                        else None,
                        "snippet": r.get("snippet"),
                    }
                    for r in items
                    if isinstance(r, dict) and r.get("link")
                ]
            # Page loaded successfully but no comment notifications found
            if has_content:
                return []
    except LinkedInScraperException:
        raise
    except Exception as e:
        logger.warning("Notifications scrape failed, will fallback to posts: %s", e)
    return None


async def find_unreplied_comments(
    page: Page,
    since_days: int = 7,
    max_posts: int = 20,
) -> list[dict[str, Any]]:
    """
    Find comments on the user's posts that have no reply from the logged-in user.

    Prefer: scrape notifications and return comment links (best-effort unreplied).
    Fallback: get my recent posts, then for each post get comments and filter
    those that don't have a reply from the current user. Results ordered by
    most recent first (best-effort).
    """
    unreplied: list[dict[str, Any]] = []
    current_name = await _get_current_user_name(page)

    # 1) Try notifications first
    from_notifications = await _unreplied_via_notifications(page, since_days, max_posts)
    if from_notifications is not None:
        if len(from_notifications) > 0:
            logger.info(
                "Using notifications for unreplied comments (%d items)",
                len(from_notifications),
            )
            for item in from_notifications:
                unreplied.append(
                    {
                        "comment_permalink": item.get("comment_permalink"),
                        "post_url": item.get("post_url"),
                        "snippet": item.get("snippet"),
                        "author_name": None,
                        "text": None,
                    }
                )
        else:
            logger.info(
                "Notifications loaded successfully but no unreplied comments found"
            )
        return unreplied

    # 2) Fallback: scan recent posts and collect comments without our reply.
    #    Cap navigations to avoid triggering rate limits.
    _MAX_FALLBACK_NAVIGATIONS = 5
    logger.info(
        "Fallback: scanning recent posts for unreplied comments (max %d navigations)",
        _MAX_FALLBACK_NAVIGATIONS,
    )
    posts = await get_my_recent_posts(
        page, limit=max_posts, since_days=since_days, max_scrolls=max(10, max_posts)
    )
    await asyncio.sleep(humanized_delay())

    nav_count = 0
    for i, post in enumerate(posts):
        if nav_count >= _MAX_FALLBACK_NAVIGATIONS:
            logger.info(
                "Reached max fallback navigations (%d), stopping",
                _MAX_FALLBACK_NAVIGATIONS,
            )
            break
        if i > 0:
            await asyncio.sleep(humanized_delay())
        post_url = post.get("post_url")
        if not post_url:
            continue
        try:
            comments = await get_post_comments(
                page, post_url, current_user_name=current_name
            )
            nav_count += 1
            for c in comments:
                if c.get("has_reply_from_author"):
                    continue
                unreplied.append(
                    {
                        "comment_id": c.get("comment_id"),
                        "comment_permalink": c.get("comment_permalink") or post_url,
                        "post_url": post_url,
                        "author_name": c.get("author_name"),
                        "text": c.get("text"),
                        "snippet": (c.get("text") or "")[:200],
                    }
                )
        except Exception as e:
            logger.debug("Skip post %s for unreplied: %s", post_url, e)
        if len(unreplied) >= max_posts * 5:
            break

    return unreplied
