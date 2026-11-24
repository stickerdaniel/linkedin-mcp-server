"""Read one LinkedIn post through its rendered permalink page."""

from __future__ import annotations

from typing import Any

from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.identifiers import normalize_post_url


class PostReader:
    """Read post text, visible discussion and engagement without acting on them."""

    def __init__(self, capture: SectionCapture):
        self._capture = capture

    async def get_post(self, post_url: str, max_scrolls: int = 3) -> dict[str, Any]:
        """Read a post and the comments/counters the page currently renders.

        Scrolling is bounded; collapsed comments, replies and reaction lists
        are not expanded. Missing counters are not zero, and a rendered comment
        sample is not the full discussion. Keep these as the page's raw text
        rather than synthesizing counts or an engagement score.
        """
        url = normalize_post_url(post_url)
        extracted = await self._capture.capture(
            url,
            section_name="post",
            plan=CapturePlan(CaptureMode.STANDARD, max_scrolls=max_scrolls),
        )

        result: dict[str, Any] = {"url": url, "sections": {}}
        if extracted.text == RATE_LIMITED_SECTION_TEXT:
            result["section_errors"] = {"post": rate_limited_section_error()}
        elif extracted.text:
            result["sections"]["post"] = extracted.text
            if extracted.references:
                result["references"] = {"post": extracted.references}
        elif extracted.error:
            result["section_errors"] = {"post": extracted.error}
        return result
