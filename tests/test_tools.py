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


def _make_mock_extractor(scrape_result: dict) -> MagicMock:
    """Create a mock LinkedInExtractor that returns the given result."""
    mock = MagicMock()
    mock.scrape_person = AsyncMock(return_value=scrape_result)
    mock.scrape_company = AsyncMock(return_value=scrape_result)
    mock.scrape_job = AsyncMock(return_value=scrape_result)
    mock.search_jobs = AsyncMock(return_value=scrape_result)
    mock.search_people = AsyncMock(return_value=scrape_result)
    mock.extract_page = AsyncMock(return_value="some text")
    return mock


class TestPersonTool:
    async def test_get_person_profile_success(
        self, mock_context, patch_tool_deps, monkeypatch
    ):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
            "pages_visited": ["https://www.linkedin.com/in/test-user/"],
            "sections_requested": ["main_profile"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn("test-user", mock_context)
        assert result["url"] == "https://www.linkedin.com/in/test-user/"
        assert "main_profile" in result["sections"]
        assert result["sections_requested"] == ["main_profile"]

    async def test_get_person_profile_with_sections(
        self, mock_context, patch_tool_deps, monkeypatch
    ):
        """Verify sections parameter is passed through."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {
                "main_profile": "John Doe",
                "experience": "Work history",
                "contact_info": "Email: test@test.com",
            },
            "pages_visited": [
                "https://www.linkedin.com/in/test-user/",
                "https://www.linkedin.com/in/test-user/details/experience/",
                "https://www.linkedin.com/in/test-user/overlay/contact-info/",
            ],
            "sections_requested": ["main_profile", "experience", "contact_info"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user", mock_context, sections="experience,contact_info"
        )
        assert result["sections_requested"] == [
            "main_profile",
            "experience",
            "contact_info",
        ]
        mock_extractor.scrape_person.assert_awaited_once()

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

    async def test_search_people(self, mock_context, patch_tool_deps, monkeypatch):
        expected = {
            "url": "https://www.linkedin.com/search/results/people/?keywords=AI+engineer&location=New+York",
            "sections": {"search_results": "Jane Doe\nAI Engineer at Acme\nNew York"},
            "pages_visited": [
                "https://www.linkedin.com/search/results/people/?keywords=AI+engineer&location=New+York"
            ],
            "sections_requested": ["search_results"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.person.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn("AI engineer", mock_context, location="New York")
        assert "search_results" in result["sections"]
        mock_extractor.search_people.assert_awaited_once_with("AI engineer", "New York")


class TestCompanyTools:
    async def test_get_company_profile(
        self, mock_context, patch_tool_deps, monkeypatch
    ):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
            "pages_visited": ["https://www.linkedin.com/company/testcorp/about/"],
            "sections_requested": ["about"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn("testcorp", mock_context)
        assert "about" in result["sections"]

    async def test_get_company_posts(self, mock_context, patch_tool_deps, monkeypatch):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(return_value="Post 1\nPost 2")
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert "posts" in result["sections"]
        assert result["sections"]["posts"] == "Post 1\nPost 2"
        assert result["sections_requested"] == ["posts"]


class TestJobTools:
    async def test_get_job_details(self, mock_context, patch_tool_deps, monkeypatch):
        expected = {
            "url": "https://www.linkedin.com/jobs/view/12345/",
            "sections": {"job_posting": "Software Engineer\nGreat opportunity"},
            "pages_visited": ["https://www.linkedin.com/jobs/view/12345/"],
            "sections_requested": ["job_posting"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.job.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_job_details")
        result = await tool_fn("12345", mock_context)
        assert "job_posting" in result["sections"]

    async def test_search_jobs(self, mock_context, patch_tool_deps, monkeypatch):
        expected = {
            "url": "https://www.linkedin.com/jobs/search/?keywords=python",
            "sections": {"search_results": "Job 1\nJob 2"},
            "pages_visited": ["https://www.linkedin.com/jobs/search/?keywords=python"],
            "sections_requested": ["search_results"],
        }
        mock_extractor = _make_mock_extractor(expected)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.job.LinkedInExtractor",
            lambda *a, **kw: mock_extractor,
        )

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_jobs")
        result = await tool_fn("python", mock_context, location="Remote")
        assert "search_results" in result["sections"]
