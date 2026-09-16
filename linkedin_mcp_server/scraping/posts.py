"""Post-content workflows: content search, saved items, and post detail."""

from __future__ import annotations

from typing import Any, Literal, cast
from urllib.parse import unquote, urlparse

import logging
import re

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
    RateLimitError,
)
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    FilterValidationError,
)
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    classify_link,
    normalize_url,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.search_urls import build_content_search_url
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5

SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"
LINKEDIN_BASE_URL = "https://www.linkedin.com"

EnrichLevel = Literal["none", "truncated", "all"]
_ENRICH_LEVELS: tuple[EnrichLevel, ...] = ("none", "truncated", "all")

# Progress while scrolling the saved-items list, counted from anchors in
# <main>. ``/feed/update/`` covers posts, ``/pulse/`` covers articles, and a
# URL pattern is locale-independent — the page offers no countable container
# structure to lean on instead. ``?start=`` offsets are a no-op on this
# surface: verified live on 2026-09-15, where ``?start=10`` returned the
# first ten items again (unlike saved-jobs, which paginates by offset).
_SAVED_ITEM_ANCHOR_COUNT_JS = r"""() => {
    const main = document.querySelector('main') || document.body;
    return main.querySelectorAll(
        'a[href*="/feed/update/"], a[href*="/pulse/"]'
    ).length;
}"""

# One saved item per permalink anchor. Everything read here is either a URL
# or the presence of an attribute, so no locale-dependent text decides what
# an item is or whether it is cut:
#
# - the item container is the anchor's ``<li>``: measured live on
#   2026-09-15, every item's ``li`` held exactly one permalink anchor;
# - ``author`` is the name span LinkedIn marks ``aria-hidden`` beside its
#   visually-hidden "view X's profile" twin, which is the only place on the
#   card where the name appears without surrounding UI wording;
# - ``truncated`` is the expander button, the one button in an item carrying
#   ``aria-label`` *without* ``aria-expanded`` (the overflow menu has
#   ``aria-expanded``). Measured on the saved-items list only — a feed or
#   post-detail page adds react/comment/share buttons of the same shape, so
#   this selector must not be reused there;
# - ``preview`` is the link-preview card, which LinkedIn renders as a second
#   anchor to the same permalink whose text is "title\ndomain".
_SAVED_ITEMS_JS = r"""() => {
    const main = document.querySelector('main') || document.body;
    const anchors = Array.from(main.querySelectorAll(
        'a[href*="/feed/update/"], a[href*="/pulse/"]'
    ));
    const seen = new Set();
    const items = [];
    for (const anchor of anchors) {
        const item = anchor.closest('li');
        if (!item || seen.has(item)) continue;
        seen.add(item);
        const name = item.querySelector('a[href*="/in/"] span[aria-hidden="true"]');
        const preview = Array.from(item.querySelectorAll(
            'a[href*="/feed/update/"], a[href*="/pulse/"]'
        )).map(card => (card.innerText || '').trim()).find(Boolean) || '';
        items.push({
            href: anchor.href || anchor.getAttribute('href') || '',
            text: (item.innerText || '').trim(),
            author: name ? (name.innerText || '').trim() : '',
            preview: preview,
            truncated: Boolean(
                item.querySelector('button[aria-label]:not([aria-expanded])')
            ),
        });
    }
    return { text: (main.innerText || '').trim(), items: items };
}"""

