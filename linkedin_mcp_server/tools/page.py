"""LinkedIn page scraping tool for supported public routes outside core tools."""

import logging
from typing import Any
from urllib.parse import urlparse

from fastmcp import Context, FastMCP

from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import LinkedInMCPError
from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG, ExtractedSection

logger = logging.getLogger(__name__)

_SUPPORTED_ROUTE_FAMILIES = (
    "/school/",
    "/showcase/",
    "/newsletters/",
    "/pulse/",
    "/feed/update/",
    "/events/",
    "/groups",
    "/services",
    "/products",
)


def _matches_supported_route(path: str) -> bool:
    """Return True when the path matches one of the supported route families."""
    for prefix in _SUPPORTED_ROUTE_FAMILIES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
            continue
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return False


def normalize_linkedin_page_url(linkedin_url: str) -> str:
    """Normalize a supported LinkedIn URL or relative path into a full URL."""
    raw = linkedin_url.strip()
    if not raw:
        raise LinkedInMCPError("LinkedIn URL is required.")

    candidate = raw
    if "://" not in candidate:
        candidate = (
            f"https://www.linkedin.com{candidate}"
            if candidate.startswith("/")
            else f"https://www.linkedin.com/{candidate}"
        )

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise LinkedInMCPError("LinkedIn URL must use http or https.")

    if parsed.netloc.lower() not in {"linkedin.com", "www.linkedin.com"}:
        raise LinkedInMCPError("LinkedIn URL must point to linkedin.com.")

    path = parsed.path or "/"
    if not _matches_supported_route(path):
        supported = ", ".join(_SUPPORTED_ROUTE_FAMILIES)
        raise LinkedInMCPError(
            f"Unsupported LinkedIn page URL. Supported route families: {supported}"
        )

    query = f"?{parsed.query}" if parsed.query else ""
    return f"https://www.linkedin.com{path}{query}"


def _build_page_result(url: str, extracted: ExtractedSection) -> dict[str, Any]:
    """Build the MCP response payload for a single generic page scrape."""
    sections: dict[str, str] = {}
    references: dict[str, list[Any]] = {}
    section_errors: dict[str, dict[str, Any]] = {}

    if extracted.text and extracted.text != _RATE_LIMITED_MSG:
        sections["page"] = extracted.text
        if extracted.references:
            references["page"] = extracted.references
    elif extracted.error:
        section_errors["page"] = extracted.error

    result: dict[str, Any] = {
        "url": url,
        "sections": sections,
    }
    if references:
        result["references"] = references
    if section_errors:
        result["section_errors"] = section_errors
    return result


def register_page_tools(mcp: FastMCP) -> None:
    """Register LinkedIn page tools with the MCP server."""

    @mcp.tool(
        timeout=TOOL_TIMEOUT_SECONDS,
        title="Get LinkedIn Page",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"page", "scraping"},
        exclude_args=["extractor"],
    )
    async def get_linkedin_page(
        linkedin_url: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get a LinkedIn page from a supported public route family not covered by the
        person/company/job tools.

        Args:
            linkedin_url: Full LinkedIn URL or relative LinkedIn path.
                Supported route families: /school/, /showcase/, /newsletters/,
                /pulse/, /feed/update/, /events/, /groups, /services, /products

        Returns:
            Dict with url, sections (page -> raw text), and optional references.
        """
        try:
            url = normalize_linkedin_page_url(linkedin_url)
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="get_linkedin_page"
            )

            logger.info("Scraping LinkedIn page: %s", url)
            await ctx.report_progress(
                progress=0, total=100, message="Starting LinkedIn page scrape"
            )

            extracted = await extractor.extract_page(url, section_name="page")

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return _build_page_result(url, extracted)

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_linkedin_page")
        except Exception as e:
            raise_tool_error(e, "get_linkedin_page")  # NoReturn
