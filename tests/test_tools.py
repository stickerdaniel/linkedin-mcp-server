from typing import Any, Callable, Coroutine
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.scraping.extractor import ExtractedSection, _RATE_LIMITED_MSG


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
    mock.get_inbox = AsyncMock(return_value=scrape_result)
    mock.get_conversation = AsyncMock(return_value=scrape_result)
    mock.search_conversations = AsyncMock(return_value=scrape_result)
    mock.send_message = AsyncMock(return_value=scrape_result)
    mock.extract_page = AsyncMock(
        return_value=ExtractedSection(text="some text", references=[])
    )
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
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Post 1\nPost 2", references=[])
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert "posts" in result["sections"]
        assert result["sections"]["posts"] == "Post 1\nPost 2"
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_get_company_posts_omits_rate_limited_sentinel(self, mock_context):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert result["sections"] == {}

    async def test_get_company_posts_returns_section_errors(self, mock_context):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[],
                error={"issue_template_path": "/tmp/company-posts-issue.md"},
            )
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert result["sections"] == {}
        assert result["section_errors"]["posts"]["issue_template_path"] == (
            "/tmp/company-posts-issue.md"
        )

    async def test_get_company_posts_omits_orphaned_references(self, mock_context):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[
                    {
                        "kind": "company",
                        "url": "/company/testcorp/",
                        "text": "TestCorp",
                    }
                ],
            )
        )

        from linkedin_mcp_server.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context, extractor=mock_extractor)
        assert result["sections"] == {}
        assert "references" not in result


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


class TestMessagingTools:
    async def test_get_inbox(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"inbox": "Conversation 1\nConversation 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_inbox")
        result = await tool_fn(mock_context, extractor=mock_extractor)
        assert "inbox" in result["sections"]
        mock_extractor.get_inbox.assert_awaited_once_with(limit=20)

    async def test_get_inbox_with_limit(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"inbox": "Conversations"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_inbox")
        result = await tool_fn(mock_context, limit=10, extractor=mock_extractor)
        assert "inbox" in result["sections"]
        mock_extractor.get_inbox.assert_awaited_once_with(limit=10)

    async def test_get_conversation_by_thread_id(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/messaging/thread/abc123/",
            "sections": {"conversation": "Hello\nHi there"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_conversation")
        result = await tool_fn(
            mock_context, thread_id="abc123", extractor=mock_extractor
        )
        assert "conversation" in result["sections"]
        mock_extractor.get_conversation.assert_awaited_once_with(
            linkedin_username=None, thread_id="abc123"
        )

    async def test_get_conversation_by_username(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/messaging/thread/xyz/",
            "sections": {"conversation": "Messages here"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_conversation")
        result = await tool_fn(
            mock_context, linkedin_username="testuser", extractor=mock_extractor
        )
        assert "conversation" in result["sections"]
        mock_extractor.get_conversation.assert_awaited_once_with(
            linkedin_username="testuser", thread_id=None
        )

    async def test_get_conversation_requires_identifier(self, mock_context):
        mock_extractor = MagicMock()
        mock_extractor.get_conversation = AsyncMock(
            side_effect=ValueError(
                "Provide at least one of linkedin_username or thread_id"
            )
        )

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "get_conversation")
        with pytest.raises(ValueError, match="linkedin_username or thread_id"):
            await tool_fn(mock_context, extractor=mock_extractor)

    async def test_search_conversations(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/messaging/",
            "sections": {"search_results": "Matching conversations"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "search_conversations")
        result = await tool_fn("project update", mock_context, extractor=mock_extractor)
        assert "search_results" in result["sections"]
        mock_extractor.search_conversations.assert_awaited_once_with("project update")

    async def test_send_message(self, mock_context):
        expected = {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"confirmation": "Message sent to testuser: Hello!"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "send_message")
        result = await tool_fn(
            "testuser", "Hello!", mock_context, extractor=mock_extractor
        )
        assert "confirmation" in result["sections"]
        mock_extractor.send_message.assert_awaited_once_with("testuser", "Hello!")

    async def test_send_message_error(self, mock_context):
        from fastmcp.exceptions import ToolError

        from linkedin_mcp_server.core.exceptions import LinkedInScraperException

        mock_extractor = MagicMock()
        mock_extractor.send_message = AsyncMock(
            side_effect=LinkedInScraperException("Message button not found")
        )

        from linkedin_mcp_server.tools.messaging import register_messaging_tools

        mcp = FastMCP("test")
        register_messaging_tools(mcp)

        tool_fn = await get_tool_fn(mcp, "send_message")
        with pytest.raises(ToolError, match="Message button not found"):
            await tool_fn("testuser", "Hi", mock_context, extractor=mock_extractor)


class TestToolTimeouts:
    async def test_all_tools_have_global_timeout(self):
        from linkedin_mcp_server.constants import TOOL_TIMEOUT_SECONDS
        from linkedin_mcp_server.server import create_mcp_server

        mcp = create_mcp_server()

        tool_names = (
            "get_person_profile",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "get_job_details",
            "search_jobs",
            "get_inbox",
            "get_conversation",
            "search_conversations",
            "send_message",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == TOOL_TIMEOUT_SECONDS