# The post-detail page renders the body in full, so the primitive behind
# enrichment is one navigation and one read. What it must not read is the
# rest of the page: the author header, the follow/translate/react controls
# and the entire comment thread are innerText too, and a body drowned in
# them is not a body.
#
# The page addresses its own content, and every signal used here is a URN or
# an attribute, never a class or a label (measured live on 2026-09-16):
#
# - the post is `[role="article"][data-urn="urn:li:activity:<id>"]`, matched
#   against the URN the caller asked for, so a page carrying several updates
#   cannot answer with the wrong one;
# - each comment is a `urn:li:comment:(activity:…,…)` element **inside** that
#   post element, which is why comments are excluded by their own URN rather
#   than by being outside the post. It announces that URN under `data-id`,
#   not `data-urn` — measured, and the reason both attributes are matched:
#   scoping on `data-urn` alone returned the whole thread;
# - the body blocks carry `dir` (LinkedIn marks user-authored text with its
#   direction); a `dir` element inside an anchor is the author card or the
#   link-preview card, not prose. A reshare's quoted body carries no comment
#   URN of its own and is kept on purpose.
#
# Images and links are read from the post element, minus the same comment
# subtrees: a commenter's attachment and links are not the post's.
_POST_DETAIL_JS = r"""({ urn }) => {
    const main = document.querySelector('main') || document.body;
    const pageText = (main.innerText || '').trim();
    const root = Array.from(
        main.querySelectorAll('[role="article"][data-urn]')
    ).find(el => el.getAttribute('data-urn') === urn) || null;
    const commentSelector =
        '[data-id^="urn:li:comment"], [data-urn^="urn:li:comment"]';
    const inComment = el => Boolean(el.closest(commentSelector));

    // A `dir` block inside an anchor is a card, not prose — except one: a
    // reshare wraps the quoted post in an anchor to *that post's*
    // `/feed/update/` permalink, which is how the quoted body reaches the
    // reader at all (measured on 2026-09-16). The wrapper is told apart from
    // the author and link-preview cards by where it points, never by its
    // text, and `/pulse/` is deliberately not in the exception: an article
    // card points there too, and its title belongs to `preview`, not to the
    // body.
    const resharedPostHref = /\/feed\/update\//;
    const inCardAnchor = el => {
        const anchor = el.closest('a');
        if (!anchor) return false;
        return !resharedPostHref.test(
            anchor.getAttribute('href') || anchor.href || ''
        );
    };

    // Screen-reader-only labels inside the body. LinkedIn prefixes every
    // hashtag anchor with a hidden word ("Hashtag"), which innerText reports
    // on a line of its own, and that word is localized. They are recognized
    // by how they are rendered — clipped to nothing, or one pixel square and
    // taken out of flow — never by the class that does it or the word it
    // says, and are handed to Python as exact lines to drop.
    const isScreenReaderOnly = el => {
        const style = getComputedStyle(el);
        if (style.clip === 'rect(0px, 0px, 0px, 0px)') return true;
        return (
            style.position === 'absolute' &&
            parseFloat(style.width) <= 1 &&
            parseFloat(style.height) <= 1
        );
    };

    let text = '';
    let firstBody = null;
    const hiddenLabels = new Set();
    if (root) {
        const blocks = Array.from(root.querySelectorAll('[dir]')).filter(
            el => !inCardAnchor(el) && !inComment(el)
        );
        const outermost = blocks.filter(
            el => !blocks.some(other => other !== el && other.contains(el))
        );
        firstBody = outermost[0] || null;
        text = outermost
            .map(el => (el.innerText || '').trim())
            .filter(Boolean)
            .join('\n\n');
        for (const block of outermost) {
            for (const el of block.querySelectorAll('*')) {
                if (!isScreenReaderOnly(el)) continue;
                const label = (el.textContent || '').trim();
                if (label) hiddenLabels.add(label);
            }
        }
    }

    // The author card sits above the body and carries links of its own — the
    // profile, and the website button an author with a page gets. Neither is
    // something the post points at, and both are indistinguishable from a
    // body link by host. Position separates them: the body's own links and
    // the link-preview card below it follow the first body block, the header
    // precedes it. A post with no text keeps everything, having no header to
    // tell apart.
    const ownLink = link =>
        !firstBody ||
        firstBody.contains(link) ||
        Boolean(
            firstBody.compareDocumentPosition(link) &
                Node.DOCUMENT_POSITION_FOLLOWING
        );

    const scope = root || main;
    return {
        text: text,
        page_text: pageText,
        scoped: Boolean(root),
        hidden_labels: Array.from(hiddenLabels),
        images: Array.from(scope.querySelectorAll('img[src]'))
            .filter(img => !inComment(img))
            .map(img => img.src),
        links: Array.from(scope.querySelectorAll('a[href]'))
            .filter(link => !inComment(link) && ownLink(link))
            .map(link => link.href),
    };
}"""

