"""
LinkedIn individual post scraping tool.

Fetches the full text and downloads any attached images from a single
LinkedIn post URL. Images are saved to a temp directory and returned
as local file paths alongside the post content.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG
from linkedin_mcp_server.scraping.link_metadata import Reference

logger = logging.getLogger(__name__)

# LinkedIn post URL patterns that are allowed
_ALLOWED_LINKEDIN_HOSTS = {"www.linkedin.com", "linkedin.com"}
_ALLOWED_POST_PATHS = ("/feed/update/", "/posts/")

# Maximum images to capture per post (prevents timeout on long pages)
_MAX_POST_IMAGES = 10

# Minimum image dimension to be considered a post attachment (not icon/avatar)
_MIN_IMAGE_SIZE_PX = 100

# CSS selectors scoped to the primary post container on a post permalink page
_POST_IMAGE_SELECTORS = (
    # Post permalink page — main post media container
    ".update-components-image img",
    ".feed-shared-image img",
    ".update-components-linkedin-video__embed img",
    # Document/article attachment thumbnails
    ".feed-shared-document__container img",
    # Fallback: any large image inside the top-level post article only
    "article.feed-shared-update-v2 img",
)


def _validate_linkedin_post_url(url: str) -> str:
    """Validate that url is a LinkedIn post permalink. Returns cleaned url.

    Raises ValueError with a user-visible message if validation fails.
    """
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        raise ValueError(f"Invalid URL: {url!r}")

    if parsed.hostname not in _ALLOWED_LINKEDIN_HOSTS:
        raise ValueError(
            f"Only LinkedIn post URLs are accepted "
            f"(got host {parsed.hostname!r}). "
            f"Expected www.linkedin.com/feed/update/... or www.linkedin.com/posts/..."
        )

    if not any(parsed.path.startswith(p) for p in _ALLOWED_POST_PATHS):
        raise ValueError(
            f"URL does not look like a LinkedIn post permalink: {url!r}. "
            f"Expected paths starting with /feed/update/ or /posts/."
        )

    # Normalise to https and strip fragments/query params that may cause issues
    clean = f"https://www.linkedin.com{parsed.path}"
    if not clean.endswith("/"):
        clean += "/"
    return clean


def register_post_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register post-related tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"post", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_post(
        post_url: str,
        ctx: Context,
        download_images: bool = True,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get the full content and attached images of a single LinkedIn post.

        Navigates to the post URL, expands any "...more" truncation, extracts
        the full post text, and optionally downloads attached images to a
        temporary directory.

        Args:
            post_url: Full LinkedIn post permalink, e.g.
                "https://www.linkedin.com/feed/update/urn:li:activity:123/"
                or "https://www.linkedin.com/posts/username_slug-activity-123/"
                Only LinkedIn post URLs are accepted (SSRF protection).
            download_images: Whether to download attached images (default True).
                Each image is saved as a PNG to a temp dir and the local
                file path is returned. Useful for reading diagrams or charts
                embedded in posts. Capped at 10 images per post.

        Returns:
            Dict with:
            - url: canonical post URL after navigation (may differ from input)
            - sections["post"]: full post text with "...more" expanded
            - images: list of dicts, each with:
                - path: local file path to the downloaded PNG
                - index: 0-based position among returned post images
            - references: links, mentions, and attachments found in the post
            - section_errors: present when extraction fails
        """
        # Issue 1: Validate URL before any navigation (SSRF protection)
        try:
            clean_url = _validate_linkedin_post_url(post_url)
        except ValueError as e:
            raise_tool_error(e, "get_post")
            return {}  # unreachable, raise_tool_error always raises

        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_post"
            )

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to post"
            )

            # Navigate to the validated URL
            extracted = await extractor.extract_page(
                url=clean_url,
                section_name="post",
                max_scrolls=2,
            )

            sections: dict[str, str] = {}
            section_errors: dict[str, dict[str, Any]] = {}
            images: list[dict[str, Any]] = []
            references: dict[str, list[Reference]] = {}

            if extracted.text and extracted.text != _RATE_LIMITED_MSG:
                sections["post"] = extracted.text
            elif extracted.text == _RATE_LIMITED_MSG:
                section_errors["post"] = {
                    "error_type": "rate_limit",
                    "error_message": extracted.text,
                }
            elif extracted.error:
                section_errors["post"] = extracted.error

            # Issue 8: Preserve references (links, mentions, attachments)
            if extracted.references:
                references["post"] = extracted.references

            await ctx.report_progress(
                progress=40, total=100, message="Expanding post text"
            )

            # Issue 9: Click inline "...more" / "see more" to expand truncated text
            page = extractor._page
            try:
                see_more = page.locator(
                    "button.feed-shared-inline-show-more-text__see-more-less-toggle, "
                    "button[aria-label*='see more'], "
                    ".feed-shared-text .see-more"
                ).first
                if await see_more.count() > 0:
                    await see_more.click(timeout=3000)
                    # Re-extract text after expansion
                    expanded = await page.evaluate(
                        "() => (document.querySelector('main') || document.body).innerText || ''"
                    )
                    if expanded and len(expanded) > len(sections.get("post", "")):
                        sections["post"] = expanded
            except Exception as e:
                logger.debug("Could not expand post text: %s", e)

            # Issue 7: Return the actual navigated URL, not the raw input
            canonical_url = page.url or clean_url

            await ctx.report_progress(
                progress=60, total=100, message="Capturing images"
            )

            # Issue 3: Run image capture regardless of whether text extracted
            # (handles image-only posts)
            if download_images and not section_errors:
                # Issue 5: Create temp dir lazily, only when we have images to save
                tmp_dir: Path | None = None
                capture_count = 0

                # Issue 2: Use post-scoped selectors instead of broad `main img`
                selector = ", ".join(_POST_IMAGE_SELECTORS)
                img_locators = page.locator(selector)
                count = await img_locators.count()

                for i in range(count):
                    # Issue 4: Cap at max images to prevent timeout
                    if capture_count >= _MAX_POST_IMAGES:
                        logger.debug("Reached max image cap (%d)", _MAX_POST_IMAGES)
                        break

                    try:
                        img = img_locators.nth(i)
                        box = await img.bounding_box()
                        if not box or box["width"] < _MIN_IMAGE_SIZE_PX or box["height"] < _MIN_IMAGE_SIZE_PX:
                            continue

                        # Issue 5: Create temp dir on first successful image
                        if tmp_dir is None:
                            tmp_dir = Path(tempfile.mkdtemp(prefix="linkedin_post_imgs_"))

                        # Issue 6: Use 0-based index among returned images only
                        out_path = tmp_dir / f"image_{capture_count}.png"
                        await img.screenshot(path=str(out_path))
                        images.append({"path": str(out_path), "index": capture_count})
                        capture_count += 1
                        logger.info("Saved post image %d to %s", capture_count, out_path)
                    except Exception as e:
                        logger.debug("Could not capture image at DOM position %d: %s", i, e)

            await ctx.report_progress(
                progress=100, total=100, message="Complete"
            )

            # Issue 7: Return canonical URL from page navigation
            result: dict[str, Any] = {"url": canonical_url, "sections": sections}
            if images:
                result["images"] = images
            if references:
                result["references"] = references
            if section_errors:
                result["section_errors"] = section_errors
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_post")
        except Exception as e:
            raise_tool_error(e, "get_post")
