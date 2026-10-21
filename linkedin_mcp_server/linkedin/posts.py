"""Content-search workflow behind the LinkedIn "Posts" tab."""

from __future__ import annotations

import re
from typing import Any

from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.search_urls import build_content_search_url

# Content search is an infinite scroll with no ``&start=`` pagination, so
# ``max_pages`` caps scroll depth instead of fetching discrete pages. One
# nominal "page" is this many scrolls.
_CONTENT_SCROLLS_PER_REQUESTED_PAGE = 5


class PostSearch:
    """Own workflows whose subject is LinkedIn post content and interactions.

    Handles content search across posts as well as browser-UI post commenting.
    """

    def __init__(
        self,
        capture: SectionCapture,
        session: Any | None = None,
        navigator: Any | None = None,
    ):
        self._capture = capture
        self._session = session
        self._navigator = navigator

    def normalize_post_url(self, post_permalink: str) -> str:
        """Resolve a post permalink, activity URN, or slug to a canonical URL."""
        permalink = post_permalink.strip()
        # If already a full post URL, preserve it directly to prevent breaking share/slug routes
        if permalink.startswith("https://www.linkedin.com/posts/") or permalink.startswith("http://www.linkedin.com/posts/"):
            return permalink
        m = re.search(r"activity[-:]([0-9]+)", permalink)
        if m:
            return f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
        if permalink.startswith("http://") or permalink.startswith("https://"):
            return permalink
        if permalink.startswith("/posts/"):
            return f"https://www.linkedin.com{permalink}"
        if permalink.startswith("/"):
            return f"https://www.linkedin.com{permalink}"
        return f"https://www.linkedin.com/posts/{permalink}"

    async def post_comment(
        self,
        post_permalink: str,
        comment_text: str,
        confirm_post: bool = True,
    ) -> dict[str, Any]:
        """Post a comment to a LinkedIn post via browser UI automation."""
        if not comment_text or not comment_text.strip():
            raise ValueError("comment_text cannot be empty")

        target_url = self.normalize_post_url(post_permalink)

        if not confirm_post:
            return {
                "status": "confirmation_required",
                "url": target_url,
                "comment_text": comment_text,
                "message": "Set confirm_post=True to post the comment.",
            }

        page = getattr(self._session, "page", None) if self._session else None
        if page is None:
            raise RuntimeError("No active browser page available for post_comment")

        await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Locate the Tiptap/ProseMirror comment editor
        editor = page.locator('div[role="textbox"][contenteditable="true"]').first
        await editor.scroll_into_view_if_needed()
        await editor.click()
        await page.wait_for_timeout(500)

        # Type comment text into the editor
        await page.keyboard.type(comment_text, delay=2)
        await page.wait_for_timeout(1000)

        # Find the submit Comment button that follows the editor in DOM order
        submit_btn_handle = await page.evaluate_handle(
            """() => {
                const editor = document.querySelector('div[role="textbox"][contenteditable="true"]');
                const commentButtons = Array.from(document.querySelectorAll('button')).filter(b => b.innerText.trim() === 'Comment');
                const submitBtn = commentButtons.find(b => (editor.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING));
                return submitBtn || null;
            }"""
        )

        if not submit_btn_handle or not submit_btn_handle.as_element():
            raise RuntimeError("Submit Comment button not found on post page")

        button_el = submit_btn_handle.as_element()
        is_disabled = await button_el.evaluate(
            'b => b.disabled || b.getAttribute("aria-disabled") === "true"'
        )
        if is_disabled:
            raise RuntimeError("Submit Comment button remains disabled after typing")

        await button_el.click()
        await page.wait_for_timeout(4000)

        # Confirm comment is visible in post content
        content = await page.content()
        snippet = comment_text.strip()[:40]
        verified = snippet in content

        return {
            "status": "posted",
            "url": page.url,
            "confirmed": verified,
            "comment_text": comment_text,
        }

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
            (post authors, companies, linked jobs, and ``feed_post`` permalinks
            read from the payload responses) and ``section_errors``.
            Verified live: the results page renders no per-post permalink
            anchors in the DOM; the permalinks come from the JSON/document
            responses instead, in either ``/feed/update/<urn>/`` or
            ``/posts/<slug>`` form (both valid). The LLM should parse the raw
            text to extract each post's author, headline, body, date, and
            reaction counts.
        """
        # Builds before it navigates, so a recency filter LinkedIn would
        # ignore is refused rather than answered with unfiltered results.
        url = build_content_search_url(keywords, date_posted=date_posted)
        max_scrolls = max(1, max_pages) * _CONTENT_SCROLLS_PER_REQUESTED_PAGE
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(
                CaptureMode.SEARCH_RESULTS | CaptureMode.POST_PERMALINKS,
                max_scrolls,
            ),
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