_MAX_SAVED_POSTS_SCROLLS = 12
_MAX_SAVED_POSTS_STALE = 3

# Post images live on LinkedIn's media CDN, and so does every author avatar,
# commenter thumbnail and image attached to a comment on the same page. The
# distinguishing mark is the path segment LinkedIn names the rendition after,
# not the host, and it is not locale-dependent. Measured live on 2026-09-15:
# a post's own media is `feedshare-*` or `articleshare-*`, while the
# renditions below belong to people and to comments, never to the post.
_MEDIA_IMAGE_HOSTS = ("media.licdn.com", "dms.licdn.com")
_NON_CONTENT_IMAGE_MARKERS = (
    "profile-displayphoto",
    "profile-framedphoto",
    "company-logo",
    "comment-image",
)

# A saved item's canonical identity. ``/feed/update/<urn>/`` carries the
# activity URN that ``read_post`` takes; ``/pulse/<slug>/`` articles have no
# URN, so their permalink is their identity.
_FEED_PERMALINK_RE = re.compile(r"^/feed/update/([^/?#]+)")
_ACTIVITY_URN_RE = re.compile(r"^urn:li:[a-zA-Z]+:[0-9]+$")

# A link-preview card ends in its source domain. Matching a hostname shape
# rather than a word keeps the split locale-independent: LinkedIn's own
# article shares end in a sentence ("… auf LinkedIn • Lesedauer 4 Min."),
# which fails this pattern and stays part of the title.
_PREVIEW_DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE
)


