from typing import Any, Callable, Coroutine
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP


async def get_tool_fn(
    mcp: FastMCP, name: str
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Extract tool function from FastMCP by name using public API."""
    tool = await mcp.get_tool(name)
    if tool is None:
        raise ValueError(f"Tool '{name}' not found")
    return tool.fn  # type: ignore[attr-defined]


@pytest.fixture
def patch_tool_deps(monkeypatch):
    """Patch ensure_authenticated and get_or_create_browser for all tools."""
    mock_browser = MagicMock()
    mock_browser.page = MagicMock()

    for module in ["person", "company", "job"]:
        monkeypatch.setattr(
            f"linkedin_mcp_server.tools.{module}.ensure_authenticated", AsyncMock()
        )
        monkeypatch.setattr(
            f"linkedin_mcp_server.tools.{module}.get_or_create_browser",
            AsyncMock(return_value=mock_browser),
        )

    return mock_browser


class TestPersonTool:
    async def test_get_person_profile_success(
        self, mock_context, patch_tool_deps, monkeypatch
    ):
        mock_person = MagicMock()
        mock_person.to_dict.return_value = {"full_name": "Test User"}
        mock_scraper = MagicMock()
        mock_scraper.scrape = AsyncMock(return_value=mock_person)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.PersonScraper",
            lambda *a, **kw: mock_scraper,
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn("test-user", mock_context)
        assert result["full_name"] == "Test User"

    async def test_get_person_profile_error(self, mock_context, monkeypatch):
        from linkedin_mcp_server.exceptions import SessionExpiredError

        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.ensure_authenticated",
            AsyncMock(side_effect=SessionExpiredError()),
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn("test-user", mock_context)
        assert result["error"] == "session_expired"


class TestCompanyTools:
    async def test_get_company_profile(
        self, mock_context, patch_tool_deps, monkeypatch
    ):
        mock_company = MagicMock()
        mock_company.to_dict.return_value = {"name": "Test Corp"}
        mock_scraper = MagicMock()
        mock_scraper.scrape = AsyncMock(return_value=mock_company)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company.CompanyScraper",
            lambda *a, **kw: mock_scraper,
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn("testcorp", mock_context)
        assert result["name"] == "Test Corp"

    async def test_get_company_posts(self, mock_context, patch_tool_deps, monkeypatch):
        mock_post = MagicMock()
        mock_post.to_dict.return_value = {"text": "Hello world"}
        mock_scraper = MagicMock()
        mock_scraper.scrape = AsyncMock(return_value=[mock_post])
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company.CompanyPostsScraper",
            lambda *a, **kw: mock_scraper,
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, limit=5)
        assert result["count"] == 1
        assert result["posts"][0]["text"] == "Hello world"


class TestJobTools:
    async def test_get_job_details(self, mock_context, patch_tool_deps, monkeypatch):
        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"title": "Engineer"}
        mock_scraper = MagicMock()
        mock_scraper.scrape = AsyncMock(return_value=mock_job)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.job.JobScraper", lambda *a, **kw: mock_scraper
        )

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_job_details")
        result = await tool_fn("12345", mock_context)
        assert result["title"] == "Engineer"

    async def test_search_jobs(self, mock_context, patch_tool_deps, monkeypatch):
        mock_scraper = MagicMock()
        mock_scraper.search = AsyncMock(return_value=["url1", "url2"])
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.job.JobSearchScraper",
            lambda *a, **kw: mock_scraper,
        )

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_jobs")
        result = await tool_fn("python", mock_context, location="Remote", limit=10)
        assert result["count"] == 2
        assert "url1" in result["job_urls"]
