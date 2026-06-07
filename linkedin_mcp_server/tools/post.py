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
from typing import Any

from fastmcp import Context, FastMCP

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG

logger = logging.getLogger(__name__)


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

        Navigates to the post URL, extracts the full post text (bypassing
        the "...more" truncation), and optionally downloads any attached
        images to a temporary directory.

        Args:
            post_url: Full LinkedIn post URL, e.g.
                "https://www.linkedin.com/feed/update/urn:li:activity:123/"
                or "https://www.linkedin.com/posts/username_slug-activity-123/"
            download_images: Whether to download attached images (default True).
                Each image is saved as a PNG to a temp dir and the local
                file path is returned. Useful for reading diagrams or charts
                embedded in posts.

        Returns:
            Dict with:
            - url: canonical post URL
            - sections["post"]: full post text
            - images: list of dicts with keys:
                - path: local file path to the downloaded PNG
                - index: image position in the post (0-based)
            - section_errors: present when extraction fails
        """
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_post"
            )

            await ctx.report_progress(
                progress=0, total=100, message="Navigating to post"
            )

            # Navigate and extract text using the existing page-level extractor
            extracted = await extractor.extract_page(
                url=post_url,
                section_name="post",
                max_scrolls=2,
            )

            sections: dict[str, str] = {}
            section_errors: dict[str, dict[str, Any]] = {}
            images: list[dict[str, Any]] = []

            if extracted.text and extracted.text != _RATE_LIMITED_MSG:
                sections["post"] = extracted.text
            elif extracted.text == _RATE_LIMITED_MSG:
                section_errors["post"] = {
                    "error_type": "rate_limit",
                    "error_message": extracted.text,
                }
            elif extracted.error:
                section_errors["post"] = extracted.error

            await ctx.report_progress(
                progress=50, total=100, message="Extracting images"
            )

            # Download attached images if requested and text extraction succeeded
            if download_images and sections.get("post"):
                page = extractor._page
                tmp_dir = Path(tempfile.mkdtemp(prefix="linkedin_post_imgs_"))

                # Find all images in the post content area (exclude avatars/icons)
                img_locators = page.locator(
                    "main img, article img, "
                    ".feed-shared-update-v2__content img, "
                    ".update-components-image img, "
                    ".ivm-view-attr__img-wrapper img"
                )
                count = await img_locators.count()

                for i in range(count):
                    try:
                        img = img_locators.nth(i)
                        # Skip tiny images (icons, avatars < 100px)
                        box = await img.bounding_box()
                        if not box or box["width"] < 100 or box["height"] < 100:
                            continue

                        out_path = tmp_dir / f"image_{i}.png"
                        await img.screenshot(path=str(out_path))
                        images.append({"path": str(out_path), "index": i})
                        logger.info("Saved post image %d to %s", i, out_path)
                    except Exception as e:
                        logger.debug("Could not capture image %d: %s", i, e)

            await ctx.report_progress(
                progress=100, total=100, message="Complete"
            )

            result: dict[str, Any] = {"url": post_url, "sections": sections}
            if images:
                result["images"] = images
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