class PostSearch:
    """Own the workflows whose subject is LinkedIn post content.

    Content search is a single scrolled capture, while saved items need
    their own scroll loop with an anchor-count progress signal. Both stay
    here so the facade sees one owner for post-shaped surfaces.
    """

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
        capture: SectionCapture,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content
        self._capture = capture

    async def search_posts(
        self,
        keywords: str,
        date_posted: str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Search LinkedIn posts/content and extract the results page.

        Reproduces the LinkedIn "Posts" content-search tab — the surface for
        catching informal "we're hiring" / "Buscamos ..." posts before a
        formal job listing exists.

        Args:
            keywords: Free-text query (e.g. "Buscamos Unity", "estamos contratando").
            date_posted: Optional recency filter, one of the keys of
                ``search_urls.CONTENT_DATE_POSTED_MAP``. Invalid values raise
                ``FilterValidationError`` (a ``ValueError`` subclass) rather
                than reaching LinkedIn, which would ignore them silently and
                return unfiltered results that look filtered.
            max_pages: Scroll depth, expressed in result "pages" of roughly
                ``_CONTENT_SCROLLS_PER_REQUESTED_PAGE`` scrolls each (default
                3). Content search is an infinite scroll with no per-page URL,
                so this caps how far the page is scrolled rather than fetching
                discrete ``&start=`` pages.

        Returns:
            {url, sections: {search_results: text}} plus optional ``references``
            (post authors, companies, linked jobs) and ``section_errors``.
            Verified live: the results page carries no per-post permalink
            anchors, so a post is addressable only through its author.
            The LLM should parse the raw text to extract each post's author,
            headline, body, date, and reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(CaptureMode.SEARCH_RESULTS, max_scrolls),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = {
                "error_type": "rate_limit",
                "error_message": extracted.text,
            }
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {"url": url, "sections": sections}
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_saved_posts(
        self,
        num_posts: int = 10,
        enrich: str = "none",
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """List the authenticated user's saved posts and articles.

        Navigates to ``/my-items/saved-posts/`` and scrolls until at least
        ``num_posts`` saved-item anchors are present in ``<main>``, or the
        list stops growing. Saved posts carry ``/feed/update/<urn>/``
        permalinks, saved articles ``/pulse/<slug>/`` ones; both become
        items. The count signal counts in-page anchors, so no
        locale-dependent text decides when to stop.

        The listing renders a cut body for most items. ``enrich`` re-reads
        the post-detail page, which renders the body in full:

        - ``"none"`` (default): listing only, one navigation.
        - ``"truncated"``: re-read the items the listing cut.
        - ``"all"``: re-read every item, which is the only level that
          reports images for an item whose listing text was complete.

        Enrichment costs one navigation per item, so a full page of saved
        posts is a batch operation rather than an interactive one. Failures
        are isolated per item; a rate limit stops the loop and reports what
        was already read.

        Args:
            num_posts: How many saved items to scroll to (the ceiling the
                tool enforces is 50).
            enrich: One of ``none``, ``truncated``, ``all``. Anything else
                raises ``FilterValidationError`` rather than silently
                reading the listing only.
            callbacks: Optional progress sink, called once per enriched item.

        Returns:
            {url, saved_posts: [item, ...]} plus optional ``section_errors``.
            Each item carries ``kind``, ``permalink``, ``text``,
            ``truncated``, and — when the card offers them — ``urn``,
            ``author`` and ``preview``. Enriched items carry the detail
            page's ``text`` with ``truncated`` false, plus ``images`` and
            ``links``; an item whose re-read failed carries ``error``.
        """
        level = _validated_enrich(enrich)
        try:
            items = await self._collect_saved_items(num_posts)
        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract saved posts: %s", e)
            return {
                "url": SAVED_POSTS_URL,
                "saved_posts": [],
                "section_errors": {
                    "saved_posts": build_issue_diagnostics(
                        e, context="extract_saved_posts"
                    )
                },
            }

        if items is None:
            return {
                "url": SAVED_POSTS_URL,
                "saved_posts": [],
                "section_errors": {
                    "saved_posts": {
                        "error_type": "rate_limit",
                        "error_message": RATE_LIMITED_SECTION_TEXT,
                    }
                },
            }

        result: dict[str, Any] = {"url": SAVED_POSTS_URL, "saved_posts": items}
        if level != "none":
            rate_limited = await self._enrich_saved_items(items, level, callbacks)
            if rate_limited:
                result["section_errors"] = {
                    "saved_posts": {
                        "error_type": "rate_limit",
                        "error_message": RATE_LIMITED_SECTION_TEXT,
                    }
                }
        return result

    async def read_post(self, urn: str) -> dict[str, Any]:
        """Read one post or article in full from its permalink page.

        Args:
            urn: An activity URN (``urn:li:activity:123``), a permalink path
                (``/feed/update/<urn>/``, ``/pulse/<slug>/``) or the full
                LinkedIn URL of either. Anything else raises
                ``InvalidReferenceError``.

        Returns:
            {url, text, images, links}. ``text`` is the detail page's body,
            which LinkedIn renders untruncated, so no expander is clicked.
            ``images`` are the post's own media CDN URLs — signed and
            expiring, so a caller that wants the bytes must fetch them
            promptly — and ``links`` are the non-LinkedIn URLs in the post,
            returned as LinkedIn serves them (``lnkd.in`` shortlinks
            included, unresolved).
        """
        return await self._scrape_post_detail(urn)

    async def _enrich_saved_items(
        self,
        items: list[dict[str, Any]],
        level: EnrichLevel,
        callbacks: ProgressCallback | None,
    ) -> bool:
        """Re-read the selected items in place; report whether a limit hit.

        A dead or unreadable permalink is that item's problem and nothing
        else's, so it lands in the item's ``error`` and the loop continues.
        A rate limit is the session's problem: the loop stops instead of
        walking the rest of the list into the same wall.
        """
        targets = [
            item for item in items if level == "all" or item.get("truncated", False)
        ]
        for index, item in enumerate(targets, start=1):
            if callbacks:
                await callbacks.on_progress(
                    f"Reading saved post {index}/{len(targets)}",
                    int(index * 100 / len(targets)),
                )
            try:
                detail = await self._scrape_post_detail(item["permalink"])
            except RateLimitError:
                logger.warning(
                    "Rate limited after enriching %d of %d saved posts",
                    index - 1,
                    len(targets),
                )
                return True
            except AuthenticationError:
                raise
            except Exception as e:
                logger.debug("Enriching %s failed: %s", item["permalink"], e)
                item["error"] = build_issue_diagnostics(e, context="read_post")
                continue

            item["text"] = detail["text"]
            item["truncated"] = False
            item["images"] = detail["images"]
            item["links"] = detail["links"]
        return False

    async def _scrape_post_detail(self, reference: str) -> dict[str, Any]:
        """Navigate to one post's permalink and read text, images and links."""
        url, urn = _post_target(reference)
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        await self._session.dismiss_modal()

        payload = await page.evaluate(_POST_DETAIL_JS, {"urn": urn or ""})
        page_text = payload.get("page_text", "") or ""
        if page_text.strip() and not truncate_linkedin_noise(page_text):
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)", url
            )
            raise RateLimitError(RATE_LIMITED_SECTION_TEXT)

        # A post element was found: its text is the body and needs no chrome
        # trimming. Without one — an article page, or a layout that stopped
        # carrying the URN — the whole page is all there is, and the noise
        # filters are what keep it readable.
        if payload.get("scoped"):
            text = _without_hidden_labels(
                payload.get("text") or "", payload.get("hidden_labels") or []
            )
        else:
            logger.debug("No post element for %s; falling back to page text", url)
            text = filter_linkedin_noise_lines(truncate_linkedin_noise(page_text))

        return {
            "url": url,
            "text": text,
            "images": _content_images(payload.get("images") or []),
            "links": _external_links(payload.get("links") or []),
        }

    async def _collect_saved_items(self, num_posts: int) -> list[dict[str, Any]] | None:
        """Navigate, scroll until enough saved items, and read them.

        Returns ``None`` for a page that carried nothing but LinkedIn
        chrome, which is what a rate-limited read of this surface looks
        like — distinct from an empty list, which means nothing is saved.
        """
        page = self._session.page
        await self._navigator._navigate_to_page(SAVED_POSTS_URL)
        await self._session.check_rate_limit()

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", SAVED_POSTS_URL)

        await self._session.dismiss_modal()

        stale_count = 0
        for scroll in range(_MAX_SAVED_POSTS_SCROLLS):
            count = await page.evaluate(_SAVED_ITEM_ANCHOR_COUNT_JS)
            logger.debug("Saved posts scroll %d: %d item anchors", scroll, count)
            if count >= num_posts:
                break

            # window.scrollBy advances this list; the home feed needs
            # mouse.wheel because it scrolls a container of its own.
            await page.evaluate("window.scrollBy(0, 2000)")
            await self._session.delay(1.0)

            new_count = await page.evaluate(_SAVED_ITEM_ANCHOR_COUNT_JS)
            if new_count > count:
                stale_count = 0
            else:
                stale_count += 1
                logger.debug(
                    "Saved posts stale scroll %d/%d (still at %d item anchors)",
                    stale_count,
                    _MAX_SAVED_POSTS_STALE,
                    new_count,
                )
                if stale_count >= _MAX_SAVED_POSTS_STALE:
                    logger.debug("Saved posts list stopped growing")
                    break

        payload = await page.evaluate(_SAVED_ITEMS_JS)
        items = [
            item
            for item in (_saved_item(raw) for raw in payload.get("items") or [])
            if item is not None
        ][:num_posts]
        if items:
            return items

        raw_text = payload.get("text") or ""
        if raw_text.strip() and not truncate_linkedin_noise(raw_text):
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)",
                SAVED_POSTS_URL,
            )
            return None
        return []


