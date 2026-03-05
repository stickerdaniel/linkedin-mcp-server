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
    async def test_get_person_profile_success(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn("test-user", mock_context, extractor=mock_extractor)
        assert result["url"] == "https://www.linkedin.com/in/test-user/"
        assert "main_profile" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_get_person_profile_with_sections(self, mock_context):
        """Verify sections parameter is passed through."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {
                "main_profile": "John Doe",
                "experience": "Work history",
                "contact_info": "Email: test@test.com",
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="experience,contact_info",
            extractor=mock_extractor,
        )
        assert "main_profile" in result["sections"]
        assert "experience" in result["sections"]
        assert "contact_info" in result["sections"]
        # Verify scrape_person was called exactly once with a set[str]
        mock_extractor.scrape_person.assert_awaited_once()
        call_args = mock_extractor.scrape_person.call_args
        assert isinstance(call_args[0][1], set)
        assert "experience" in call_args[0][1]
        assert "contact_info" in call_args[0][1]

    async def test_get_person_profile_unknown_section(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="bogus_section",
            extractor=mock_extractor,
        )
        assert result["unknown_sections"] == ["bogus_section"]

    async def test_get_person_profile_error(self, mock_context):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.scrape_person = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("test-user", mock_context, extractor=mock_extractor)

    async def test_get_person_profile_auth_error(self, monkeypatch):
        """Auth failures in the DI layer produce proper ToolError responses."""
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import AuthenticationError

        mock_browser = MagicMock()
        mock_browser.page = MagicMock()
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.get_or_create_browser",
            AsyncMock(return_value=mock_browser),
        )
        monkeypatch.setattr(
            "linkedin_mcp_server.dependencies.ensure_authenticated",
            AsyncMock(side_effect=AuthenticationError("Session expired or invalid.")),
        )

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ToolError, match="Authentication failed"):
            await mcp.call_tool("get_person_profile", {"linkedin_username": "test"})

    async def test_search_people(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/search/results/people/?keywords=AI+engineer&location=New+York",
            "sections": {"search_results": "Jane Doe\nAI Engineer at Acme\nNew York"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn(
            "AI engineer", mock_context, location="New York", extractor=mock_extractor
        )
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result
        mock_extractor.search_people.assert_awaited_once_with("AI engineer", "New York")


class TestCompanyTools:
    async def test_get_company_profile(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert "about" in result["sections"]
        assert "pages_visited" not in result

    async def test_get_company_profile_unknown_section(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn(
            "testcorp", mock_context, sections="bogus", extractor=mock_extractor
        )
        assert result["unknown_sections"] == ["bogus"]

    async def test_get_company_posts(self, mock_context):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(return_value="Post 1\nPost 2")

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert "posts" in result["sections"]
        assert result["sections"]["posts"] == "Post 1\nPost 2"
        assert "pages_visited" not in result
        assert "sections_requested" not in result


class TestJobTools:
    async def test_get_job_details(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/jobs/view/12345/",
            "sections": {"job_posting": "Software Engineer\nGreat opportunity"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_job_details")
        result = await tool_fn("12345", mock_context, extractor=mock_extractor)
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result

    async def test_search_jobs(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/jobs/search/?keywords=python",
            "sections": {"search_results": "Job 1\nJob 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_jobs")
        result = await tool_fn(
            "python", mock_context, location="Remote", extractor=mock_extractor
        )
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result


class TestToolTimeouts:
    async def test_all_tools_have_global_timeout(self):
        from linkedin_mcp_server.server import create_mcp_server

        mcp = create_mcp_server()

        tool_names = (
            "get_person_profile",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "get_job_details",
            "search_jobs",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == 90.0
