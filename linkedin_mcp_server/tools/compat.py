"""Compatibility tools for ChatGPT-style MCP retrieval."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error


class SearchResult(BaseModel):
    id: str
    title: str
    url: str


class SearchResponse(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)


class FetchResponse(BaseModel):
    id: str
    title: str
    text: str
    url: str
    metadata: dict[str, Any] = Field(default_factory=dict)


_LINKEDIN_BASE = "https://www.linkedin.com"
_PERSON_RE = re.compile(r"/in/([^/?#]+)/?")
_COMPANY_RE = re.compile(r"/company/([^/?#]+)/?")
_JOB_RE = re.compile(r"/jobs/view/(\d+)/?")


def _absolute_linkedin_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        url = f"/{url}"
    return f"{_LINKEDIN_BASE}{url}"


def _reference_id(kind: str, url: str) -> str | None:
    parsed = urlparse(_absolute_linkedin_url(url))
    path = parsed.path
    if kind == "person":
        match = _PERSON_RE.search(path)
        return f"person:{match.group(1)}" if match else None
    if kind == "company":
        match = _COMPANY_RE.search(path)
        return f"company:{match.group(1)}" if match else None
    if kind == "job":
        match = _JOB_RE.search(path)
        return f"job:{match.group(1)}" if match else None
    if kind == "feed_post":
        return f"post:{url}"
    return None


def _title_for_reference(reference: dict[str, Any]) -> str:
    for key in ("text", "context", "url"):
        value = reference.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return "LinkedIn result"


def _collect_results(
    *payloads: dict[str, Any], limit: int = 10
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for payload in payloads:
        references = payload.get("references", {})
        if isinstance(references, dict):
            for section_references in references.values():
                if not isinstance(section_references, list):
                    continue
                for reference in section_references:
                    if not isinstance(reference, dict):
                        continue
                    kind = reference.get("kind")
                    url = reference.get("url")
                    if not isinstance(kind, str) or not isinstance(url, str):
                        continue
                    result_id = _reference_id(kind, url)
                    if result_id is None or result_id in seen:
                        continue
                    seen.add(result_id)
                    results.append(
                        {
                            "id": result_id,
                            "title": _title_for_reference(reference),
                            "url": _absolute_linkedin_url(url),
                        }
                    )
                    if len(results) >= limit:
                        return results

        for job_id in payload.get("job_ids", []):
            if not isinstance(job_id, str):
                continue
            result_id = f"job:{job_id}"
            if result_id in seen:
                continue
            seen.add(result_id)
            results.append(
                {
                    "id": result_id,
                    "title": f"LinkedIn job {job_id}",
                    "url": f"{_LINKEDIN_BASE}/jobs/view/{job_id}/",
                }
            )
            if len(results) >= limit:
                return results
    return results


def _sections_to_text(sections: dict[str, Any]) -> str:
    chunks: list[str] = []
    for name, text in sections.items():
        if isinstance(text, str) and text.strip():
            chunks.append(f"## {name}\n{text.strip()}")
    return "\n\n".join(chunks)


def register_compat_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register generic retrieval tools used by ChatGPT data connectors."""

    @mcp.tool(
        name="search",
        timeout=tool_timeout,
        title="Search LinkedIn",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"compat", "search"},
        output_schema=SearchResponse.model_json_schema(),
        exclude_args=["extractor"],
    )
    async def search(
        query: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """Search LinkedIn people, companies, and jobs with a generic result shape."""
        try:
            extractor = extractor or await get_ready_extractor(ctx, tool_name="search")
            await ctx.report_progress(progress=0, total=100, message="Searching people")
            people = await extractor.search_people(query)
            await ctx.report_progress(
                progress=35, total=100, message="Searching companies"
            )
            companies = await extractor.search_companies(query)
            await ctx.report_progress(progress=70, total=100, message="Searching jobs")
            jobs = await extractor.search_jobs(query, max_pages=1)
            await ctx.report_progress(progress=100, total=100, message="Complete")
            return {"results": _collect_results(people, companies, jobs)}
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "search")
        except Exception as e:
            raise_tool_error(e, "search")

    @mcp.tool(
        name="fetch",
        timeout=tool_timeout,
        title="Fetch LinkedIn Result",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"compat", "fetch"},
        output_schema=FetchResponse.model_json_schema(),
        exclude_args=["extractor"],
    )
    async def fetch(
        id: str,
        ctx: Context,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """Fetch one generic LinkedIn search result by id."""
        try:
            if ":" not in id:
                raise ToolError("Invalid id. Expected kind:value, such as person:slug")
            kind, value = id.split(":", 1)
            if not value:
                raise ToolError("Invalid id. Missing value after kind prefix")

            extractor = extractor or await get_ready_extractor(ctx, tool_name="fetch")
            cb = MCPContextProgressCallback(ctx)
            if kind == "person":
                result = await extractor.scrape_person(
                    value,
                    {"main_profile", "experience", "education", "skills"},
                    callbacks=cb,
                )
                title = value
            elif kind == "company":
                result = await extractor.scrape_company(value, {"about"}, callbacks=cb)
                title = value
            elif kind == "job":
                result = await extractor.scrape_job(value)
                title = f"LinkedIn job {value}"
            elif kind == "post":
                url = _absolute_linkedin_url(value)
                extracted = await extractor.extract_page(url, section_name="post")
                result = {
                    "url": url,
                    "sections": {"post": extracted.text} if extracted.text else {},
                    "references": {"post": extracted.references}
                    if extracted.references
                    else {},
                }
                title = "LinkedIn post"
            else:
                raise ToolError(
                    "Invalid id kind. Expected one of: person, company, job, post"
                )

            sections = result.get("sections", {})
            text = _sections_to_text(sections if isinstance(sections, dict) else {})
            return {
                "id": id,
                "title": title,
                "text": text,
                "url": result.get("url", ""),
                "metadata": {
                    "kind": kind,
                    "sections": list(sections) if isinstance(sections, dict) else [],
                    "references": result.get("references", {}),
                },
            }
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "fetch")
        except Exception as e:
            raise_tool_error(e, "fetch")