def _validated_enrich(enrich: str) -> EnrichLevel:
    """Refuse an unknown level instead of quietly reading the listing only."""
    if enrich not in _ENRICH_LEVELS:
        raise FilterValidationError(
            f"Invalid enrich level: {enrich}. Valid: {', '.join(_ENRICH_LEVELS)}"
        )
    return cast(EnrichLevel, enrich)


def _saved_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Shape one saved-list card into an item, or drop it if unaddressable."""
    href = (raw.get("href") or "").strip()
    normalized = normalize_url(href) if href else None
    classified = classify_link(normalized) if normalized else None
    if not classified or classified[0] not in ("feed_post", "article"):
        return None

    kind, permalink = classified
    item: dict[str, Any] = {"kind": kind, "permalink": permalink}
    if match := _FEED_PERMALINK_RE.match(permalink):
        item["urn"] = unquote(match.group(1))

    author = (raw.get("author") or "").strip()
    if author:
        item["author"] = author

    preview = _preview(raw.get("preview") or "")
    if preview:
        item["preview"] = preview

    item["text"] = filter_linkedin_noise_lines(raw.get("text") or "")
    item["truncated"] = bool(raw.get("truncated"))
    return item


def _preview(text: str) -> dict[str, str] | None:
    """Split a link-preview card into its title and its source domain.

    ``domain`` is the routing signal a caller needs — its presence means the
    post points at content living somewhere else — so it is only reported
    when the card's last line is shaped like a hostname. LinkedIn's own
    article shares end in a prose line instead, which stays in the title.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    preview: dict[str, str] = {}
    if len(lines) > 1 and _PREVIEW_DOMAIN_RE.match(lines[-1]):
        preview["domain"] = lines[-1].lower()
        lines = lines[:-1]
    title = " ".join(lines).strip()
    if title:
        preview["title"] = title
    return preview or None


