"""
Scraping logic for LinkedIn posts and comments (my feed, post comments, unreplied).

Uses page.evaluate and DOM traversal to extract structured data. Best-effort:
post_id/urn, created_at, comment_permalink may be missing if not present in DOM.
"""

import asyncio
import logging
import re
from typing import Any

from patchright.async_api import Page

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.core.utils import (
    detect_rate_limit,
    handle_modal_close,
    scroll_to_bottom,
)

logger = logging.getLogger(__name__)

_NAV_DELAY = 2.0
_FEED_URL = "https://www.linkedin.com/feed/"
_NOTIFICATIONS_URL = "https://www.linkedin.com/notifications/"
_ACTIVITY_URN_PATTERN = re.compile(r"urn:li:activity:(\d+)")
_BASE_POST_URL = "https://www.linkedin.com/feed/update/"


def _normalize_post_url(post_url_or_id: str) -> str:
    """Return canonical post URL from post_url or post_id."""
    s = post_url_or_id.strip()
    if s.startswith("http"):
        return s
    # Allow raw numeric id or urn
    match = _ACTIVITY_URN_PATTERN.search(s)
    if match:
        return f"{_BASE_POST_URL}urn:li:activity:{match.group(1)}/"
    if s.isdigit():
        return f"{_BASE_POST_URL}urn:li:activity:{s}/"
    return f"{_BASE_POST_URL}{s}/" if not s.endswith("/") else f"{_BASE_POST_URL}{s}"


async def _get_current_user_name(page: Page) -> str | None:
    """Try to get current user display name from nav (for reply detection)."""
    try:
        name = await page.evaluate(
            """() => {
            const sel = 'nav a[href*="/in/"]:not([href*="/in/me"])';
            const a = document.querySelector(sel);
            if (!a) return null;
            const text = (a.getAttribute('aria-label') || a.textContent || '').trim();
            return text || null;
        }"""
        )
        return name if isinstance(name, str) and name else None
    except Exception as e:
        logger.debug("Could not get current user name: %s", e)
        return None


async def get_my_recent_posts(page: Page, limit: int = 20) -> list[dict[str, Any]]:
    """
    Scrape recent posts from the logged-in user's feed (own posts best-effort).

    Navigates to feed, scrolls to load content, extracts post cards that link to
    /feed/update/urn:li:activity:. Returns list of dicts with post_url, post_id,
    text_preview, created_at (best-effort).
    """
    posts: list[dict[str, Any]] = []
    try:
        await page.goto(_FEED_URL, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
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
        logger.warning("get_my_recent_posts extraction failed: %s", e)
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
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
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
        logger.warning("get_profile_recent_posts extraction failed for %s: %s", profile_url, e)
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
    comments: list[dict[str, Any]] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=3)
        await asyncio.sleep(1)

        raw = await page.evaluate(
            """(postAuthorName) => {
            const comments = [];
            const main = document.querySelector('main');
            if (!main) return comments;
            const commentBlocks = main.querySelectorAll('[class*="comment"], [data-id*="comment"], section');
            const nameToCheck = (postAuthorName || '').trim().toLowerCase();
            for (const block of commentBlocks) {
                const authorLink = block.querySelector('a[href*="/in/"]');
                if (!authorLink) continue;
                const authorUrl = (authorLink.getAttribute('href') || '').trim();
                if (authorUrl.includes('/in/me') || !authorUrl) continue;
                const authorName = (authorLink.innerText || authorLink.getAttribute('aria-label') || '').trim();
                const textEl = block.querySelector('[dir="ltr"]') || block.querySelector('span');
                const text = (textEl ? textEl.innerText : block.innerText) || '';
                const cleanText = text.replace(authorName, '').trim().slice(0, 2000);
                let commentId = null;
                const permLink = block.querySelector('a[href*="commentUrn"]');
                if (permLink) commentId = (permLink.getAttribute('href') || '').match(/commentUrn=([^&]+)/)?.[1] || null;
                let hasReplyFromAuthor = false;
                if (nameToCheck) {
                    const replyBlocks = block.querySelectorAll('[class*="reply"], [class*="comment"]');
                    for (const r of replyBlocks) {
                        if (r === block) continue;
                        const replyLink = r.querySelector('a[href*="/in/"]');
                        if (!replyLink) continue;
                        const replyName = (replyLink.innerText || '').trim().toLowerCase();
                        if (replyName && nameToCheck && replyName.indexOf(nameToCheck) !== -1) hasReplyFromAuthor = true;
                    }
                }
                comments.push({
                    comment_id: commentId,
                    author_name: authorName || null,
                    author_url: authorUrl.startsWith('http') ? authorUrl : 'https://www.linkedin.com' + (authorUrl.startsWith('/') ? authorUrl : '/' + authorUrl),
                    text: cleanText,
                    created_at: null,
                    comment_permalink: permLink ? (permLink.getAttribute('href') || '').trim() : null,
                    has_reply_from_author: hasReplyFromAuthor
                });
            }
            return comments;
        }""",
            current_user_name or "",
        )

        if isinstance(raw, list):
            for c in raw:
                if isinstance(c, dict) and (c.get("author_name") or c.get("text")):
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
    return comments