def _without_hidden_labels(text: str, labels: list[str]) -> str:
    """Drop the lines a screen-reader-only element put in the body.

    innerText reports a hidden label on a line of its own, so the match is
    whole-line: a hashtag's "Hashtag" prefix goes, the "#topic" beneath it
    stays, and a body that happens to contain the same word mid-sentence is
    untouched. The labels come from the page rather than from a table here
    because every one of them is localized.
    """
    hidden = {label.strip() for label in labels if label.strip()}
    if not hidden:
        return text.strip()
    kept = [line for line in text.splitlines() if line.strip() not in hidden]
    return "\n".join(kept).strip()


def _content_images(sources: list[str]) -> list[str]:
    """Keep a post's own media, dropping avatars, logos and inline assets."""
    images: list[str] = []
    for source in sources:
        host = urlparse(source).netloc.lower()
        if host not in _MEDIA_IMAGE_HOSTS:
            continue
        if any(marker in source for marker in _NON_CONTENT_IMAGE_MARKERS):
            continue
        if source not in images:
            images.append(source)
    return images


def _external_links(hrefs: list[str]) -> list[str]:
    """Keep the non-LinkedIn URLs a post points at, in page order."""
    links: list[str] = []
    for href in hrefs:
        normalized = normalize_url(href)
        if not normalized:
            continue
        classified = classify_link(normalized)
        if not classified or classified[0] != "external":
            continue
        if normalized not in links:
            links.append(normalized)
    return links


def _post_target(reference: str) -> tuple[str, str | None]:
    """Resolve a URN, permalink path or URL into a URL and its activity URN.

    The URN travels with the URL because the page addresses its own post by
    it: without one — a ``/pulse/`` article — the reader has no post element
    to scope to and falls back to the whole page.
    """
    value = (reference or "").strip()
    if not value:
        raise InvalidReferenceError(
            "Empty post reference. Pass an activity URN (urn:li:activity:123) "
            "or a permalink such as /feed/update/urn:li:activity:123/."
        )

    if _ACTIVITY_URN_RE.match(value):
        return f"{LINKEDIN_BASE_URL}/feed/update/{value}/", value

    candidate = value if value.startswith("http") else f"{LINKEDIN_BASE_URL}{value}"
    normalized = normalize_url(candidate) if value.startswith(("http", "/")) else None
    classified = classify_link(normalized) if normalized else None
    if not classified or classified[0] not in ("feed_post", "article"):
        raise InvalidReferenceError(
            f"Unusable post reference: {reference}. Pass an activity URN "
            "(urn:li:activity:123), a /feed/update/ or /pulse/ permalink, or "
            "the LinkedIn URL of either."
        )

    permalink = classified[1]
    urn = None
    if match := _FEED_PERMALINK_RE.match(permalink):
        candidate_urn = unquote(match.group(1))
        urn = candidate_urn if _ACTIVITY_URN_RE.match(candidate_urn) else None
    return f"{LINKEDIN_BASE_URL}{permalink}", urn