async def _unreplied_via_notifications(
    page: Page, since_days: int, max_posts: int
) -> list[dict[str, Any]] | None:
    """
    Try to get unreplied comments from notifications page.
    Returns list of unreplied comment items or None if notifications path failed.
    """
    try:
        await page.goto(_NOTIFICATIONS_URL, wait_until="domcontentloaded", timeout=30000)
        await detect_rate_limit(page)
        await handle_modal_close(page)
        await scroll_to_bottom(page, pause_time=0.5, max_scrolls=3)
        await asyncio.sleep(1)

        raw = await page.evaluate(
            """(maxItems) => {
            const items = [];
            const main = document.querySelector('main');
            if (!main) return items;
            const links = main.querySelectorAll('a[href*="/feed/update/"], a[href*="commentUrn"]');
            const seen = new Set();
            for (const a of links) {
                const href = (a.getAttribute('href') || '').trim();
                const fullUrl = href.startsWith('http') ? href : 'https://www.linkedin.com' + (href.startsWith('/') ? href : '/' + href);
                if (seen.has(fullUrl)) continue;
                seen.add(fullUrl);
                const text = (a.closest('li') || a.closest('div')).innerText || '';
                if (!text.toLowerCase().includes('comment') && !href.includes('comment')) continue;
                items.push({ link: fullUrl, snippet: text.slice(0, 200) });
                if (items.length >= maxItems) break;
            }
            return items;
        }""",
            max_posts * 3,
        )

        if isinstance(raw, list) and len(raw) > 0:
            return [
                {
                    "comment_permalink": r.get("link"),
                    "post_url": r.get("link").split("?")[0] if r.get("link") else None,
                    "snippet": r.get("snippet"),
                }
                for r in raw
                if isinstance(r, dict) and r.get("link")
            ]
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
    from_notifications = await _unreplied_via_notifications(
        page, since_days, max_posts
    )
    if from_notifications is not None and len(from_notifications) > 0:
        logger.info("Using notifications for unreplied comments (%d items)", len(from_notifications))
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
        return unreplied

    # 2) Fallback: scan recent posts and collect comments without our reply
    logger.info("Fallback: scanning recent posts for unreplied comments")
    posts = await get_my_recent_posts(page, limit=max_posts)
    await asyncio.sleep(_NAV_DELAY)

    for i, post in enumerate(posts):
        if i > 0:
            await asyncio.sleep(_NAV_DELAY)
        post_url = post.get("post_url")
        if not post_url:
            continue
        try:
            comments = await get_post_comments(page, post_url, current_user_name=current_name)
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
